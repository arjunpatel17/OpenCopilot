import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import AsyncIterator
from app.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 300  # 5 min max per command

# Read-only tools for plan mode (no edit/shell/write)
PLAN_MODE_TOOLS = ["read", "search", "web"]

# Marker prefix for tool events (not normal text — parsed by callers)
TOOL_EVENT_PREFIX = "\x00TOOL:"


def _find_cli(name: str) -> str | None:
    """Find a CLI tool in PATH."""
    return shutil.which(name)


def ensure_workspace_dirs():
    """Ensure standard workspace directories exist."""
    workspace = Path(settings.workspace_dir)
    (workspace / "projects").mkdir(parents=True, exist_ok=True)
    (workspace / "reports").mkdir(parents=True, exist_ok=True)


def _load_agent_instructions(agent_name: str) -> str | None:
    """Load the body of an .agent.md file to use as system context."""
    from app.services.agent_parser import load_agent
    agent_path = Path(settings.agents_path) / f"{agent_name}.agent.md"
    if not agent_path.exists():
        return None
    try:
        agent = load_agent(agent_path)
        return agent.body
    except Exception:
        return None


async def _run_cli(args: list[str], error_label: str) -> AsyncIterator[str]:
    """Run a CLI command, yield output chunks, handle hangs and errors."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,  # close stdin so interactive prompts fail fast
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
        full_output = []
        no_output_cycles = 0
        max_no_output = 60  # 60 * 15s = 15 minutes max silence
        # After receiving output, allow less silence before assuming done
        max_no_output_after_data = 8  # 8 * 15s = 2 minutes post-output silence

        while True:
            try:
                chunk = await asyncio.wait_for(process.stdout.read(512), timeout=15)
            except asyncio.TimeoutError:
                # Check if process is still running
                if process.returncode is not None:
                    break
                no_output_cycles += 1
                # If we already got output and then silence, finish sooner
                effective_max = max_no_output_after_data if full_output else max_no_output
                if no_output_cycles >= effective_max:
                    process.kill()
                    if full_output:
                        # Got output then silence — CLI likely finished its reply
                        break
                    yield "\n[Error]: Command timed out after 15 minutes with no output."
                    return
                # Still running — yield a keepalive so callers know it's alive
                continue
            if not chunk:
                break
            no_output_cycles = 0  # Reset on actual output
            text = chunk.decode("utf-8", errors="replace")
            full_output.append(text)
            yield text

        await asyncio.wait_for(process.wait(), timeout=10)

        if process.returncode != 0:
            stderr = await process.stderr.read()
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            combined = "".join(full_output) + error_msg
            # Detect common setup issues
            if "auth login" in combined or "not logged" in combined.lower():
                yield "\n[Error]: GitHub CLI not authenticated. Run: gh auth login"
            elif "Install" in combined and "y/N" in combined:
                yield "\n[Error]: GitHub Copilot CLI not installed. Run: gh extension install github/gh-copilot"
            elif error_msg:
                yield f"\n[Error]: {error_msg}"

    except asyncio.TimeoutError:
        process.kill()
        yield "\n[Error]: Command timed out."


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


async def run_code_chat(prompt: str, agent_name: str | None = None, *, model_name: str | None = None) -> AsyncIterator[str]:
    """Run an agent. Tries `code chat` first, falls back to gh copilot with agent instructions."""
    code_path = _find_cli("code")
    if code_path:
        args = [code_path, "chat"]
        if agent_name:
            args.extend(["-m", agent_name])
        else:
            args.extend(["-m", "agent"])
        args.append(prompt)
        async for chunk in _run_cli(args, "code chat"):
            yield chunk
        return

    # Fallback: load agent instructions and send via gh copilot
    async for chunk in run_with_agent(prompt, agent_name, model_name=model_name):
        yield chunk


async def run_with_agent(prompt: str, agent_name: str | None = None, *, model_name: str | None = None) -> AsyncIterator[str]:
    """Run a prompt with agent instructions injected, via gh copilot."""
    if agent_name:
        instructions = _load_agent_instructions(agent_name)
        if instructions:
            combined = f"Follow these instructions:\n\n{instructions}\n\n---\nUser request: {prompt}"
        else:
            yield f"[Warning]: Agent '{agent_name}' not found, running as freeform.\n\n"
            combined = prompt
    else:
        combined = prompt

    async for chunk in run_gh_copilot(combined, model_name=model_name):
        yield chunk


async def run_gh_copilot(prompt: str, *, model_name: str | None = None) -> AsyncIterator[str]:
    """Run `gh copilot -p` with JSON output and yield text chunks until the turn ends."""
    # Prefer standalone copilot binary, fall back to gh copilot wrapper
    copilot_path = _find_cli("copilot")
    gh_path = _find_cli("gh")
    if not copilot_path and not gh_path:
        yield "[Error]: Neither 'copilot' nor 'gh' CLI found in PATH."
        return

    model = model_name or settings.copilot_model
    if copilot_path:
        args = [copilot_path, "--allow-all", "--output-format", "json"]
    else:
        args = [gh_path, "copilot", "--", "--allow-all", "--output-format", "json"]
    if model:
        args.extend(["--model", model])
    args.extend(["-p", prompt])

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=settings.workspace_dir,
        )
    except FileNotFoundError:
        yield "[Error]: 'gh' CLI not found in PATH."
        return

    assert process.stdout is not None
    assert process.stderr is not None

    try:
        buffer = ""
        no_output_cycles = 0
        max_no_output = 60  # 60 * 15s = 15 minutes max silence
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

            # Process complete JSONL lines
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

                # Capture errors from the session
                if event_type == "session.error":
                    error_msg = event.get("data", {}).get("message", "Unknown error")
                    logger.error("Copilot session error: %s", error_msg)
                    yield f"\n[Error]: {error_msg}"
                    process.kill()
                    return

                # Stream assistant text deltas to the user
                if event_type == "assistant.message_delta":
                    delta = event.get("data", {}).get("deltaContent", "")
                    if delta:
                        got_any_delta = True
                        yield delta

                # Show tool execution activity
                elif event_type == "tool.execution_start":
                    tool_name = event.get("data", {}).get("toolName", "")
                    if tool_name:
                        desc = _summarize_tool_call(tool_name, event.get("data", {}).get("arguments", {}))
                        yield f"{TOOL_EVENT_PREFIX}{tool_name}|{desc}\n"

                # Show new turn starting (after tool results return)
                elif event_type == "assistant.turn_start":
                    turn_id = event.get("data", {}).get("turnId", "0")
                    if turn_id != "0":
                        yield "\n---\n"

                # Process finished — final summary event
                elif event_type == "result":
                    process.kill()
                    return

        # If we got no deltas, check stderr for error info
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


# ========== Plan mode (read-only, no edits/shell) ==========

async def run_plan_mode(prompt: str, agent_name: str | None = None, *, model_name: str | None = None) -> AsyncIterator[str]:
    """Run copilot in plan mode — read-only tools, no edits or shell commands."""
    if agent_name:
        instructions = _load_agent_instructions(agent_name)
        if instructions:
            combined = (
                "You are in PLAN MODE. Analyze, research, and create a detailed plan. "
                "Do NOT edit files or run shell commands.\n\n"
                f"Follow these instructions:\n\n{instructions}\n\n---\nUser request: {prompt}"
            )
        else:
            yield f"[Warning]: Agent '{agent_name}' not found, running as freeform plan.\n\n"
            combined = (
                "You are in PLAN MODE. Analyze, research, and create a detailed plan. "
                f"Do NOT edit files or run shell commands.\n\nUser request: {prompt}"
            )
    else:
        combined = (
            "You are in PLAN MODE. Analyze, research, and create a detailed plan. "
            f"Do NOT edit files or run shell commands.\n\nUser request: {prompt}"
        )

    copilot_path = _find_cli("copilot")
    gh_path = _find_cli("gh")
    if not copilot_path and not gh_path:
        yield "[Error]: Neither 'copilot' nor 'gh' CLI found in PATH."
        return

    model = model_name or settings.copilot_model
    if copilot_path:
        args = [copilot_path, "--allow-all", "--output-format", "json"]
        for tool in PLAN_MODE_TOOLS:
            args.extend(["--available-tools", tool])
    else:
        args = [gh_path, "copilot", "--", "--allow-all", "--output-format", "json"]
        for tool in PLAN_MODE_TOOLS:
            args.extend(["--available-tools", tool])
    if model:
        args.extend(["--model", model])
    args.extend(["-p", combined])

    async for chunk in _run_jsonl_stream(args):
        yield chunk


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
                        yield delta

                elif event_type == "tool.execution_start":
                    tool_name = event.get("data", {}).get("toolName", "")
                    if tool_name:
                        desc = _summarize_tool_call(tool_name, event.get("data", {}).get("arguments", {}))
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
    gh_path = _find_cli("gh")
    if copilot_path:
        args = [copilot_path, "version"]
    elif gh_path:
        args = [gh_path, "copilot", "--", "version"]
    else:
        return "Copilot CLI not found"

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
    gh_path = _find_cli("gh")
    if not copilot_path and not gh_path:
        logger.warning("No copilot CLI found, using default model list")
        _cached_models = _DEFAULT_MODELS
        return _cached_models

    # Ask copilot to list models via a quick prompt
    try:
        raw_output = []
        async for chunk in run_gh_copilot(
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
    if agent_name:
        async for chunk in run_with_agent(prompt, agent_name, model_name=model_name):
            chunks.append(chunk)
    else:
        async for chunk in run_gh_copilot(prompt, model_name=model_name):
            chunks.append(chunk)
    return "".join(chunks)
