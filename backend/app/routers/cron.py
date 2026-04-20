"""Cron job API — endpoints called by the Azure Function timer to execute due jobs."""

import hmac
import logging
from fastapi import APIRouter, Header, HTTPException
from app.config import settings
from app.services import cron_store, email_service
from app.services.copilot import TOOL_EVENT_PREFIX

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


def _verify_secret(x_cron_secret: str = Header(...)) -> None:
    if not settings.cron_secret or not hmac.compare_digest(x_cron_secret, settings.cron_secret):
        raise HTTPException(status_code=403, detail="Invalid cron secret")


@router.get("/due")
async def get_due_jobs(x_cron_secret: str = Header(...)):
    """Return list of job IDs that are currently due for execution."""
    _verify_secret(x_cron_secret)
    jobs = cron_store.get_all_jobs()
    due = [j.id for j in jobs if cron_store.is_job_due(j)]
    return {"due": due}


@router.post("/run/{job_id}")
async def run_job(job_id: str, x_cron_secret: str = Header(...)):
    """Execute a specific cron job: run the agent, email results, notify Telegram."""
    _verify_secret(x_cron_secret)

    job = cron_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not job.enabled:
        return {"status": "skipped", "reason": "job disabled"}

    logger.info("Executing cron job %s: agent=%s prompt=%s", job.id, job.agent_name, job.prompt[:80])

    # Snapshot workspace files before the run to detect new files afterward
    from app.services import copilot, blob_storage
    from pathlib import Path

    workspace = Path(settings.workspace_dir)
    before_files = set()
    if workspace.exists():
        before_files = {str(f.relative_to(workspace)) for f in workspace.rglob("*") if f.is_file()}

    chunks = []
    error = None
    try:
        async for chunk in copilot.run_code_chat(
            job.prompt, job.agent_name, model_name=job.model_name
        ):
            if not chunk.strip().startswith(TOOL_EVENT_PREFIX):
                chunks.append(chunk)
    except Exception as e:
        error = str(e)
        logger.exception("Cron job %s failed", job.id)

    output = "".join(chunks).strip()

    # Sync workspace files to blob storage
    try:
        blob_storage.sync_workspace_to_storage()
    except Exception:
        logger.exception("Failed to sync workspace after cron job %s", job.id)

    # Collect content of any new files created during the run as attachments
    attachments: list[tuple[str, str | bytes]] = []
    if workspace.exists():
        after_files = {str(f.relative_to(workspace)) for f in workspace.rglob("*") if f.is_file()}
        new_files = after_files - before_files
        for rel_path in sorted(new_files):
            fp = workspace / rel_path
            try:
                attachments.append((rel_path, fp.read_text(encoding="utf-8")))
            except (UnicodeDecodeError, ValueError):
                try:
                    attachments.append((rel_path, fp.read_bytes()))
                except Exception:
                    logger.warning("Could not read generated file for attachment: %s", rel_path)

    # Update last_run timestamp
    cron_store.update_last_run(job.id)

    # Build email body
    if error:
        subject = f"[OpenCopilot] Cron job failed: {job.agent_name}"
        body = f"Cron job '{job.agent_name}' (ID: {job.id}) failed.\n\nError: {error}\n\nPrompt: {job.prompt}"
    else:
        subject = f"[OpenCopilot] {job.agent_name} — scheduled report"
        parts = [
            f"Cron job '{job.agent_name}' (ID: {job.id}) completed.\n",
            f"Prompt: {job.prompt}\n",
            f"{'=' * 60}\n",
            output,
        ]
        if attachments:
            parts.append(f"\n\n(See {len(attachments)} attached report file(s).)")
        body = "\n".join(parts)

    email_sent = False
    email_error = ""
    if job.email:
        email_sent, email_error = email_service.send_result_email(job.email, subject, body, attachments=attachments)

    # Send short Telegram notification (include output if no email)
    tg_status = await _notify_telegram(job, error=error, email_sent=email_sent, email_error=email_error, output=output)

    return {
        "status": "error" if error else "ok",
        "job_id": job.id,
        "email_sent": email_sent,
        "telegram_notified": tg_status,
        "output_length": len(output),
    }


async def _notify_telegram(
    job: cron_store.CronJob, error: str | None = None, email_sent: bool = True, email_error: str = "", output: str = ""
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
            # Telegram has a 4096 char limit per message, split if needed
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
