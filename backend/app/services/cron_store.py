"""Cron job storage — CRUD with Azure Blob Storage persistence."""

import json
import logging
import secrets
import time
from dataclasses import dataclass, asdict, field

logger = logging.getLogger(__name__)

BLOB_PATH = "cron/jobs.json"

# Schedule presets: name -> interval in seconds
SCHEDULE_PRESETS: dict[str, int] = {
    "every 1h": 3600,
    "every 6h": 21600,
    "daily": 86400,
    "weekly": 604800,
}

# Weekdays handled specially (mon-fri, checked at execution time)
WEEKDAY_SCHEDULES = {"weekdays"}

ALL_SCHEDULES = set(SCHEDULE_PRESETS.keys()) | WEEKDAY_SCHEDULES


@dataclass
class CronJob:
    id: str
    chat_id: int
    agent_name: str
    prompt: str
    schedule: str
    email: str
    model_name: str | None = None
    created_at: float = field(default_factory=time.time)
    enabled: bool = True
    last_run: float | None = None


def _generate_id() -> str:
    return secrets.token_hex(2)


def _load_jobs() -> list[CronJob]:
    from app.services.blob_storage import get_blob_content
    try:
        data = get_blob_content(BLOB_PATH)
        items = json.loads(data.decode("utf-8"))
        return [CronJob(**item) for item in items]
    except Exception:
        return []


def _save_jobs(jobs: list[CronJob]) -> None:
    from app.services.blob_storage import upload_blob
    data = json.dumps([asdict(j) for j in jobs], indent=2).encode("utf-8")
    upload_blob(BLOB_PATH, data, content_type="application/json")


def add_job(
    chat_id: int,
    agent_name: str,
    prompt: str,
    schedule: str,
    email: str,
    model_name: str | None = None,
) -> CronJob:
    jobs = _load_jobs()
    job = CronJob(
        id=_generate_id(),
        chat_id=chat_id,
        agent_name=agent_name,
        prompt=prompt,
        schedule=schedule,
        email=email,
        model_name=model_name,
    )
    jobs.append(job)
    _save_jobs(jobs)
    logger.info("Cron job %s created for chat %s: %s %s", job.id, chat_id, schedule, agent_name)
    return job


def remove_job(job_id: str, chat_id: int) -> bool:
    jobs = _load_jobs()
    before = len(jobs)
    jobs = [j for j in jobs if not (j.id == job_id and j.chat_id == chat_id)]
    if len(jobs) == before:
        return False
    _save_jobs(jobs)
    logger.info("Cron job %s removed by chat %s", job_id, chat_id)
    return True


def list_jobs(chat_id: int) -> list[CronJob]:
    return [j for j in _load_jobs() if j.chat_id == chat_id]


def get_all_jobs() -> list[CronJob]:
    return _load_jobs()


def get_job(job_id: str) -> CronJob | None:
    for j in _load_jobs():
        if j.id == job_id:
            return j
    return None


def update_last_run(job_id: str) -> None:
    jobs = _load_jobs()
    for j in jobs:
        if j.id == job_id:
            j.last_run = time.time()
            break
    _save_jobs(jobs)


def is_job_due(job: CronJob) -> bool:
    if not job.enabled:
        return False

    now = time.time()

    # Weekday check: only run Mon-Fri
    if job.schedule == "weekdays":
        import datetime
        weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
        if weekday >= 5:  # Saturday=5, Sunday=6
            return False
        interval = 86400  # once per day
    else:
        interval = SCHEDULE_PRESETS.get(job.schedule)
        if interval is None:
            return False

    if job.last_run is None:
        return True

    return (now - job.last_run) >= interval
