import asyncio
import logging
import re
import httpx
from pathlib import Path
from telegram import Update, Bot
from telegram.constants import ChatAction
from app.services import copilot
from app.services.copilot import TOOL_EVENT_PREFIX
from app.config import settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

AGENT_CMD_PREFIX = "/agent "
MODEL_FLAG_RE = re.compile(r'--model\s+(\S+)')


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


# ========== Command handlers ==========

HELP_TEXT = (
    "⚡ *OpenCopilot Bot*\n\n"
    "Send any message to chat with GitHub Copilot\\.\n\n"
    "*Modes:*\n"
    "`/agent <name> <prompt>` — Run an agent \\(full tools\\)\n"
    "`/plan <prompt>` — Plan mode \\(read\\-only, no edits\\)\n"
    "`/plan <agent> <prompt>` — Plan with an agent\n\n"
    "*Introspection:*\n"
    "`/agents` — List available agents\n"
    "`/skills` — List available skills\n"
    "`/models` — List available AI models\n"
    "`/mcps` — List MCP servers\n"
    "`/files` — List workspace files\n"
    "`/version` — Show CLI version\n"
    "`/help` — Show this help\n\n"
    "*CLI pass\\-through:*\n"
    "`/explain <prompt>` — Explain code or concepts\n"
    "`/suggest <prompt>` — Suggest a shell command\n\n"
    "*Examples:*\n"
    "• Hello, help me write a Python script\n"
    "• /agent stock\\-analysis\\-pro AAPL at \\$242\\.50\n"
    "• /plan Analyze the codebase and propose refactoring\n"
    "• /mcps\n"
    "• /explain What does asyncio\\.gather do?\n\n"
    "*Model selection:*\n"
    "Add `\\-\\-model <id>` to any message:\n"
    "• \\-\\-model claude\\-opus\\-4\\.6\\-1m explain quantum computing\n"
    "• /agent stock\\-analysis \\-\\-model gpt\\-5\\.4 AAPL"
)


async def _handle_cmd_help(bot: Bot, chat_id: int) -> None:
    await bot.send_message(chat_id=chat_id, text=HELP_TEXT, parse_mode="MarkdownV2")


async def _handle_cmd_agents(bot: Bot, chat_id: int) -> None:
    from app.services.agent_parser import list_agents
    agents_dir = Path(settings.agents_path)
    agents = list_agents(agents_dir)
    if not agents:
        await bot.send_message(chat_id=chat_id, text="No agents configured. Create agents in the web app or add .agent.md files.")
        return
    lines = ["🤖 *Available Agents:*\n"]
    for a in agents:
        desc = f" — {a.description}" if a.description else ""
        skills_info = f" ({a.skills_count} skills)" if a.skills_count else ""
        lines.append(f"• `{a.name}`{desc}{skills_info}")
    lines.append(f"\nUse: /agent <name> <prompt>")
    await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


async def _handle_cmd_skills(bot: Bot, chat_id: int) -> None:
    skills_dir = Path(settings.skills_path)
    skills = []
    if skills_dir.exists():
        for f in sorted(skills_dir.glob("*.skill.md")):
            try:
                from app.services.agent_parser import parse_markdown_file
                raw = f.read_text(encoding="utf-8")
                fm, _ = parse_markdown_file(raw)
                name = fm.get("name", f.stem.removesuffix(".skill"))
                desc = fm.get("description", "")
                skills.append((name, desc))
            except Exception:
                continue

    if not skills:
        await bot.send_message(chat_id=chat_id, text="No skills configured. Create skills in the web app or add .skill.md files.")
        return
    lines = ["🧩 *Available Skills:*\n"]
    for name, desc in skills:
        desc_text = f" — {desc}" if desc else ""
        lines.append(f"• `{name}`{desc_text}")
    await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


