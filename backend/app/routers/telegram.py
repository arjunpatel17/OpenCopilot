import asyncio
import logging
from fastapi import APIRouter, Request, Depends, HTTPException
from app.auth import get_current_user
from app.config import settings
from app.services import telegram_bot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.post("/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates via webhook. Verified by secret token header."""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")

    # Verify webhook secret if configured
    if settings.telegram_webhook_secret:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != settings.telegram_webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update_data = await request.json()
    # Run in background so webhook responds immediately (Telegram has a ~60s timeout)
    task = asyncio.create_task(telegram_bot.handle_telegram_message(update_data))
    task.add_done_callback(lambda t: logger.exception("Telegram handler failed", exc_info=t.exception()) if t.exception() else None)
    return {"ok": True}


@router.post("/setup-webhook")
async def setup_webhook(request: Request, user: dict = Depends(get_current_user)):
    """Set up the Telegram webhook to point to this server."""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=400, detail="Set TELEGRAM_BOT_TOKEN first")

    body = await request.json()
    base_url = body.get("base_url", "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="Provide base_url in request body")

    webhook_url = f"{base_url}/api/telegram/webhook"
    result = await telegram_bot.setup_webhook(webhook_url)
    return result


@router.delete("/webhook")
async def remove_webhook(user: dict = Depends(get_current_user)):
    """Remove the Telegram webhook."""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=400, detail="Set TELEGRAM_BOT_TOKEN first")
    return await telegram_bot.remove_webhook()
