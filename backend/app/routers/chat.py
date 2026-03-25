import json
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from app.auth import get_current_user
from app.models.chat import (
    ChatRequest, ChatMessage, ChatSessionSummary, ChatSession,
    MessageRole, MessageContent, MessageContentType,
)
from app.services import copilot, session_manager, response_parser, blob_storage
from app.services.copilot import TOOL_EVENT_PREFIX

router = APIRouter(prefix="/api/chat", tags=["chat"])

AGENT_SLASH_RE = re.compile(r"^/(\S+)\s*(.*)", re.DOTALL)

# Commands that should not be treated as agent names
RESERVED_COMMANDS = {"models", "help", "version", "agents", "skills", "mcps", "files", "plan", "explain", "suggest", "sync"}


def _parse_user_input(message: str) -> tuple[str | None, str]:
    """Parse slash commands like /stock-analysis-pro AAPL at $242.50."""
    match = AGENT_SLASH_RE.match(message)
    if match and match.group(1).lower() not in RESERVED_COMMANDS:
        return match.group(1), match.group(2).strip()
    return None, message


@router.get("/sessions", response_model=list[ChatSessionSummary])
async def list_sessions(user: dict = Depends(get_current_user)):
    return session_manager.list_sessions()


@router.get("/sessions/{session_id}", response_model=ChatSession)
async def get_session(session_id: str, user: dict = Depends(get_current_user)):
    return session_manager.get_session(session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    session_manager.delete_session(session_id)


@router.post("")
async def chat_sync(request: ChatRequest, user: dict = Depends(get_current_user)):
    """Synchronous chat endpoint for short queries."""
    agent_name, prompt = _parse_user_input(request.message)
    if request.agent_name:
        agent_name = request.agent_name
    model_name = request.model_name

    # Get or create session
    if request.session_id:
        session = session_manager.get_session(request.session_id)
    else:
        session = session_manager.create_session()

    # Save user message
    user_msg = ChatMessage(
        role=MessageRole.user,
        contents=[MessageContent(type=MessageContentType.text, content=request.message)],
        timestamp=datetime.now(timezone.utc),
        agent_name=agent_name,
    )
    session_manager.add_message(session.id, user_msg)

    # Run Copilot
    raw_output = await copilot.run_copilot_sync(prompt, agent_name, model_name=model_name)

    # Sync any files created by the copilot to blob storage
    blob_storage.sync_workspace_to_storage()

    # Parse output
    contents = response_parser.parse_copilot_output(raw_output)

    # Save assistant message
    assistant_msg = ChatMessage(
        role=MessageRole.assistant,
        contents=contents,
        timestamp=datetime.now(timezone.utc),
        agent_name=agent_name,
    )
    session_manager.add_message(session.id, assistant_msg)

    return {
        "session_id": session.id,
        "message": assistant_msg.model_dump(),
    }


@router.websocket("/stream")
async def chat_stream(websocket: WebSocket):
    """WebSocket endpoint for streaming chat responses."""
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_text()
            request = json.loads(data)
            message = request.get("message", "")
            agent_name_req = request.get("agent_name")
            model_name = request.get("model_name")
            session_id = request.get("session_id")
            image_path = request.get("image_path")  # path to an uploaded image

            agent_name, prompt = _parse_user_input(message)
            if agent_name_req:
                agent_name = agent_name_req

            # If an image was attached, prepend view instruction to the prompt
            if image_path:
                from pathlib import Path
                from app.config import settings
                abs_path = Path(settings.workspace_dir) / image_path
                prompt = f"Look at the image file at {abs_path} using the view tool. {prompt}"

            # Get or create session
            if session_id:
                try:
                    session = session_manager.get_session(session_id)
                except Exception:
                    session = session_manager.create_session()
            else:
                session = session_manager.create_session()

            # Notify client of session ID
            await websocket.send_text(json.dumps({
                "type": "session",
                "session_id": session.id,
            }))

            # Save user message
            user_msg = ChatMessage(
                role=MessageRole.user,
                contents=[MessageContent(type=MessageContentType.text, content=message)],
                timestamp=datetime.now(timezone.utc),
                agent_name=agent_name,
            )
            session_manager.add_message(session.id, user_msg)

            # Stream Copilot output
            full_output = []
            if agent_name:
                stream = copilot.run_code_chat(prompt, agent_name, model_name=model_name)
            else:
                stream = copilot.run_gh_copilot(prompt, model_name=model_name)

            async for chunk in stream:
                # Detect structured tool event markers and send as separate message type
                stripped = chunk.strip()
                if stripped.startswith(TOOL_EVENT_PREFIX):
                    marker = stripped[len(TOOL_EVENT_PREFIX):]
                    parts = marker.split("|", 1)
                    tool_name = parts[0]
                    tool_desc = parts[1] if len(parts) > 1 else tool_name
                    await websocket.send_text(json.dumps({
                        "type": "tool",
                        "tool_name": tool_name,
                        "description": tool_desc,
                    }))
                    continue
                # Skip bare turn separators
                if stripped == '---':
                    continue
                full_output.append(chunk)
                await websocket.send_text(json.dumps({
                    "type": "chunk",
                    "content": chunk,
                }))

            # Parse full output and save
            raw = "".join(full_output)
            contents = response_parser.parse_copilot_output(raw)

            # Sync any files created by the copilot to blob storage
            blob_storage.sync_workspace_to_storage()

            assistant_msg = ChatMessage(
                role=MessageRole.assistant,
                contents=contents,
                timestamp=datetime.now(timezone.utc),
                agent_name=agent_name,
            )
            session_manager.add_message(session.id, assistant_msg)

            await websocket.send_text(json.dumps({
                "type": "done",
                "contents": [c.model_dump() for c in contents],
            }))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "content": str(e),
            }))
        except Exception:
            pass