async def _handle_cmd_mcps(bot: Bot, chat_id: int) -> None:
    servers = copilot.get_mcp_servers()
    if not servers:
        await bot.send_message(chat_id=chat_id, text="No MCP servers configured.")
        return
    lines = ["🔌 *MCP Servers:*\n"]
    for s in servers:
        cmd = s.get("command", "")
        source = s.get("source", "")
        type_str = s.get("type", "stdio")
        args = " ".join(s.get("args", []))
        line = f"• *{s['name']}*"
        if source:
            line += f" [{source}]"
        if cmd and cmd != "(bundled with Copilot CLI)":
            line += f"\n  `{cmd} {args}`".rstrip()
        elif type_str == "built-in":
            line += " (built-in)"
        lines.append(line)
    await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


async def _handle_cmd_files(bot: Bot, chat_id: int) -> None:
    from app.services import blob_storage
    tree = blob_storage.get_file_tree("")
    if not tree:
        await bot.send_message(chat_id=chat_id, text="📁 Workspace is empty.")
        return

    lines = ["📁 *Workspace Files:*\n"]

    def _render_tree(nodes, depth=0):
        for node in nodes:
            indent = "  " * depth
            icon = "📁" if node.is_folder else "📄"
            lines.append(f"{indent}{icon} {node.name}")
            if node.is_folder and node.children:
                _render_tree(node.children, depth + 1)

    _render_tree(tree)
    text = "\n".join(lines)
    for chunk in _split_message(text):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")


async def _handle_cmd_version(bot: Bot, chat_id: int) -> None:
    version = await copilot.get_cli_version()
    await bot.send_message(chat_id=chat_id, text=f"⚡ {version}")


async def _handle_cmd_models(bot: Bot, chat_id: int) -> None:
    model_groups = copilot.get_models()
    lines = ["🧠 *Available Models:*\n"]
    for group in model_groups:
        lines.append(f"*{group['group']}:*")
        for m in group["models"]:
            lines.append(f"  • `{m['id']}` — {m['name']}")
        lines.append("")
    lines.append("Use with: /agent <name> --model <id> <prompt>")
    await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


async def _handle_cmd_sync(bot: Bot, chat_id: int) -> None:
    from app.services.github_sync import sync_agents_from_github
    await bot.send_message(chat_id=chat_id, text="🔄 Syncing agents and skills from GitHub...")
    try:
        result = await sync_agents_from_github()
        if result.get("synced"):
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Sync complete from `{result['repo']}`\n\n"
                    f"• {result['agents']} agent(s)\n"
                    f"• {result['skills']} skill(s)"
                ),
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(chat_id=chat_id, text=f"⚠️ {result.get('reason', 'Sync failed')}")
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Sync failed: {e}")


