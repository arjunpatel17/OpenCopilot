"""Executes cron jobs off the request path.

Nothing here may assume an HTTP request is still open: an agent run routinely
outlives the 240s Container Apps ingress timeout. Runs are driven by the queue
consumer, which holds a message in the queue for the run's full duration so
KEDA keeps a replica alive until the work is actually finished.
"""

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path

from app.config import settings
from app.services import blob_storage, cron_store, email_service, job_queue, run_store
from app.services.copilot import TOOL_EVENT_PREFIX

logger = logging.getLogger(__name__)

# The workspace and the copilot CLI are process-wide shared state, so only one
# agent run may be in flight at a time.
_run_lock = asyncio.Lock()

# Strong refs for fire-and-forget tasks so they aren't garbage collected.
_background_tasks: set[asyncio.Task] = set()

# Floor for how often an in-flight run's queue message is made visible-again-later.
MIN_VISIBILITY_RENEWAL_SECONDS = 30

# job_id -> run_id for runs that are queued or executing on this replica.
_inflight: dict[str, str] = {}


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def start_run(job: cron_store.CronJob) -> run_store.RunRecord:
    """Create a run record and hand the work off for background execution."""
    existing_run_id = _inflight.get(job.id)
    if existing_run_id:
        existing = run_store.get_run(existing_run_id)
        if existing and existing.status not in run_store.TERMINAL_STATUSES:
            logger.info("Job %s already has run %s in flight", job.id, existing.id)
            return existing
        _inflight.pop(job.id, None)

    run = run_store.create_run(job.id, job.agent_name)
    _inflight[job.id] = run.id

    # Stamp last_run at dispatch, not completion: the Azure Function polls every
    # 5 minutes and would otherwise re-trigger a job for the whole time it runs.
    cron_store.update_last_run(job.id)

    if job_queue.is_enabled() and await job_queue.enqueue({"run_id": run.id, "job_id": job.id}):
        return run

    # No queue configured (local dev) or the enqueue failed — fall back to an
    # in-process task. This still survives the ingress timeout, but not a
    # scale-to-zero, so it is a degraded path.
    logger.warning("Agent run queue unavailable; executing run %s in-process", run.id)
    _spawn(_execute_by_id(run.id, job.id))
    return run


async def _execute_by_id(run_id: str, job_id: str) -> None:
    run = run_store.get_run(run_id)
    job = cron_store.get_job(job_id)
    if run is None or job is None:
        logger.error("Cannot execute run %s: run or job %s missing", run_id, job_id)
        _inflight.pop(job_id, None)
        return
    await _run_guarded(job, run)


