import datetime
import json
import logging
import os
import azure.functions as func
import httpx
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

CONTAINER_APP_URL = None
CRON_SECRET = None
STORAGE_CONNECTION_STRING = None
STORAGE_CONTAINER = None

BLOB_PATH = "cron/jobs.json"

# Schedule presets: name -> interval in seconds (must match backend/cron_store.py)
SCHEDULE_PRESETS = {
    "every 1h": 3600,
    "every 6h": 21600,
    "daily": 86400,
    "weekly": 604800,
}
WEEKDAY_SCHEDULES = {"weekdays"}


def _get_config():
    global CONTAINER_APP_URL, CRON_SECRET, STORAGE_CONNECTION_STRING, STORAGE_CONTAINER
    if CONTAINER_APP_URL is None:
        CONTAINER_APP_URL = os.environ["CONTAINER_APP_URL"].rstrip("/")
        CRON_SECRET = os.environ["CRON_SECRET"]
        STORAGE_CONNECTION_STRING = os.environ["STORAGE_CONNECTION_STRING"]
        STORAGE_CONTAINER = os.environ.get("STORAGE_CONTAINER", "copilot-files")
    return CONTAINER_APP_URL, CRON_SECRET


def _get_due_jobs_from_blob() -> list[str]:
    """Read cron/jobs.json from Blob Storage and return IDs of due jobs.

    This avoids waking the Container App just to check if jobs are due.
    """
    try:
        blob_service = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        blob_client = blob_service.get_blob_client(STORAGE_CONTAINER, BLOB_PATH)
        data = blob_client.download_blob().readall()
        jobs = json.loads(data.decode("utf-8"))
    except Exception:
        logging.debug("No cron jobs file found in blob storage")
        return []

    import time

    now = time.time()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    due = []

    for job in jobs:
        if not job.get("enabled", True):
            continue

        schedule = job.get("schedule", "")

        if schedule == "weekdays":
            if now_dt.weekday() >= 5:
                continue
            interval = 86400
        else:
            interval = SCHEDULE_PRESETS.get(schedule)
            if interval is None:
                continue

        run_at = job.get("run_at")
        if run_at:
            try:
                target_hour, target_minute = map(int, run_at.split(":"))
            except (ValueError, AttributeError):
                continue
            target_minutes = target_hour * 60 + target_minute
            current_minutes = now_dt.hour * 60 + now_dt.minute
            diff = current_minutes - target_minutes
            if diff < 0 or diff >= 5:
                continue

        last_run = job.get("last_run")
        if last_run is not None and (now - last_run) < (interval * 0.9):
            continue

        due.append(job["id"])

    return due


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False)
def cron_trigger(timer: func.TimerRequest) -> None:
    """Check for due cron jobs every 5 minutes and trigger their execution.

    Reads jobs.json directly from Blob Storage so the Container App is only
    woken up when there is actual work to do (saving ~$2.50/day in idle costs).
    """
    _get_config()

    # Check blob storage first — this does NOT wake the Container App
    due_jobs = _get_due_jobs_from_blob()
    if not due_jobs:
        return

    base_url = CONTAINER_APP_URL
    headers = {"X-Cron-Secret": CRON_SECRET}

    logging.info("Found %d due cron jobs: %s", len(due_jobs), due_jobs)

    try:
        with httpx.Client(timeout=30) as client:
            # Execute each due job (use longer timeout — agent runs can take minutes)
            for job_id in due_jobs:
                try:
                    run_resp = client.post(
                        f"{base_url}/api/cron/run/{job_id}",
                        headers=headers,
                        timeout=900,
                    )
                    run_resp.raise_for_status()
                    result = run_resp.json()
                    logging.info("Job %s: %s", job_id, result.get("status"))
                except Exception:
                    logging.exception("Failed to execute job %s", job_id)
    except Exception:
        logging.exception("Cron trigger failed")