# ========== Main message handler ==========

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
    elif message.photo or (message.document and message.document.mime_type and message.document.mime_type.startswith("image/")):
        # Handle image messages — download and reference in prompt
        image_path = await _save_telegram_image(bot, message)
        if image_path is None:
            await bot.send_message(chat_id=chat_id, text="❌ Could not download the image.")
            return
        caption = message.caption.strip() if message.caption else "Analyze this image"
        text = f"Look at the image file at {image_path} using the view tool. {caption}"
    elif message.text:
        text = message.text.strip()
    else:
        return

    if not text:
        return

    # ---- Extract --model flag if present ----
    model_name = None
    model_match = MODEL_FLAG_RE.search(text)
    if model_match:
        model_name = model_match.group(1)
        text = MODEL_FLAG_RE.sub('', text).strip()

    # ---- Dispatch commands ----

    cmd = text.split()[0].lower() if text.startswith("/") else None

    # Instant commands (no copilot invocation)
    if cmd in ("/start", "/help"):
        await _handle_cmd_help(bot, chat_id)
        return
    if cmd == "/agents":
        await _handle_cmd_agents(bot, chat_id)
        return
    if cmd == "/skills":
        await _handle_cmd_skills(bot, chat_id)
        return
    if cmd == "/mcps":
        await _handle_cmd_mcps(bot, chat_id)
        return
    if cmd == "/files":
        await _handle_cmd_files(bot, chat_id)
        return
    if cmd == "/version":
        await _handle_cmd_version(bot, chat_id)
        return
    if cmd == "/models":
        await _handle_cmd_models(bot, chat_id)
        return

    # Determine mode & parse prompt
    agent_name = None
    prompt = text
    mode = "agent"  # default: full agent mode

    if cmd == "/plan":
        # /plan [agent-name] prompt
        mode = "plan"
        rest = text[len("/plan"):].strip()
        if rest:
            parts = rest.split(" ", 1)
            # Check if first word is an agent name
            from app.services.agent_parser import list_agents
            agents_dir = Path(settings.agents_path)
            agent_names = [a.name for a in list_agents(agents_dir)]
            if parts[0] in agent_names:
                agent_name = parts[0]
                prompt = parts[1] if len(parts) > 1 else ""
            else:
                prompt = rest
        else:
            prompt = ""

    elif cmd == "/explain":
        prompt = text[len("/explain"):].strip()
        # Wrap prompt to request explanation
        if prompt:
            prompt = f"Explain the following:\n\n{prompt}"

    elif cmd == "/suggest":
        prompt = text[len("/suggest"):].strip()
        if prompt:
            prompt = f"Suggest a shell command for the following task. Only output the command and a brief explanation:\n\n{prompt}"

    elif text.startswith(AGENT_CMD_PREFIX):
        parts = text[len(AGENT_CMD_PREFIX):].strip().split(" ", 1)
        agent_name = parts[0]
        prompt = parts[1] if len(parts) > 1 else ""

    elif text.startswith("/") and cmd not in ("/start", "/help"):
        # Parse /agent-name prompt (same as web UI slash commands)
        parts = text[1:].split(" ", 1)
        agent_name = parts[0]
        prompt = parts[1] if len(parts) > 1 else ""

    if not prompt:
        await bot.send_message(chat_id=chat_id, text="Please provide a message or prompt.")
        return

    # Send initial status message
    mode_label = "plan" if mode == "plan" else "agent"
    model_info = f" ({model_name})" if model_name else ""
    if agent_name:
        status_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Running `{agent_name}` in {mode_label} mode{model_info}... This may take a few minutes.",
        )
    else:
        status_label = f"📋 Planning{model_info}..." if mode == "plan" else f"⏳ Processing{model_info}..."
        status_msg = await bot.send_message(chat_id=chat_id, text=status_label)

    # Keep typing indicator alive while processing
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_send_typing_loop(bot, chat_id, stop_typing))

    # Ensure workspace directories exist
    copilot.ensure_workspace_dirs()

    # Stream output from Copilot — send incremental messages
    try:
        buffer = ""
        last_send_time = asyncio.get_event_loop().time()
        send_interval = 3  # seconds between incremental sends
        sent_any = False
        tool_history = []  # track tools used for status updates

        if mode == "plan":
            stream = copilot.run_plan_mode(prompt, agent_name, model_name=model_name)
        elif agent_name:
            stream = copilot.run_with_agent(prompt, agent_name, model_name=model_name)
        else:
            stream = copilot.run_gh_copilot(prompt, model_name=model_name)

        logger.warning("Starting copilot stream (%s mode) for chat %s", mode, chat_id)

        async for chunk in stream:
            # Detect structured tool event markers from copilot stream
            if chunk.strip().startswith(TOOL_EVENT_PREFIX):
                marker = chunk.strip()[len(TOOL_EVENT_PREFIX):]
                parts = marker.split("|", 1)
                tool_name = parts[0]
                tool_desc = parts[1] if len(parts) > 1 else tool_name
                tool_history.append(tool_desc)
                # Update the status message with current tool activity
                status_text = _build_status_text(agent_name, mode, tool_history)
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text=status_text,
                    )
                except Exception:
                    pass  # edit may fail if message hasn't changed
                continue

            # Skip bare turn separators from being buffered
            if chunk.strip() == '---':
                continue

            buffer += chunk
            now = asyncio.get_event_loop().time()
            # Send buffered text periodically to show progress
            if now - last_send_time >= send_interval and buffer.strip():
                cleaned = _clean_output(buffer)
                if cleaned:
                    for msg_chunk in _split_message(cleaned):
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
            # Collect files from all output directories
            generated = []
            for prefix in ("projects", "reports", ""):
                try:
                    tree = blob_storage.get_file_tree(prefix)
                    _collect_files(tree, prefix, generated)
                except Exception:
                    pass
            if generated:
                # Group by top-level folder
                display = generated[:15]
                file_list = "\n".join(f"• {f}" for f in display)
                extra = f"\n  _...and {len(generated) - 15} more_" if len(generated) > 15 else ""
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📁 **{synced} file(s) generated:**\n{file_list}{extra}\n\nView or download in the web app's File Explorer.",
                    parse_mode="Markdown",
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📁 {synced} file(s) were generated. View them in the web app's File Explorer.",
                )
        except Exception:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📁 {synced} file(s) were generated. View them in the web app's File Explorer.",
            )


