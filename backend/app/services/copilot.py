import asyncio
import json
import logging
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# How long the CLI may produce no stdout at all before we assume it is wedged.
# A single long tool call (large build, slow web fetch) can legitimately go
# quiet for many minutes, so this is deliberately generous.
STDOUT_READ_TIMEOUT = 15
MAX_IDLE_SECONDS = 3600

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


def _safe_kill(process) -> None:
    """Kill a subprocess if still alive; swallow ProcessLookupError race when
    the process has already exited (common after a CLI 'result' event)."""
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except Exception as e:
        logger.debug("safe_kill suppressed %s", e)


def ensure_workspace_dirs():
    """Ensure standard workspace directories exist."""
    workspace = Path(settings.workspace_dir)
    (workspace / "projects").mkdir(parents=True, exist_ok=True)
    (workspace / "reports").mkdir(parents=True, exist_ok=True)
    # copilot CLI discovers agents from the git root, so ensure workspace is a git repo
    if not (workspace / ".git").exists():
        import subprocess
        subprocess.run(["git", "init", str(workspace)], capture_output=True)



def _summarize_tool_call(tool_name: str, arguments) -> str:
    """Create a human-readable summary of a tool call from its name and arguments.

    `arguments` is usually a dict, but some MCP/agent tool events emit it as
    a string (JSON-encoded or freeform) or null. Coerce to a dict-like view
    before any `.get(...)` so a malformed event can't crash the stream reader
    mid-run.
    """
    if not isinstance(arguments, dict):
        # String form: best-effort JSON parse, else show raw string.
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                arguments = parsed if isinstance(parsed, dict) else {}
            except Exception:
                short = arguments.strip().split("\n")[0][:60]
                return f"{tool_name}: {short}" if short else tool_name
        else:
            arguments = {}
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


# Suffix used by the UI to request a model's 1M ("long") context window. The
# Copilot CLI exposes the larger window via `--context long_context` rather than
# a distinct model id, so we strip this suffix and pass the flag instead. Works
# for any base model that supports the long-context tier (e.g. Opus 4.8 / 5.5).
_LONG_CONTEXT_SUFFIX = "-1m"
_LONG_CONTEXT_TIER = "long_context"


def _resolve_model_args(model: str | None) -> list[str]:
    """Turn a (possibly suffixed) model id into CLI `--model`/`--context` args.

    A trailing `-1m` is a UI convenience meaning "use this model's 1M context
    window". It is not a real CLI model id, so we map it to the base model plus
    `--context long_context`.
    """
    if not model:
        return []
    if model.endswith(_LONG_CONTEXT_SUFFIX):
        base = model[: -len(_LONG_CONTEXT_SUFFIX)]
        return ["--model", base, "--context", _LONG_CONTEXT_TIER]
    return ["--model", model]


