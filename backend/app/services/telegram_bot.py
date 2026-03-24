import asyncio
import logging
import httpx
from telegram import Update, Bot
from telegram.constants import ChatAction
from app.services import copilot
from app.config import settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

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
    try:
        await _handle_telegram_message_inner(update_data)
    except Exception:
        logger.exception("Unhandled error in telegram handler")


async def _handle_telegram_message_inner(update_data: dict) -> None:
    update = Update.de_json(update_data, Bot(settings.telegram_bot_token))

    if not update or not update.message:
        return

    message = update.message
    chat_id = message.chat_id
    username = message.from_user.username if message.from_user else None

    if not _is_user_allowed(username):
        bot = Bot(settings.telegram_bot_token)
        await bot.send_message(chat_id=chat_id, text="⛔ You are not authorized to use this bot.")
        return

    bot = Bot(settings.telegram_bot_token)

    # Handle voice messages
    if message.voice or message.audio:
        voice = message.voice or message.audio
        text = await _transcribe_voice(bot, voice.file_id)
        if text is None:
            await bot.send_message(chat_id=chat_id, text="❌ Could not transcribe voice message. Set AZURE_SPEECH_KEY for voice support.")
            return
        await bot.send_message(chat_id=chat_id, text=f"🎤 _{text}_", parse_mode="Markdown")
    elif message.text:
        text = message.text.strip()
    else:
        return

    if not text:
        return

    # Parse agent command: /agent stock-analysis-pro AAPL at $242.50
    agent_name = None
    prompt = text

    if text.startswith(AGENT_CMD_PREFIX):
        parts = text[len(AGENT_CMD_PREFIX):].strip().split(" ", 1)
        agent_name = parts[0]
        prompt = parts[1] if len(parts) > 1 else ""

    elif text.startswith("/") and not text.startswith("/start"):
        # Parse /agent-name prompt (same as web UI slash commands)
        parts = text[1:].split(" ", 1)
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

    # Stream output from Copilot — send incremental messages
    try:
        buffer = ""
        last_send_time = asyncio.get_event_loop().time()
        send_interval = 3  # seconds between incremental sends
        sent_any = False

        if agent_name:
            stream = copilot.run_with_agent(prompt, agent_name)
        else:
            stream = copilot.run_gh_copilot(prompt)

        logger.warning("Starting copilot stream for chat %s", chat_id)

        async for chunk in stream:
            buffer += chunk
            now = asyncio.get_event_loop().time()
            # Send buffered text periodically to show progress
            if now - last_send_time >= send_interval and buffer.strip():
                for msg_chunk in _split_message(_clean_output(buffer)):
                    await bot.send_message(chat_id=chat_id, text=msg_chunk)
                    sent_any = True
                buffer = ""
                last_send_time = now

        logger.warning("Copilot stream ended for chat %s, buffer remaining: %d chars", chat_id, len(buffer))

        # Send any remaining text
        if buffer.strip():
            cleaned = _clean_output(buffer)
            if cleaned.strip():
                for msg_chunk in _split_message(cleaned):
                    await bot.send_message(chat_id=chat_id, text=msg_chunk)
                    sent_any = True

        if not sent_any:
            await bot.send_message(chat_id=chat_id, text="_(No response from Copilot)_")

    except Exception as e:
        logger.exception("Copilot execution failed for chat %s", chat_id)
        await bot.send_message(chat_id=chat_id, text=f"❌ Error: {e}")
    finally:
        stop_typing.set()
        typing_task.cancel()

    # Sync any files created by copilot to blob storage
    from app.services import blob_storage
    synced = blob_storage.sync_workspace_to_storage()

    # Delete the "processing" message
    try:
        await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
    except Exception:
        pass

    # Notify about generated files
    if synced > 0:
        try:
            tree = blob_storage.get_file_tree("reports")
            file_names = [n.name for n in tree if not n.is_folder]
            if file_names:
                file_list = "\n".join(f"• {f}" for f in file_names[:10])
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📁 **{synced} file(s) generated:**\n{file_list}\n\nView them in the web app's File Explorer.",
                    parse_mode="Markdown",
                )
        except Exception:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📁 {synced} file(s) were generated. View them in the web app's File Explorer.",
            )


async def _transcribe_voice(bot: Bot, file_id: str) -> str | None:
    """Download a Telegram voice message and transcribe it via Azure Speech-to-Text."""
    if not settings.azure_speech_key:
        logger.warning("AZURE_SPEECH_KEY not set — cannot transcribe voice messages")
        return None

    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()

        url = (
            f"https://{settings.azure_speech_region}.stt.speech.microsoft.com"
            f"/speech/recognition/conversation/cognitiveservices/v1"
            f"?language=en-US"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={
                    "Ocp-Apim-Subscription-Key": settings.azure_speech_key,
                    "Content-Type": "audio/ogg; codecs=opus",
                },
                content=bytes(file_bytes),
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("RecognitionStatus") == "Success":
                return result.get("DisplayText", "").strip()
            logger.warning("Speech recognition status: %s", result.get("RecognitionStatus"))
            return None
    except Exception:
        logger.exception("Voice transcription failed")
        return None


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