def _collect_files(nodes, prefix: str, result: list, depth: int = 0):
    """Recursively collect file paths from a tree for display."""
    for node in nodes:
        path = f"{prefix}/{node.name}" if prefix else node.name
        if node.is_folder:
            if node.children:
                _collect_files(node.children, path, result, depth + 1)
        else:
            result.append(path)


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


async def _save_telegram_image(bot: Bot, message) -> str | None:
    """Download an image from a Telegram message and save it to the workspace uploads directory.
    Returns the absolute file path, or None on failure."""
    try:
        if message.photo:
            # Photos come as an array of sizes — take the largest
            photo = message.photo[-1]
            file_id = photo.file_id
            ext = "jpg"
        elif message.document:
            file_id = message.document.file_id
            name = message.document.file_name or "image"
            ext = name.rsplit(".", 1)[-1] if "." in name else "png"
        else:
            return None

        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()

        # Save to workspace/uploads/
        import uuid
        uploads_dir = Path(settings.workspace_dir) / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        filepath = uploads_dir / filename
        filepath.write_bytes(bytes(file_bytes))

        logger.info("Saved Telegram image to %s (%d bytes)", filepath, len(file_bytes))
        return str(filepath)
    except Exception:
        logger.exception("Failed to save Telegram image")
        return None


def _build_status_text(agent_name: str | None, mode: str, tool_history: list[str]) -> str:
    """Build a status message showing current activity with descriptions."""
    mode_label = "📋 Plan" if mode == "plan" else "⚡ Agent"
    header = f"{mode_label} mode"
    if agent_name:
        header += f" — {agent_name}"

    # Show recent activity steps (last 8 to keep message compact)
    recent = tool_history[-8:]
    steps = "\n".join(f"  ✓ {s}" for s in recent[:-1])
    current = recent[-1] if recent else "Starting"
    progress = f"{steps}\n  ⏳ {current}..." if steps else f"  ⏳ {current}..."

    return f"{header}\n\n{progress}"


def _clean_output(text: str) -> str:
    """Remove tool markers, turn separators, and usage stats from output."""
    import re
    lines = text.split("\n")
    cleaned = []
    skip_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Total usage est:") or stripped.startswith("API time spent:"):
            skip_section = True
        if skip_section:
            continue
        # Skip structured tool markers
        if stripped.startswith(TOOL_EVENT_PREFIX):
            continue
        # Skip legacy tool indicator lines (e.g. 🔧 _bash_)
        if re.match(r'^🔧\s*_.*_\s*$', stripped):
            continue
        if stripped == '---':
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


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
