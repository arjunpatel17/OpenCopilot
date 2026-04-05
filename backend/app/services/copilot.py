import asyncio
import json
import logging
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator
from app.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 300  # 5 min max per command

# Read-only tools for plan mode (no edit/shell/write)
PLAN_MODE_TOOLS = ["read", "search", "web"]

# Marker prefix for tool events (not normal text — parsed by callers)
TOOL_EVENT_PREFIX = "\x00TOOL:"


# ========== Process activity log (in-memory, shared across callers) ==========

MAX_LOG_LINES = 500

_activity_log: deque[dict] = deque(maxlen=MAX_LOG_LINES)
_log_subscribers: list[asyncio.Queue] = []
_active_processes: dict[str, dict] = {}  # tracks running processes by ID


def _emit_log(entry: dict) -> None:
    """Add a log entry and notify all subscribers."""
    entry.setdefault("ts", time.time())
    _activity_log.append(entry)
    dead = []
    for q in _log_subscribers:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _log_subscribers.remove(q)


def subscribe_logs() -> asyncio.Queue:
    """Subscribe to live log entries. Returns a Queue that receives new entries."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _log_subscribers.append(q)
    return q


def unsubscribe_logs(q: asyncio.Queue) -> None:
    try:
        _log_subscribers.remove(q)
    except ValueError:
        pass


def get_log_snapshot() -> list[dict]:
    """Return current log buffer."""
    return list(_activity_log)


def get_active_process() -> dict | None:
    """Return info about a currently running process, if any."""
    if _active_processes:
        # Return the most recently started one
        return max(_active_processes.values(), key=lambda p: p.get("started", 0))
    return None


def _find_cli(name: str) -> str | None:
    """Find a CLI tool in PATH."""
    return shutil.which(name)


def ensure_workspace_dirs():
    """Ensure standard workspace directories exist."""
    workspace = Path(settings.workspace_dir)
    (workspace / "projects").mkdir(parents=True, exist_ok=True)
    (workspace / "reports").mkdir(parents=True, exist_ok=True)
    # copilot CLI discovers agents from the git root, so ensure workspace is a git repo
    if not (workspace / ".git").exists():
        import subprocess
        subprocess.run(["git", "init", str(workspace)], capture_output=True)



def _summarize_tool_call(tool_name: str, arguments: dict) -> str:
    """Create a human-readable summary of a tool call from its name and arguments."""
    if tool_name == "report_intent":
        return arguments.get("intent", "Planning")
    if tool_name == "bash" or tool_name == "shell":
        cmd = arguments.get("command", "")
        if cmd:
            # Show first 80 chars of the command
            short = cmd.strip().split("\n")[0][:80]
            return f"$ {short}"
        return "Running shell command"
    if tool_name in ("read", "view"):
        path = arguments.get("path", arguments.get("file", ""))
        if path:
            # Show just the filename/last path component
            short = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"Reading {short}"
        return "Reading file"
    if tool_name == "read_agent":
        return f"Loading agent: {arguments.get('agentName', arguments.get('name', 'unknown'))}"
    if tool_name == "write":
        path = arguments.get("path", arguments.get("file", ""))
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Writing {short}" if short else "Writing file"
    if tool_name == "edit":
        path = arguments.get("path", arguments.get("file", ""))
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Editing {short}" if short else "Editing file"
    if tool_name == "search" or tool_name == "grep":
        query = arguments.get("query", arguments.get("pattern", ""))
        if query:
            return f"Searching: {query[:60]}"
        return "Searching codebase"
    if tool_name in ("web", "fetch", "web_fetch"):
        url = arguments.get("url", "")
        if url:
            return f"Fetching: {url[:80]}"
        return "Browsing web"
    if tool_name == "web_search":
        query = arguments.get("query", arguments.get("search_query", ""))
        if query:
            return f"Web search: {query[:60]}"
        return "Searching the web"
    if tool_name == "ask_user":
        return "Asking for input"
    # For any MCP or unknown tool, show name + first argument value
    if arguments:
        first_val = str(next(iter(arguments.values()), ""))[:60]
        return f"{tool_name}: {first_val}" if first_val else tool_name
    return tool_name


def _prepend_history(prompt: str, history: str | None) -> str:
    """If history is provided, prepend it as conversation context."""
    if not history:
        return prompt
    return (
        "Previous conversation for context (do NOT repeat previous answers, "
        "just use this to understand what the user is referring to):\n\n"
        f"{history}\n\n---\nCurrent request: {prompt}"
    )


async def run_code_chat(prompt: str, agent_name: str | None = None, *, model_name: str | None = None, history: str | None = None) -> AsyncIterator[str]:
    """Run a prompt via the standalone `copilot` CLI with structured JSONL output."""
    prompt = _prepend_history(prompt, history)
    copilot_path = _find_cli("copilot")
    if not copilot_path:
        yield "[Error]: 'copilot' CLI not found in PATH. Install with: npm install -g @github/copilot"
        return

    model = model_name or settings.copilot_model
    args = [copilot_path, "--output-format", "json", "--allow-all"]
    if agent_name:
        args.extend(["--agent", agent_name])
    if model:
        args.extend(["--model", model])
    args.extend(["-p", prompt])

    # Truncate prompt for display
    display_prompt = prompt[:120] + ("..." if len(prompt) > 120 else "")
    proc_id = f"code-{id(args)}-{time.time()}"
    _active_processes[proc_id] = {"prompt": display_prompt, "model": model, "started": time.time(), "status": "running"}
    _emit_log({"type": "process_start", "prompt": display_prompt, "model": model})
    logger.info("Copilot process starting", extra={"agent_name": agent_name, "model": model})

    try:
        async for chunk in _run_jsonl_stream(args):
            yield chunk
    finally:
        duration_ms = round((time.time() - _active_processes.get(proc_id, {}).get("started", time.time())) * 1000)
        _active_processes.pop(proc_id, None)
        _emit_log({"type": "process_end"})
        logger.info("Copilot process finished", extra={"agent_name": agent_name, "model": model, "duration_ms": duration_ms})


# ========== Plan mode (read-only, no edits/shell) ==========

async def run_plan_mode(prompt: str, agent_name: str | None = None, *, model_name: str | None = None, history: str | None = None) -> AsyncIterator[str]:
    """Run copilot in plan mode — read-only tools, no edits or shell commands."""
    prompt = _prepend_history(prompt, history)
    combined = (
        "You are in PLAN MODE. Analyze, research, and create a detailed plan. "
        f"Do NOT edit files or run shell commands.\n\nUser request: {prompt}"
    )

    copilot_path = _find_cli("copilot")
    if not copilot_path:
        yield "[Error]: 'copilot' CLI not found in PATH. Install with: npm install -g @github/copilot"
        return

    model = model_name or settings.copilot_model
    args = [copilot_path, "--output-format", "json", "--allow-all"]
    for tool in PLAN_MODE_TOOLS:
        args.extend(["--available-tools", tool])
    if agent_name:
        args.extend(["--agent", agent_name])
    if model:
        args.extend(["--model", model])
    args.extend(["-p", combined])

    # Track process for logs
    display_prompt = prompt[:120] + ("..." if len(prompt) > 120 else "")
    proc_id = f"plan-{id(args)}-{time.time()}"
    _active_processes[proc_id] = {"prompt": display_prompt, "model": model, "started": time.time(), "status": "running"}
    _emit_log({"type": "process_start", "prompt": display_prompt, "model": model})
    logger.info("Copilot plan process starting", extra={"agent_name": agent_name, "model": model})

    try:
        async for chunk in _run_jsonl_stream(args):
            yield chunk
    finally:
        duration_ms = round((time.time() - _active_processes.get(proc_id, {}).get("started", time.time())) * 1000)
        _active_processes.pop(proc_id, None)
        _emit_log({"type": "process_end"})
        logger.info("Copilot plan process finished", extra={"agent_name": agent_name, "model": model, "duration_ms": duration_ms})


# ========== Shared JSONL stream reader ==========

async def _run_jsonl_stream(args: list[str]) -> AsyncIterator[str]:
    """Run a copilot CLI process that outputs JSONL and yield text deltas."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=settings.workspace_dir,
        )
    except FileNotFoundError:
        yield f"[Error]: '{args[0]}' not found in PATH."
        return

    assert process.stdout is not None
    assert process.stderr is not None

    try:
        buffer = ""
        no_output_cycles = 0
        max_no_output = 60
        got_any_delta = False

        while True:
            try:
                chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=15)
            except asyncio.TimeoutError:
                if process.returncode is not None:
                    break
                no_output_cycles += 1
                if no_output_cycles >= max_no_output:
                    process.kill()
                    if got_any_delta:
                        break
                    yield "\n[Error]: Command timed out."
                    return
                continue

            if not chunk:
                break

            no_output_cycles = 0
            buffer += chunk.decode("utf-8", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "session.error":
                    error_msg = event.get("data", {}).get("message", "Unknown error")
                    logger.error("Copilot session error: %s", error_msg)
                    yield f"\n[Error]: {error_msg}"
                    process.kill()
                    return

                if event_type == "assistant.message_delta":
                    delta = event.get("data", {}).get("deltaContent", "")
                    if delta:
                        got_any_delta = True
                        _emit_log({"type": "text_delta", "content": delta})
                        yield delta

                elif event_type == "tool.execution_start":
                    tool_name = event.get("data", {}).get("toolName", "")
                    if tool_name:
                        desc = _summarize_tool_call(tool_name, event.get("data", {}).get("arguments", {}))
                        _emit_log({"type": "tool_start", "tool": tool_name, "description": desc})
                        yield f"{TOOL_EVENT_PREFIX}{tool_name}|{desc}\n"

                elif event_type == "assistant.turn_start":
                    turn_id = event.get("data", {}).get("turnId", "0")
                    if turn_id != "0":
                        yield "\n---\n"

                elif event_type == "result":
                    process.kill()
                    return

        if not got_any_delta:
            stderr_data = await process.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            if stderr_text:
                yield f"[Error]: {stderr_text}"
            else:
                yield "[Error]: No response received from Copilot CLI."

    except Exception as e:
        yield f"[Error]: {e}"
    finally:
        if process.returncode is None:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except Exception:
            pass


# ========== CLI introspection helpers ==========

async def get_cli_version() -> str:
    """Get copilot CLI version string."""
    copilot_path = _find_cli("copilot")
    if not copilot_path:
        return "'copilot' CLI not found in PATH"

    try:
        proc = await asyncio.create_subprocess_exec(
            copilot_path, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return stdout.decode("utf-8", errors="replace").strip() or "Unknown version"
    except Exception as e:
        return f"Error getting version: {e}"


def get_mcp_servers() -> list[dict]:
    """Read configured MCP servers from copilot config files."""
    servers = []

    # Check ~/.copilot/mcp-config.json (global)
    global_config = Path.home() / ".copilot" / "mcp-config.json"
    if global_config.exists():
        try:
            data = json.loads(global_config.read_text(encoding="utf-8"))
            for name, cfg in (data.get("mcpServers") or data.get("servers") or {}).items():
                servers.append({
                    "name": name,
                    "source": "global (~/.copilot/mcp-config.json)",
                    "command": cfg.get("command", ""),
                    "args": cfg.get("args", []),
                    "type": cfg.get("type", "stdio"),
                })
        except Exception:
            pass

    # Check workspace .copilot/mcp-config.json
    workspace_config = Path(settings.workspace_dir) / ".copilot" / "mcp-config.json"
    if workspace_config.exists():
        try:
            data = json.loads(workspace_config.read_text(encoding="utf-8"))
            for name, cfg in (data.get("mcpServers") or data.get("servers") or {}).items():
                servers.append({
                    "name": name,
                    "source": "workspace (.copilot/mcp-config.json)",
                    "command": cfg.get("command", ""),
                    "args": cfg.get("args", []),
                    "type": cfg.get("type", "stdio"),
                })
        except Exception:
            pass

    # Check .vscode/mcp.json (VS Code style)
    vscode_mcp = Path(settings.workspace_dir) / ".vscode" / "mcp.json"
    if vscode_mcp.exists():
        try:
            data = json.loads(vscode_mcp.read_text(encoding="utf-8"))
            for name, cfg in (data.get("mcpServers") or data.get("servers") or {}).items():
                servers.append({
                    "name": name,
                    "source": "vscode (.vscode/mcp.json)",
                    "command": cfg.get("command", ""),
                    "args": cfg.get("args", []),
                    "type": cfg.get("type", "stdio"),
                })
        except Exception:
            pass

    # Built-in: GitHub MCP server is always available
    servers.append({
        "name": "github-mcp-server",
        "source": "built-in",
        "command": "(bundled with Copilot CLI)",
        "args": [],
        "type": "built-in",
    })

    return servers


# ========== Model discovery ==========

# Default fallback models if CLI discovery fails
_DEFAULT_MODELS = [
    {"group": "Claude", "models": [
        {"id": "claude-opus-4.6-1m", "name": "Claude Opus 4.6 (1M context)"},
        {"id": "claude-opus-4.6", "name": "Claude Opus 4.6"},
        {"id": "claude-opus-4.5", "name": "Claude Opus 4.5"},
        {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"},
        {"id": "claude-sonnet-4", "name": "Claude Sonnet 4"},
        {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5"},
    ]},
    {"group": "GPT", "models": [
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.3-codex", "name": "GPT-5.3-Codex"},
        {"id": "gpt-5.2-codex", "name": "GPT-5.2-Codex"},
        {"id": "gpt-5.2", "name": "GPT-5.2"},
        {"id": "gpt-5.1-codex-max", "name": "GPT-5.1-Codex-Max"},
        {"id": "gpt-5.1-codex", "name": "GPT-5.1-Codex"},
        {"id": "gpt-5.1", "name": "GPT-5.1"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
        {"id": "gpt-5.1-codex-mini", "name": "GPT-5.1-Codex-Mini"},
        {"id": "gpt-5-mini", "name": "GPT-5 mini"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
    ]},
    {"group": "Gemini", "models": [
        {"id": "gemini-3-pro-preview", "name": "Gemini 3 Pro (Preview)"},
    ]},
]

# Cached model list — populated at startup
_cached_models: list[dict] | None = None


def get_models() -> list[dict]:
    """Return the cached model list (grouped)."""
    return _cached_models or _DEFAULT_MODELS


async def discover_models() -> list[dict]:
    """Query copilot CLI for available models and cache the result."""
    global _cached_models

    copilot_path = _find_cli("copilot")
    if not copilot_path:
        logger.warning("'copilot' CLI not found, using default model list")
        _cached_models = _DEFAULT_MODELS
        return _cached_models

    # Ask copilot to list models via a quick prompt
    try:
        raw_output = []
        async for chunk in run_code_chat(
            "List every available model ID. Output ONLY a JSON array of objects "
            "with 'id' and 'name' fields, no markdown, no explanation. Example: "
            '[{"id":"claude-sonnet-4.6","name":"Claude Sonnet 4.6"}]'
        ):
            if not chunk.strip().startswith(TOOL_EVENT_PREFIX):
                raw_output.append(chunk)

        text = "".join(raw_output).strip()
        # Extract JSON array from response
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            models_list = json.loads(text[start:end + 1])
            if isinstance(models_list, list) and len(models_list) > 3:
                # Group by provider prefix
                groups: dict[str, list[dict]] = {}
                for m in models_list:
                    mid = m.get("id", "")
                    mname = m.get("name", mid)
                    if mid.startswith("claude"):
                        group = "Claude"
                    elif mid.startswith("gpt"):
                        group = "GPT"
                    elif mid.startswith("gemini"):
                        group = "Gemini"
                    elif mid.startswith("o"):
                        group = "OpenAI Reasoning"
                    else:
                        group = "Other"
                    groups.setdefault(group, []).append({"id": mid, "name": mname})

                _cached_models = [{"group": g, "models": ms} for g, ms in groups.items()]
                logger.info("Discovered %d models from CLI", len(models_list))
                return _cached_models

    except Exception:
        logger.exception("Model discovery from CLI failed, using defaults")

    _cached_models = _DEFAULT_MODELS
    return _cached_models


async def run_copilot_sync(prompt: str, agent_name: str | None = None, *, model_name: str | None = None) -> str:
    """Run a Copilot command and return the full output."""
    chunks = []
    async for chunk in run_code_chat(prompt, agent_name, model_name=model_name):
        chunks.append(chunk)
    return "".join(chunks)
