import json
import uuid
from datetime import datetime, timezone
from app.models.chat import ChatSession, ChatMessage, ChatSessionSummary
from app.services import blob_storage

SESSIONS_PREFIX = "sessions/"


def _session_path(session_id: str) -> str:
    return f"{SESSIONS_PREFIX}{session_id}.json"


def create_session(title: str = "New Chat") -> ChatSession:
    now = datetime.now(timezone.utc)
    session = ChatSession(
        id=str(uuid.uuid4()),
        title=title,
        created_at=now,
        updated_at=now,
        messages=[],
    )
    _save_session(session)
    return session


def _save_session(session: ChatSession) -> None:
    data = session.model_dump_json(indent=2)
    blob_storage.upload_blob(
        _session_path(session.id),
        data.encode("utf-8"),
        content_type="application/json",
    )


def get_session(session_id: str) -> ChatSession:
    data = blob_storage.get_blob_content(_session_path(session_id))
    return ChatSession.model_validate_json(data)


def update_session(session: ChatSession) -> None:
    session.updated_at = datetime.now(timezone.utc)
    _save_session(session)


def add_message(session_id: str, message: ChatMessage) -> ChatSession:
    session = get_session(session_id)
    session.messages.append(message)
    # Auto-title from first user message
    if session.title == "New Chat" and message.role == "user":
        first_text = message.contents[0].content if message.contents else ""
        session.title = first_text[:80] if first_text else "New Chat"
    update_session(session)
    return session


def list_sessions() -> list[ChatSessionSummary]:
    blobs = blob_storage.list_blobs(SESSIONS_PREFIX)
    sessions = []
    for blob_info in blobs:
        if blob_info.is_folder:
            continue
        try:
            data = blob_storage.get_blob_content(blob_info.path)
            session = ChatSession.model_validate_json(data)
            sessions.append(ChatSessionSummary(
                id=session.id,
                title=session.title,
                created_at=session.created_at,
                updated_at=session.updated_at,
                message_count=len(session.messages),
            ))
        except Exception:
            continue
    return sorted(sessions, key=lambda s: s.updated_at, reverse=True)


def delete_session(session_id: str) -> None:
    blob_storage.delete_blob(_session_path(session_id))
