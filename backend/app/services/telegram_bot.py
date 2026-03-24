import asyncio
import logging
from telegram import Update, Bot
from telegram.constants import ChatAction
from app.services import copilot
from app.config import settings

logger = logging.getLogger(__name__)

AGENT_CMD_PREFIX = "/agent "


def _is_user_allowed(username: str | None) -> bool:
    if not settings.telegram_allowed_users:
        return True
    return username in settings.telegram_allowed_users


async def _send_typing_loop(bot: Bot, chat_id: int, stop_event: asyncio.Event):
    """Send typing indicator every 5 seconds until stopped."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(5)


async def handle_telegram_message(update_data: dict) -> None:
    """Process an incoming Telegram update from the webhook."""
    update = Update.de_json(update_data, Bot(settings.telegram_bot_token))

    if not update or not update.message or not update.message.text:
        return

    message = update.message
    chat_id = message.chat_id
    text = message.text.strip()
    username = message.from_user.username if message.from_user else None

    if not _is_user_allowed(username):
        bot = Bot(settings.telegram_bot_token)
        await bot.send_message(chat_id=chat_id, text="⛔ You are not authorized to use this bot.")
        return

    bot = Bot(settings.telegram_bot_token)

    # Parse agent command: /agent stock-analysis-pro AAPL at $242.50
    agent_name = None
    prompt = text

    if text.startswith(AGENT_CMD_PREFIX):
        parts = text[len(AGENT_CMD_PREFIX):].strip().split(" ", 1)
        agent_name = parts[0]
        prompt = parts[1] if len(parts) > 1 else ""

    elif text.startswith("/start"):
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⚡ OpenCopilot Bot\n\n"
                "Send me any message to chat with GitHub Copilot.\n\n"
                "To run a specific agent:\n"
                "/agent agent-name your prompt here\n\n"
                "Examples:\n"
                "• Hello, help me write a Python script\n"
                "• /agent stock-analysis-pro AAPL at $242.50"
            ),
        )
        return

    if not prompt:
        await bot.send_message(chat_id=chat_id, text="Please provide a message or prompt.")
        return

    # Send initial status message
    if agent_name:
        status_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Running agent `{agent_name}`... This may take a few minutes.",
        )
    else:
        status_msg = await bot.send_message(
            chat_id=chat_id,
            text="⏳ Processing...",
        )

    # Keep typing indicator alive while processing
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_send_typing_loop(bot, chat_id, stop_typing))

    # Stream output from Copilot
    try:
        chunks = []
        if agent_name:
            stream = copilot.run_with_agent(prompt, agent_name)
        else:
            stream = copilot.run_gh_copilot(prompt)

        async for chunk in stream:
            chunks.append(chunk)

        result = "".join(chunks)
    except Exception as e:
        logger.exception("Copilot execution failed")
        result = f"❌ Error: {e}"
    finally:
        stop_typing.set()
        typing_task.cancel()

    # Delete the "processing" message
    try:
        await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
    except Exception:
        pass

    if not result.strip():
        result = "_(No response from Copilot)_"

    # Clean up usage stats from gh copilot output
    result = _clean_output(result)

    # Send result — split if needed for Telegram's 4096 char limit
    for chunk in _split_message(result):
        await bot.send_message(chat_id=chat_id, text=chunk)


def _clean_output(text: str) -> str:
    """Remove gh copilot usage stats and noise from the output."""
    lines = text.split("\n")
    cleaned = []
    skip_section = False
    for line in lines:
        # Skip usage stats block at the end
        if line.strip().startswith("Total usage est:") or line.strip().startswith("API time spent:"):
            skip_section = True
        if skip_section:
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message into chunks that fit Telegram's limit."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Try to split at a newline near the limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


async def setup_webhook(webhook_url: str) -> dict:
    """Set the Telegram webhook URL."""
    bot = Bot(settings.telegram_bot_token)
    result = await bot.set_webhook(
        url=webhook_url,
        secret_token=settings.telegram_webhook_secret or None,
    )
    info = await bot.get_webhook_info()
    return {
        "ok": result,
        "webhook_url": info.url,
        "pending_updates": info.pending_update_count,
    }


async def remove_webhook() -> dict:
    """Remove the Telegram webhook."""
    bot = Bot(settings.telegram_bot_token)
    result = await bot.delete_webhook()
    return {"ok": result}