async def run_code_chat(prompt: str, agent_name: str | None = None, *, model_name: str | None = None, history: str | None = None) -> AsyncIterator[str]:
    """Run a prompt via the standalone `copilot` CLI with structured JSONL output."""
    # Ensure data files are fresh from blob storage before the agent reads them
    try:
        from app.services.blob_storage import restore_data_from_storage
        restore_data_from_storage()
    except Exception:
        logger.debug("Data restore from blob skipped", exc_info=True)

    prompt = _prepend_history(prompt, history)
    copilot_path = _find_cli("copilot")
    if not copilot_path:
        yield "[Error]: 'copilot' CLI not found in PATH. Install with: npm install -g @github/copilot"
        return

    model = model_name or settings.copilot_model
    # `--max-autopilot-continues` defaults to 5 in the copilot CLI; without
    # raising it, long agent runs (e.g. stocks-social-media iterating across
    # several tickers) hit the cap and the CLI emits a clean `result` event
    # mid-task around the 10-minute mark. 200 leaves ample headroom while
    # still preventing a runaway loop.
    args = [
        copilot_path,
        "--output-format", "json",
        "--allow-all",
        "--mode", "autopilot",
        "--max-autopilot-continues", "200",
    ]
    if agent_name:
        args.extend(["--agent", agent_name])
    args.extend(_resolve_model_args(model))
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
    args = [
        copilot_path,
        "--output-format", "json",
        "--allow-all",
        "--mode", "autopilot",
        "--max-autopilot-continues", "200",
    ]
    for tool in PLAN_MODE_TOOLS:
        args.extend(["--available-tools", tool])
    if agent_name:
        args.extend(["--agent", agent_name])
    args.extend(_resolve_model_args(model))
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

    # Termination instrumentation — populated so the `finally` block can log
    # exactly *why* the stream ended (clean CLI result, stdout EOF, app-side
    # idle-kill, session.error, exception). Critical for diagnosing the
    # ~10-min cutoff in long agent runs.
    exit_reason = "unknown"
    delta_count = 0
    tool_count = 0
    last_event_type: str | None = None

    try:
        buffer = ""
        no_output_cycles = 0
        max_no_output = MAX_IDLE_SECONDS // STDOUT_READ_TIMEOUT
        got_any_delta = False

        while True:
            try:
                chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=STDOUT_READ_TIMEOUT)
            except asyncio.TimeoutError:
                if process.returncode is not None:
                    exit_reason = "process_exited_during_idle"
                    break
                no_output_cycles += 1
                if no_output_cycles >= max_no_output:
                    exit_reason = "app_idle_timeout_kill"
                    _safe_kill(process)
                    if got_any_delta:
                        break
                    yield "\n[Error]: Command timed out."
                    return
                continue

            if not chunk:
                exit_reason = "stdout_eof"
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
                last_event_type = event_type

                if event_type == "session.error":
                    error_msg = event.get("data", {}).get("message", "Unknown error")
                    logger.error("Copilot session error: %s", error_msg)
                    exit_reason = "session_error"
                    yield f"\n[Error]: {error_msg}"
                    _safe_kill(process)
                    return

                if event_type == "assistant.message_delta":
                    delta = event.get("data", {}).get("deltaContent", "")
                    if delta:
                        got_any_delta = True
                        delta_count += 1
                        _emit_log({"type": "text_delta", "content": delta})
                        yield delta

                elif event_type == "tool.execution_start":
                    tool_name = event.get("data", {}).get("toolName", "")
                    if tool_name:
                        tool_count += 1
                        desc = _summarize_tool_call(tool_name, event.get("data", {}).get("arguments", {}))
                        _emit_log({"type": "tool_start", "tool": tool_name, "description": desc})
                        yield f"{TOOL_EVENT_PREFIX}{tool_name}|{desc}\n"

                elif event_type == "assistant.turn_start":
                    turn_id = event.get("data", {}).get("turnId", "0")
                    if turn_id != "0":
                        yield "\n---\n"

                elif event_type == "result":
                    exit_reason = "cli_result_event"
                    result_data = event.get("data", {}) or {}
                    logger.info(
                        "Copilot CLI emitted result event",
                        extra={
                            "result_keys": list(result_data.keys()),
                            "result_preview": json.dumps(result_data)[:500],
                        },
                    )
                    _safe_kill(process)
                    return

        if not got_any_delta:
            stderr_data = await process.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            if stderr_text:
                yield f"[Error]: {stderr_text}"
            else:
                yield "[Error]: No response received from Copilot CLI."

    except Exception as e:
        exit_reason = f"exception:{type(e).__name__}"
        logger.exception("Copilot stream reader raised")
        yield f"[Error]: {e}"
    finally:
        # Always drain stderr so we can see any final CLI message — previously
        # only happened when zero deltas were streamed, which silently hid
        # crashes/limits that occurred mid-stream.
        stderr_tail = ""
        try:
            if process.stderr is not None:
                stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=2)
                stderr_tail = stderr_bytes.decode("utf-8", errors="replace").strip()
        except Exception:
            pass

        if process.returncode is None:
            _safe_kill(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except Exception:
            pass

        logger.info(
            "Copilot stream reader exit",
            extra={
                "exit_reason": exit_reason,
                "returncode": process.returncode,
                "delta_count": delta_count,
                "tool_count": tool_count,
                "last_event_type": last_event_type,
                "stderr_tail": stderr_tail[-2000:] if stderr_tail else "",
            },
        )


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

# Public fallback catalog verified against the Copilot model API and supported
# model documentation on 2026-10-10. Account policies may narrow this list.
# https://docs.github.com/en/copilot/reference/ai-models/supported-models
_DEFAULT_MODELS = [
    {"group": "Claude", "models": [
        {"id": "claude-opus-5.5", "name": "Claude Opus 5.5"},
        {"id": "claude-opus-5.5-1m", "name": "Claude Opus 5.5 (1M context)"},
        {"id": "claude-opus-5", "name": "Claude Opus 5"},
        {"id": "claude-opus-5-1m", "name": "Claude Opus 5 (1M context)"},
        {"id": "claude-opus-4.8", "name": "Claude Opus 4.8"},
        {"id": "claude-opus-4.8-1m", "name": "Claude Opus 4.8 (1M context)"},
        {"id": "claude-sonnet-5.5", "name": "Claude Sonnet 5.5"},
        {"id": "claude-sonnet-5.5-1m", "name": "Claude Sonnet 5.5 (1M context)"},
        {"id": "claude-sonnet-5", "name": "Claude Sonnet 5"},
        {"id": "claude-sonnet-5-1m", "name": "Claude Sonnet 5 (1M context)"},
        {"id": "claude-haiku-5.5", "name": "Claude Haiku 5.5"},
        {"id": "claude-haiku-5.5-1m", "name": "Claude Haiku 5.5 (1M context)"},
        {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5"},
    ]},
    {"group": "GPT", "models": [
        {"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol"},
        {"id": "gpt-6-astra", "name": "GPT-6 Astra"},
        {"id": "gpt-6-sol", "name": "GPT-6 Sol"},
        {"id": "gpt-6-luna", "name": "GPT-6 Luna"},
        {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
        {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
        {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"},
        {"id": "gpt-5.5", "name": "GPT-5.5"},
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
        {"id": "gpt-5.3-codex", "name": "GPT-5.3-Codex"},
        {"id": "gpt-5-mini", "name": "GPT-5 mini"},
    ]},
    {"group": "Gemini", "models": [
        {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
        {"id": "gemini-3.7-flash", "name": "Gemini 3.7 Flash"},
    ]},
    {"group": "Microsoft", "models": [
        {"id": "mai-code-1.1-flash", "name": "MAI-Code-1.1-Flash"},
    ]},
    {"group": "Grok", "models": [
        {"id": "grok-4.7", "name": "Grok 4.7"},
        {"id": "grok-4.6", "name": "Grok 4.6"},
        {"id": "grok-4.5", "name": "Grok 4.5"},
    ]},
]

# Explicitly supported CLI tier aliases, not an inference from native context
# size. GPT/Gemini models with large native windows do not need a tier flag.
_LONG_CONTEXT_ALIASES = {
    model["id"][:-len(_LONG_CONTEXT_SUFFIX)]: model
    for group in _DEFAULT_MODELS
    for model in group["models"]
    if model["id"].endswith(_LONG_CONTEXT_SUFFIX)
}

_MODEL_CATALOG_URL = "https://api.githubcopilot.com/models"
_MODEL_CACHE_TTL_SECONDS = 300
_MODEL_RETRY_SECONDS = 60

# Cached model list — populated at startup and refreshed by API/Telegram reads.
_cached_models: list[dict] | None = None
_model_cache_expires_at = 0.0
_model_discovery_lock = asyncio.Lock()


def get_models() -> list[dict]:
    """Return the last catalog, or the public fallback before discovery."""
    # An authenticated empty catalog is authoritative, not a discovery failure.
    return _cached_models if _cached_models is not None else _DEFAULT_MODELS


def _group_catalog_models(models: list) -> list[dict]:
    """Keep user-selectable, policy-enabled agent models and group providers."""
    groups: dict[str, list[dict]] = {}
    seen: set[str] = set()
    providers = (
        ("claude-", "Claude"), ("gpt-", "GPT"), ("gemini-", "Gemini"),
        ("mai-", "Microsoft"), ("grok-", "Grok"), ("kimi-", "Kimi"),
    )
    for model in models:
        if not isinstance(model, dict) or model.get("model_picker_enabled") is not True:
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        name = model.get("name")
        if not isinstance(name, str) or not name.strip():
            name = model_id
        # Some internal-only entries are still marked picker/policy enabled.
        if "internal" in f"{model_id} {name}".lower() or model_id in seen:
            continue
        policy = model.get("policy") or {}
        capabilities = model.get("capabilities") or {}
        if not isinstance(policy, dict) or policy.get("state", "enabled") != "enabled":
            continue
        if not isinstance(capabilities, dict) or capabilities.get("type") != "chat":
            continue
        supports = capabilities.get("supports") or {}
        if not isinstance(supports, dict) or supports.get("tool_calls") is not True:
            continue

        group = next((group for prefix, group in providers if model_id.startswith(prefix)), "Other")
        if group == "Other" and len(model_id) > 1 and model_id[0] == "o" and model_id[1].isdigit():
            group = "OpenAI Reasoning"
        groups.setdefault(group, []).append({"id": model_id, "name": name})
        seen.add(model_id)

        alias = _LONG_CONTEXT_ALIASES.get(model_id)
        limits = capabilities.get("limits") or {}
        context_size = limits.get("max_context_window_tokens", 0) if isinstance(limits, dict) else 0
        if alias and isinstance(context_size, (int, float)) and context_size >= 1000000 and alias["id"] not in seen:
            groups[group].append(dict(alias))
            seen.add(alias["id"])

    return [{"group": group, "models": models} for group, models in groups.items()]


async def discover_models() -> list[dict]:
    """Refresh the authenticated model catalog without spending an LLM turn."""
    global _cached_models, _model_cache_expires_at

    if time.monotonic() < _model_cache_expires_at:
        return get_models()

    async with _model_discovery_lock:
        # Concurrent startup/API/Telegram requests share one catalog fetch.
        if time.monotonic() < _model_cache_expires_at:
            return get_models()

        # Match the Copilot CLI's token precedence. Deployment supplies GH_TOKEN
        # for the Copilot account, not GH_REPO_TOKEN used by git operations.
        token = (
            os.environ.get("COPILOT_GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or ""
        ).strip()
        if not token:
            logger.warning("Copilot token unavailable, using cached/fallback model list")
            _model_cache_expires_at = time.monotonic() + _MODEL_RETRY_SECONDS
            return get_models()

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
                response = await client.get(
                    _MODEL_CATALOG_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Copilot-Integration-Id": "copilot-cli",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Invalid Copilot model catalog")

            _cached_models = _group_catalog_models(payload["data"])
            _model_cache_expires_at = time.monotonic() + _MODEL_CACHE_TTL_SECONDS
            logger.info("Discovered %d model choices from Copilot catalog", sum(len(group["models"]) for group in _cached_models))
        except Exception as exc:
            # Keep the last successful catalog during outages. Do not log
            # request headers, credentials, or potentially sensitive bodies.
            logger.warning("Copilot model discovery failed (%s), using cached/fallback list", type(exc).__name__)
            _model_cache_expires_at = time.monotonic() + _MODEL_RETRY_SECONDS

        return get_models()


async def run_copilot_sync(prompt: str, agent_name: str | None = None, *, model_name: str | None = None) -> str:
    """Run a Copilot command and return the full output."""
    chunks = []
    async for chunk in run_code_chat(prompt, agent_name, model_name=model_name):
        chunks.append(chunk)
    return "".join(chunks)