async def _run_guarded(job: cron_store.CronJob, run: run_store.RunRecord) -> None:
    """Execute a run, guaranteeing the record reaches a terminal state."""
    try:
        async with _run_lock:
            await execute_job(job, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Run %s crashed", run.id)
        run.status = run_store.FAILED
        run.error = "Run crashed unexpectedly"
        run.finished_at = time.time()
        with suppress(Exception):
            run_store.save_run(run)
    finally:
        _inflight.pop(job.id, None)


async def execute_job(job: cron_store.CronJob, run: run_store.RunRecord) -> run_store.RunRecord:
    """Run the agent, email results, notify Telegram, and record the outcome."""
    run.status = run_store.RUNNING
    run.started_at = time.time()
    run.attempts += 1
    run_store.save_run(run)

    logger.info(
        "Executing cron job %s (run %s): agent=%s prompt=%s",
        job.id, run.id, job.agent_name, job.prompt[:80],
    )

    from app.services import copilot

    workspace = Path(settings.workspace_dir)
    before_files: set[str] = set()
    if workspace.exists():
        before_files = {str(f.relative_to(workspace)) for f in workspace.rglob("*") if f.is_file()}

    chunks: list[str] = []
    error: str | None = None
    try:
        async for chunk in copilot.run_code_chat(
            job.prompt, job.agent_name, model_name=job.model_name
        ):
            if not chunk.strip().startswith(TOOL_EVENT_PREFIX):
                chunks.append(chunk)
    except Exception as e:
        error = str(e)
        logger.exception("Cron job %s (run %s) failed", job.id, run.id)

    output = "".join(chunks).strip()

    try:
        blob_storage.sync_workspace_to_storage()
    except Exception:
        logger.exception("Failed to sync workspace after cron job %s", job.id)

    report_links, attachments = _collect_generated_files(workspace, before_files)

    subject, body = _build_email(job, output, error, report_links, attachments)

    email_sent = False
    email_error = ""
    if job.email:
        email_sent, email_error = email_service.send_result_email(
            job.email, subject, body, attachments=attachments
        )

    tg_status = await _notify_telegram(
        job, error=error, email_sent=email_sent, email_error=email_error, output=output
    )

    run.status = run_store.FAILED if error else run_store.SUCCEEDED
    run.finished_at = time.time()
    run.output = output
    run.error = error
    run.email_sent = email_sent
    run.telegram_notified = tg_status
    run.report_files = [rel for rel, _ in report_links]
    run_store.save_run(run)

    try:
        run_store.prune_runs()
    except Exception:
        logger.warning("Run record pruning failed", exc_info=True)

    return run


def _collect_generated_files(
    workspace: Path, before_files: set[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str | bytes]]]:
    """Return (blob links, inline attachments) for files created during the run.

    Links are preferred over attachments because Azure Communication Services
    caps each email request at 10 MB.
    """
    report_links: list[tuple[str, str]] = []
    attachments: list[tuple[str, str | bytes]] = []
    if not workspace.exists():
        return report_links, attachments

    after_files = {str(f.relative_to(workspace)) for f in workspace.rglob("*") if f.is_file()}
    for rel_path in sorted(after_files - before_files):
        if not blob_storage.is_syncable_path(rel_path):
            continue

        url = blob_storage.get_blob_sas_url(rel_path)
        if url:
            report_links.append((rel_path, url))
            continue

        fp = workspace / rel_path
        try:
            if fp.stat().st_size > email_service.ACS_MAX_REQUEST_BYTES:
                logger.warning("Skipping generated file (too large for email): %s", rel_path)
                continue
            try:
                content: str | bytes = fp.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                content = fp.read_bytes()
            attachments.append((rel_path, content))
        except Exception as e:
            logger.warning("Could not read generated file for attachment %s: %s", rel_path, e)

    return report_links, attachments


def _build_email(
    job: cron_store.CronJob,
    output: str,
    error: str | None,
    report_links: list[tuple[str, str]],
    attachments: list[tuple[str, str | bytes]],
) -> tuple[str, str]:
    if report_links:
        link_lines = [f"- {rel_path}:\n  {url}" for rel_path, url in report_links]
        files_section = (
            f"\n\n{'=' * 60}\n"
            f"Report files ({len(report_links)}) — links valid for "
            f"{settings.email_link_expiry_days} day(s):\n"
            + "\n".join(link_lines)
        )
    elif attachments:
        files_section = f"\n\n(See {len(attachments)} attached report file(s).)"
    else:
        files_section = ""

    if error:
        subject = f"[OpenCopilot] Cron job failed: {job.agent_name}"
        body = (
            f"Cron job '{job.agent_name}' (ID: {job.id}) failed.\n\n"
            f"Error: {error}\n\nPrompt: {job.prompt}"
            + files_section
        )
    else:
        subject = f"[OpenCopilot] {job.agent_name} — scheduled report"
        parts = [
            f"Cron job '{job.agent_name}' (ID: {job.id}) completed.\n",
            f"Prompt: {job.prompt}\n",
            f"{'=' * 60}\n",
            output,
        ]
        body = "\n".join(parts) + files_section

    return subject, body


# ========== Queue consumer ==========

async def consumer_loop() -> None:
    """Drain the agent run queue for the lifetime of the process."""
    if not job_queue.is_enabled():
        logger.info("Agent run queue not configured — queue consumer disabled")
        return

    logger.info("Agent run queue consumer started (queue=%s)", settings.agent_queue_name)
    while True:
        try:
            message = await job_queue.receive_one()
            if message is None:
                await asyncio.sleep(settings.agent_queue_poll_interval)
                continue
            await _handle_message(message)
        except asyncio.CancelledError:
            logger.info("Agent run queue consumer stopping")
            raise
        except Exception:
            logger.exception("Agent run queue consumer iteration failed")
            await asyncio.sleep(settings.agent_queue_poll_interval)


