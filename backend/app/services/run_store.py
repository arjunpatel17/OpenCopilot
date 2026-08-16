"""Durable records for agent runs — the state clients poll for.

Run state lives in blob storage rather than process memory because a run
outlives the HTTP request that started it and must survive a replica restart.
"""

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, asdict, field

from app.config import settings

logger = logging.getLogger(__name__)

RUNS_PREFIX = "cron/runs/"

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

TERMINAL_STATUSES = (SUCCEEDED, FAILED)

# Run IDs land in a blob path, so they must be validated before use.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")

# Output is echoed back on every poll; the untruncated text goes out by email.
MAX_STORED_OUTPUT = 200_000


@dataclass
class RunRecord:
    id: str
    job_id: str
    agent_name: str
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    output: str = ""
    error: str | None = None
    email_sent: bool = False
    telegram_notified: bool = False
    report_files: list[str] = field(default_factory=list)


def is_valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID_RE.match(run_id))


def _blob_path(run_id: str) -> str:
    if not is_valid_run_id(run_id):
        raise ValueError(f"Invalid run id: {run_id!r}")
    return f"{RUNS_PREFIX}{run_id}.json"


def create_run(job_id: str, agent_name: str) -> RunRecord:
    run = RunRecord(id=secrets.token_hex(6), job_id=job_id, agent_name=agent_name)
    save_run(run)
    logger.info("Created run %s for job %s (%s)", run.id, job_id, agent_name)
    return run


def save_run(run: RunRecord) -> None:
    from app.services.blob_storage import upload_blob

    if len(run.output) > MAX_STORED_OUTPUT:
        run.output = run.output[:MAX_STORED_OUTPUT] + "\n\n[output truncated]"
    data = json.dumps(asdict(run), indent=2).encode("utf-8")
    upload_blob(_blob_path(run.id), data, content_type="application/json")


def get_run(run_id: str) -> RunRecord | None:
    from app.services.blob_storage import get_blob_content

    if not is_valid_run_id(run_id):
        return None
    try:
        data = get_blob_content(_blob_path(run_id))
        return RunRecord(**json.loads(data.decode("utf-8")))
    except Exception:
        return None


def list_runs(job_id: str | None = None, limit: int = 20) -> list[RunRecord]:
    from app.services.blob_storage import list_blobs

    runs: list[RunRecord] = []
    try:
        blobs = list_blobs(RUNS_PREFIX)
    except Exception:
        logger.exception("Failed to list run records")
        return runs

    for info in blobs:
        if info.is_folder or not info.path.endswith(".json"):
            continue
        run_id = info.path.rsplit("/", 1)[-1].removesuffix(".json")
        run = get_run(run_id)
        if run and (job_id is None or run.job_id == job_id):
            runs.append(run)

    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs[:limit]


def prune_runs(keep: int | None = None) -> int:
    """Delete the oldest run records beyond the retention limit."""
    from app.services.blob_storage import delete_blob

    keep = keep if keep is not None else settings.max_run_records
    try:
        runs = list_runs(limit=10_000)
    except Exception:
        return 0

    removed = 0
    for run in runs[keep:]:
        try:
            delete_blob(_blob_path(run.id))
            removed += 1
        except Exception:
            logger.warning("Could not prune run record %s", run.id)
    return removed
