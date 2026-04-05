import asyncio
import logging
import re
import httpx
from collections import deque
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

# ========== Per-chat concurrency locks ==========

_chat_locks: dict[int, asyncio.Lock] = {}


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a Telegram chat.

    Ensures only one copilot invocation runs per chat at a time.
    """
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# ========== Chat history (in-memory, per Telegram chat_id) ==========

MAX_HISTORY_TURNS = 10           # max user+assistant pairs to keep
MAX_HISTORY_CHARS = 4000         # hard cap on total history text length

# chat_id -> deque of (role, text) tuples
_chat_histories: dict[int, deque] = {}


def _get_history(chat_id: int) -> deque:
    if chat_id not in _chat_histories:
        _chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_TURNS * 2)
    return _chat_histories[chat_id]


def _record_message(chat_id: int, role: str, text: str) -> None:
    """Append a message to the history for a Telegram chat."""
    _get_history(chat_id).append((role, text))


def _clear_history(chat_id: int) -> None:
    _chat_histories.pop(chat_id, None)


def _format_history(chat_id: int) -> str:
    """Build a conversation transcript from recent history."""
    history = _get_history(chat_id)
    if not history:
        return ""
    lines: list[str] = []
    total = 0
    # Walk backwards to respect the char cap, then reverse
    selected: list[tuple[str, str]] = []
    for role, text in reversed(history):
        entry_len = len(text) + 20  # role label overhead
        if total + entry_len > MAX_HISTORY_CHARS:
            break
        selected.append((role, text))
        total += entry_len
    selected.reverse()
    for role, text in selected:
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


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
    "*Email Reports:*\n"
    "Add `\\-\\-email <addr>` to any agent/plan command to email the full report:\n"
    "`/agent stock\\-analysis AAPL \\-\\-email me@co\\.com`\n\n"
    "*Scheduled Jobs:*\n"
    "`/cron <schedule> <agent> <prompt> \\-\\-email <addr>` — Schedule a recurring agent run\n"
    "`/cron \.\.\. \\-\\-time HH:MM` — Set a specific run time \(UTC\)\n"
    "`/crons` — List your scheduled jobs\n"
    "`/uncron <id>` — Delete a scheduled job\n"
    "Schedules: `every 1h`, `every 6h`, `daily`, `weekly`, `weekdays`\n\n"
    "*Introspection:*\n"
    "`/agents` — List available agents\n"
    "`/skills` — List available skills\n"
    "`/models` — List available AI models\n"
    "`/mcps` — List MCP servers\n"
    "`/files` — List workspace files\n"
    "`/version` — Show CLI version\n"
    "`/clear` — Clear conversation history\n"
    "`/help` — Show this help\n\n"
    "*CLI pass\\-through:*\n"
    "`/explain <prompt>` — Explain code or concepts\n"
    "`/suggest <prompt>` — Suggest a shell command\n\n"
    "*Examples:*\n"
    "• Hello, help me write a Python script\n"
    "• /agent stock\\-analysis\\-pro AAPL at \\$242\\.50\n"
    "• /plan Analyze the codebase and propose refactoring\n"
    "• /cron daily stock\\-analysis AAPL \\-\\-email me@co\\.com\n"
    "• /cron daily stock\\-analysis AAPL \\-\\-email me@co\\.com \\-\\-time 08:00\n"
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
    text = "\n".join(lines)
    for chunk in _split_message(text):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")


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
    text = "\n".join(lines)
    for chunk in _split_message(text):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")


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

    # Derive app base URL from webhook
    app_base_url = ""
    try:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            app_base_url = webhook_info.url.replace("/api/telegram/webhook", "")
    except Exception:
        pass

    lines = ["📁 *Workspace Files:*\n"]

    def _render_tree(nodes, prefix="", depth=0):
        for node in nodes:
            indent = "  " * depth
            path = f"{prefix}/{node.name}" if prefix else node.name
            if node.is_folder:
                lines.append(f"{indent}📁 {node.name}")
                if node.children:
                    _render_tree(node.children, path, depth + 1)
            else:
                icon = "📄"
                if app_base_url:
                    lines.append(f"{indent}{icon} [{node.name}]({app_base_url}/api/files/content/{path})")
                else:
                    lines.append(f"{indent}{icon} {node.name}")

    _render_tree(tree)
    if app_base_url:
        lines.append(f"\n[Open File Explorer]({app_base_url})")
    text = "\n".join(lines)
    for chunk in _split_message(text):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown", disable_web_page_preview=True)


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


# ========== Cron job commands ==========

EMAIL_FLAG_RE = re.compile(r'--email\s+(\S+)')
TIME_FLAG_RE = re.compile(r'--time\s+(\d{1,2}:\d{2})')

SCHEDULE_KEYWORDS: dict[str, str] = {
    "every": "",       # "every 1h", "every 6h"
    "daily": "daily",
    "weekly": "weekly",
    "weekdays": "weekdays",
}


def _parse_cron_command(text: str) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """Parse /cron <schedule> <agent> <prompt> --email <addr> [--time HH:MM].

    Returns (schedule, agent_name, prompt, email, run_at, error).
    """
    from app.services.cron_store import ALL_SCHEDULES

    # Normalize em/en dashes to double hyphens (Telegram clients often auto-convert -- to —)
    text = text.replace("—", "--").replace("–", "--")

    # Extract --email flag
    email_match = EMAIL_FLAG_RE.search(text)
    if not email_match:
        return None, None, None, None, None, "Missing --email flag. Usage: /cron <schedule> <agent> <prompt> --email user@example.com"
    email = email_match.group(1)
    text = EMAIL_FLAG_RE.sub("", text).strip()

    # Extract --time flag (optional)
    run_at = None
    time_match = TIME_FLAG_RE.search(text)
    if time_match:
        run_at = time_match.group(1)
        # Validate HH:MM format
        try:
            h, m = map(int, run_at.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return None, None, None, None, None, f"Invalid time: {run_at}. Use HH:MM in 24h UTC format (e.g., 08:00, 14:30)."
            run_at = f"{h:02d}:{m:02d}"  # Normalize
        except ValueError:
            return None, None, None, None, None, f"Invalid time: {run_at}. Use HH:MM format."
        text = TIME_FLAG_RE.sub("", text).strip()

    # Remove /cron prefix
    rest = text[len("/cron"):].strip()
    if not rest:
        return None, None, None, None, "Missing schedule. Usage: /cron <schedule> <agent> <prompt> --email user@example.com"

    # Parse schedule (may be one or two words like "every 6h")
    words = rest.split()
    schedule = None
    consumed = 0

    if words[0] == "every" and len(words) > 1:
        candidate = f"every {words[1]}"
        if candidate in ALL_SCHEDULES:
            schedule = candidate
            consumed = 2
    if schedule is None and words[0] in ALL_SCHEDULES:
        schedule = words[0]
        consumed = 1

    if schedule is None:
        presets = ", ".join(f"`{s}`" for s in sorted(ALL_SCHEDULES))
        return None, None, None, None, None, f"Unknown schedule `{words[0]}`. Available: {presets}"

    remaining = words[consumed:]
    if len(remaining) < 2:
        return None, None, None, None, None, "Missing agent name and/or prompt."

    agent_name = remaining[0]
    prompt = " ".join(remaining[1:])

    # Basic email validation
    if "@" not in email or "." not in email:
        return None, None, None, None, None, f"Invalid email address: {email}"

    return schedule, agent_name, prompt, email, run_at, None


async def _handle_cmd_cron(bot: Bot, chat_id: int, text: str, model_name: str | None = None) -> None:
    """Handle /cron command — create a scheduled job."""
    from app.services import cron_store

    schedule, agent_name, prompt, email, run_at, error = _parse_cron_command(text)
    if error:
        await bot.send_message(chat_id=chat_id, text=f"⚠️ {error}")
        return

    job = cron_store.add_job(
        chat_id=chat_id,
        agent_name=agent_name,
        prompt=prompt,
        schedule=schedule,
        email=email,
        model_name=model_name,
        run_at=run_at,
    )

    time_info = f"\n• Time: `{job.run_at}` UTC" if job.run_at else ""
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Cron job created (ID: `{job.id}`)\n\n"
            f"• Agent: `{job.agent_name}`\n"
            f"• Schedule: `{job.schedule}`{time_info}\n"
            f"• Email: {job.email}\n"
            f"• Prompt: {job.prompt[:100]}{'...' if len(job.prompt) > 100 else ''}\n\n"
            f"Use /crons to list jobs, /uncron {job.id} to delete."
        ),
        parse_mode="Markdown",
    )


async def _handle_cmd_crons(bot: Bot, chat_id: int) -> None:
    """Handle /crons command — list scheduled jobs for this chat."""
    from app.services import cron_store
    import datetime

    jobs = cron_store.list_jobs(chat_id)
    if not jobs:
        await bot.send_message(chat_id=chat_id, text="No scheduled jobs. Create one with /cron.")
        return

    lines = ["📋 *Your Scheduled Jobs:*\n"]
    for j in jobs:
        last = "never"
        if j.last_run:
            dt = datetime.datetime.fromtimestamp(j.last_run, tz=datetime.timezone.utc)
            last = dt.strftime("%Y-%m-%d %H:%M UTC")
        status = "✅" if j.enabled else "⏸"
        lines.append(
            f"{status} `{j.id}` — `{j.agent_name}` ({j.schedule})\n"
            f"   📧 {j.email} | Last run: {last}\n"
            f"   Prompt: {j.prompt[:60]}{'...' if len(j.prompt) > 60 else ''}"
        )

    lines.append(f"\nDelete with: /uncron <id>")
    await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


async def _handle_cmd_uncron(bot: Bot, chat_id: int, text: str) -> None:
    """Handle /uncron <id> — delete a scheduled job."""
    from app.services import cron_store

    parts = text.split()
    if len(parts) < 2:
        await bot.send_message(chat_id=chat_id, text="Usage: /uncron <job_id>\nUse /crons to see your job IDs.")
        return

    job_id = parts[1]
    removed = cron_store.remove_job(job_id, chat_id)
    if removed:
        await bot.send_message(chat_id=chat_id, text=f"✅ Cron job `{job_id}` deleted.", parse_mode="Markdown")
    else:
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Job `{job_id}` not found. Use /crons to see your jobs.", parse_mode="Markdown")


# ========== Main message handler ==========

async def handle_telegram_message(update_data: dict) -> None:
    """Process an incoming Telegram update from the webhook."""
    try:
        await _handle_telegram_message_inner(update_data)
    except Exception:
        logger.exception("Unhandled error in telegram handler",
                         extra={"chat_id": update_data.get("message", {}).get("chat", {}).get("id")})


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

    # Acquire per-chat lock so requests are processed one at a time
    lock = _get_chat_lock(chat_id)
    if lock.locked():
        bot = Bot(settings.telegram_bot_token)
        await bot.send_message(chat_id=chat_id, text="⏳ A command is already running. Your request is queued and will start when it finishes.")

    async with lock:
        await _handle_telegram_message_locked(update_data, chat_id)


async def _handle_telegram_message_locked(update_data: dict, chat_id: int) -> None:
    update = Update.de_json(update_data, Bot(settings.telegram_bot_token))

    if not update or not update.message:
        return

    message = update.message
    username = message.from_user.username if message.from_user else None

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

    # Normalize em/en dashes to double hyphens (Telegram clients often auto-convert -- to —)
    text = text.replace("—", "--").replace("–", "--")

    # ---- Dispatch /cron early (before global --email extraction, since cron parses its own flags) ----
    cmd = text.split()[0].lower() if text.startswith("/") else None

    if cmd == "/cron":
        # Extract --model flag if present
        model_name = None
        model_match = MODEL_FLAG_RE.search(text)
        if model_match:
            model_name = model_match.group(1)
        await _handle_cmd_cron(bot, chat_id, text, model_name=model_name)
        return

    # ---- Extract --email flag if present ----
    email_addr = None
    email_match = EMAIL_FLAG_RE.search(text)
    if email_match:
        email_addr = email_match.group(1)
        text = EMAIL_FLAG_RE.sub('', text).strip()

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
    if cmd == "/clear":
        _clear_history(chat_id)
        await bot.send_message(chat_id=chat_id, text="🗑 Chat history cleared.")
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

    # Cron job commands (note: /cron is dispatched earlier, before --email extraction)
    if cmd == "/crons":
        await _handle_cmd_crons(bot, chat_id)
        return
    if cmd == "/uncron":
        await _handle_cmd_uncron(bot, chat_id, text)
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

    # Keep typing indicator alive while processing
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_send_typing_loop(bot, chat_id, stop_typing))

    # Ensure workspace directories exist
    copilot.ensure_workspace_dirs()

    # Snapshot existing workspace files before the run
    existing_files = _snapshot_workspace_files()

    full_response = ""

    # Stream output from Copilot — edit a single message in-place
    try:
        full_response_parts = []  # accumulate all text for history
        last_edit_time = asyncio.get_event_loop().time()
        edit_interval = 2  # seconds between edits (avoid Telegram rate limits)

        # The response message we'll keep editing (created on first text)
        response_msg = None
        response_text = ""       # full text of current response message
        TG_MSG_LIMIT = 4096
        overflow_msgs = []       # any prior messages that overflowed

        # Build conversation history prefix
        history_text = _format_history(chat_id)

        # Record user message in history
        _record_message(chat_id, "user", prompt)

        if mode == "plan":
            stream = copilot.run_plan_mode(prompt, agent_name, model_name=model_name, history=history_text)
        else:
            stream = copilot.run_code_chat(prompt, agent_name, model_name=model_name, history=history_text)

        logger.warning("Starting copilot stream (%s mode) for chat %s", mode, chat_id,
                       extra={"chat_id": chat_id, "agent_name": agent_name, "model": model_name})

        async for chunk in stream:
            # Convert tool event markers into readable lines
            if chunk.strip().startswith(TOOL_EVENT_PREFIX):
                marker = chunk.strip()[len(TOOL_EVENT_PREFIX):]
                parts = marker.split("|", 1)
                desc = parts[1] if len(parts) > 1 else parts[0]
                tool_line = f"⚡ {desc}\n"
                full_response_parts.append(tool_line)
                response_text += tool_line
                # fall through to the edit logic below
            else:
                # Skip bare turn separators
                if chunk.strip() == '---':
                    continue

                full_response_parts.append(chunk)
                response_text += chunk

            # If we'd exceed the Telegram limit, finalize current message
            # and start a new one
            if len(response_text) > TG_MSG_LIMIT - 100:
                cleaned = _clean_output(response_text[:TG_MSG_LIMIT])
                if response_msg and cleaned.strip():
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=response_msg.message_id,
                            text=cleaned[:TG_MSG_LIMIT],
                        )
                    except Exception:
                        pass
                    overflow_msgs.append(response_msg)
                # Start fresh message for the overflow
                response_msg = None
                response_text = response_text[TG_MSG_LIMIT:]

            now = asyncio.get_event_loop().time()
            if now - last_edit_time >= edit_interval and response_text.strip():
                cleaned = _clean_output(response_text)
                if cleaned.strip():
                    if response_msg is None:
                        # Create the first response message
                        response_msg = await bot.send_message(
                            chat_id=chat_id, text=cleaned[:TG_MSG_LIMIT]
                        )
                    else:
                        try:
                            await bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=response_msg.message_id,
                                text=cleaned[:TG_MSG_LIMIT],
                            )
                        except Exception:
                            pass  # edit fails if text hasn't changed
                    last_edit_time = now

        logger.warning("Copilot stream ended for chat %s", chat_id,
                       extra={"chat_id": chat_id})

        # Final edit with complete remaining text
        if response_text.strip():
            cleaned = _clean_output(response_text)
            if cleaned.strip():
                for msg_chunk in _split_message(cleaned):
                    if response_msg is None:
                        response_msg = await bot.send_message(
                            chat_id=chat_id, text=msg_chunk
                        )
                    else:
                        try:
                            await bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=response_msg.message_id,
                                text=msg_chunk,
                            )
                        except Exception:
                            pass
                        # If there are more chunks after this, we need new messages
                        response_msg = None

        if response_msg is None and not overflow_msgs:
            await bot.send_message(chat_id=chat_id, text="_(No response from Copilot)_")

        # Record assistant response in history
        full_response = _clean_output("".join(full_response_parts)).strip()
        if full_response:
            _record_message(chat_id, "assistant", full_response[:2000])

    except Exception as e:
        logger.exception("Copilot execution failed for chat %s", chat_id,
                         extra={"chat_id": chat_id, "agent_name": agent_name})
        await bot.send_message(chat_id=chat_id, text=f"❌ Error: {e}")
    finally:
        stop_typing.set()
        typing_task.cancel()

    # Sync any files created by copilot to blob storage
    from app.services import blob_storage
    blob_storage.sync_workspace_to_storage()

    # Determine which files are new (not in the pre-run snapshot)
    current_files = _snapshot_workspace_files()
    new_files = sorted(current_files - existing_files)

    # Send email report if --email was specified
    if email_addr:
        await _send_email_report(
            bot, chat_id, email_addr, agent_name, prompt, full_response, new_files
        )

    # Notify about generated files
    if new_files:
        # Derive app base URL from webhook info
        app_base_url = ""
        try:
            webhook_info = await bot.get_webhook_info()
            if webhook_info.url:
                # Strip /api/telegram/webhook to get base URL
                app_base_url = webhook_info.url.replace("/api/telegram/webhook", "")
        except Exception:
            pass

        try:
            # Collect files from all output directories
            generated = new_files
            if generated:
                display = generated[:15]
                if app_base_url:
                    file_list = "\n".join(
                        f"• [{f}]({app_base_url}/api/files/content/{f})" for f in display
                    )
                else:
                    file_list = "\n".join(f"• {f}" for f in display)
                extra = f"\n  _...and {len(generated) - 15} more_" if len(generated) > 15 else ""
                view_link = f"\n\n[Open File Explorer]({app_base_url})" if app_base_url else "\n\nView in the web app's File Explorer."
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📁 *{len(generated)} file(s) generated:*\n{file_list}{extra}{view_link}",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                view_link = f" [Open File Explorer]({app_base_url})" if app_base_url else ""
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📁 {len(new_files)} file(s) were generated.{view_link}",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
        except Exception:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📁 {len(new_files)} file(s) were generated.",
            )


async def _send_email_report(
    bot: Bot,
    chat_id: int,
    email: str,
    agent_name: str | None,
    prompt: str,
    output: str,
    new_files: list[str],
) -> None:
    """Build and send an email report for a Telegram agent run, similar to cron job emails."""
    from app.services import email_service

    workspace = Path(settings.workspace_dir)
    label = agent_name or "chat"

    subject = f"[OpenCopilot] {label} — Telegram report"
    parts = [
        f"Agent run '{label}' completed via Telegram.\n",
        f"Prompt: {prompt}\n",
        f"{'=' * 60}\n",
        output,
    ]

    # Include content of any newly generated files
    if new_files:
        generated_files: dict[str, str] = {}
        for rel_path in new_files:
            fp = workspace / rel_path
            try:
                generated_files[rel_path] = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                generated_files[rel_path] = "(binary file)"

        parts.append(f"\n\n{'=' * 60}")
        parts.append(f"\nGENERATED FILES ({len(generated_files)}):\n")
        for path, content in generated_files.items():
            parts.append(f"\n{'─' * 40}")
            parts.append(f"📄 {path}")
            parts.append(f"{'─' * 40}\n")
            parts.append(content)

    body = "\n".join(parts)
    sent = email_service.send_result_email(email, subject, body)

    if sent:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📧 Report emailed to {email}.",
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Failed to email report to {email}. Check server logs.",
        )


def _snapshot_workspace_files() -> set[str]:
    """Return a set of relative file paths currently in the workspace."""
    workspace = Path(settings.workspace_dir)
    if not workspace.exists():
        return set()
    files = set()
    for fp in workspace.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(workspace)
        parts = rel.parts
        if any((p.startswith(".") and p != ".github") or p == "__pycache__" for p in parts):
            continue
        if parts[0] == "sessions":
            continue
        files.add(str(rel))
    return files


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