async def _handle_message(message) -> None:
    holder = {"message": message}
    job_id: str | None = None
    interrupted = False
    try:
        try:
            payload = json.loads(message.content)
            run_id = payload["run_id"]
            job_id = payload["job_id"]
        except Exception:
            logger.error("Discarding malformed queue message: %r", message.content[:200])
            return

        run = run_store.get_run(run_id)
        if run is None:
            logger.error("Discarding queue message for unknown run %s", run_id)
            return
        if run.status in run_store.TERMINAL_STATUSES:
            logger.info("Run %s already %s — discarding duplicate message", run_id, run.status)
            return

        dequeue_count = getattr(message, "dequeue_count", 1) or 1
        if dequeue_count > settings.agent_queue_max_attempts:
            logger.error("Run %s exceeded %d attempts — abandoning", run_id, settings.agent_queue_max_attempts)
            run.status = run_store.FAILED
            run.error = f"Abandoned after {dequeue_count} delivery attempts"
            run_store.save_run(run)
            return

        job = cron_store.get_job(job_id)
        if job is None:
            logger.error("Discarding queue message for unknown job %s", job_id)
            run.status = run_store.FAILED
            run.error = f"Job {job_id} no longer exists"
            run_store.save_run(run)
            return

        keepalive = asyncio.create_task(_keep_visible(holder))
        try:
            await _run_guarded(job, run)
        finally:
            keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive
    except asyncio.CancelledError:
        # Shutting down mid-run. Leave the message in the queue so the run is
        # redelivered once a replica is back.
        interrupted = True
        raise
    finally:
        if job_id:
            _inflight.pop(job_id, None)
        if not interrupted:
            try:
                await job_queue.delete(holder["message"])
            except Exception:
                # Left in the queue; the dequeue-count guard stops an infinite retry.
                logger.exception("Failed to delete queue message after run")


async def _keep_visible(holder: dict) -> None:
    """Re-extend the message's visibility timeout for the duration of the run.

    Also what keeps the message in the queue, and therefore the replica alive.
    """
    interval = max(MIN_VISIBILITY_RENEWAL_SECONDS, settings.agent_queue_visibility_timeout // 3)
    while True:
        await asyncio.sleep(interval)
        try:
            holder["message"] = await job_queue.renew(holder["message"])
        except Exception:
            logger.exception("Failed to renew visibility timeout for in-flight run")


# ========== Telegram notification ==========

async def _notify_telegram(
    job: cron_store.CronJob,
    error: str | None = None,
    email_sent: bool = True,
    email_error: str = "",
    output: str = "",
) -> bool:
    """Send a short notification to the Telegram chat that created this job.
    When no email is configured, sends the full output directly in Telegram."""
    if not settings.telegram_bot_token:
        return False

    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)

        if error:
            text = f"❌ Cron job `{job.agent_name}` (ID: `{job.id}`) failed:\n{error[:200]}"
            await bot.send_message(chat_id=job.chat_id, text=text, parse_mode="Markdown")
        elif not job.email:
            # No email — send full results in Telegram
            header = f"✅ Cron job `{job.agent_name}` (ID: `{job.id}`) completed.\n\n"
            full_text = header + (output if output else "(no output)")
            for chunk in _split_telegram_message(full_text):
                await bot.send_message(chat_id=job.chat_id, text=chunk)
        elif not email_sent:
            reason = f"\nReason: {email_error}" if email_error else ""
            text = f"⚠️ Cron job `{job.agent_name}` (ID: `{job.id}`) completed, but the email to {job.email} failed to send.{reason}"
            await bot.send_message(chat_id=job.chat_id, text=text, parse_mode="Markdown")
        else:
            text = f"✅ Cron job `{job.agent_name}` (ID: `{job.id}`) completed. Results emailed to {job.email}."
            await bot.send_message(chat_id=job.chat_id, text=text, parse_mode="Markdown")

        return True
    except Exception:
        logger.exception("Failed to send Telegram notification for job %s", job.id)
        return False


def _split_telegram_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message into chunks that fit Telegram's message size limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
