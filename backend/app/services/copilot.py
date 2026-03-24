import asyncio
import json
import shutil
from pathlib import Path
from typing import AsyncIterator
from app.config import settings

TIMEOUT_SECONDS = 300  # 5 min max per command


def _find_cli(name: str) -> str | None:
    """Find a CLI tool in PATH."""
    return shutil.which(name)


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
    gh_path = _find_cli("gh")
    if not gh_path:
        yield "[Error]: 'gh' CLI not found. Install: brew install gh"
        return

    model = model_name or settings.copilot_model
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
        max_no_output = 60  # 15 minutes max
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

                # Stream assistant text deltas to the user
                if event_type == "assistant.message_delta":
                    delta = event.get("data", {}).get("deltaContent", "")
                    if delta:
                        got_any_delta = True
                        yield delta

                # Turn complete — we have the full response, stop
                elif event_type == "assistant.turn_end":
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
