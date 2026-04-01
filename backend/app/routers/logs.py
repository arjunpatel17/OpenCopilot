import asyncio
import json
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from app.auth import get_current_user, verify_ws_token
from app.services.copilot import subscribe_logs, unsubscribe_logs, get_log_snapshot, get_active_process

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/snapshot")
async def log_snapshot(user: dict = Depends(get_current_user)):
    """Return the current log buffer and active process info."""
    return {
        "logs": get_log_snapshot(),
        "active_process": get_active_process(),
    }


@router.websocket("/stream")
async def log_stream(ws: WebSocket):
    """Stream live log entries to the client via WebSocket."""
    user = await verify_ws_token(ws)
    if user is None:
        await ws.close(code=1008, reason="Authentication required")
        return
    await ws.accept()
    queue = subscribe_logs()
    try:
        # Send current state first
        await ws.send_json({
            "type": "snapshot",
            "logs": get_log_snapshot(),
            "active_process": get_active_process(),
        })
        # Stream new entries
        while True:
            try:
                entry = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_json(entry)
            except asyncio.TimeoutError:
                # Send keepalive
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        unsubscribe_logs(queue)
