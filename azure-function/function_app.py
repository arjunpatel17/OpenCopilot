import logging
import azure.functions as func
import httpx

app = func.FunctionApp()

CONTAINER_APP_URL = None
CRON_SECRET = None


def _get_config():
    global CONTAINER_APP_URL, CRON_SECRET
    if CONTAINER_APP_URL is None:
        import os
        CONTAINER_APP_URL = os.environ["CONTAINER_APP_URL"].rstrip("/")
        CRON_SECRET = os.environ["CRON_SECRET"]
    return CONTAINER_APP_URL, CRON_SECRET


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False)
def cron_trigger(timer: func.TimerRequest) -> None:
    """Check for due cron jobs every 5 minutes and trigger their execution."""
    base_url, secret = _get_config()
    headers = {"X-Cron-Secret": secret}

    try:
        with httpx.Client(timeout=30) as client:
            # Get due jobs
            resp = client.get(f"{base_url}/api/cron/due", headers=headers)
            resp.raise_for_status()
            due_jobs = resp.json().get("due", [])

            if not due_jobs:
                return

            logging.info("Found %d due cron jobs: %s", len(due_jobs), due_jobs)

            # Execute each due job (use longer timeout — agent runs can take minutes)
            for job_id in due_jobs:
                try:
                    run_resp = client.post(
                        f"{base_url}/api/cron/run/{job_id}",
                        headers=headers,
                        timeout=300,
                    )
                    run_resp.raise_for_status()
                    result = run_resp.json()
                    logging.info("Job %s: %s", job_id, result.get("status"))
                except Exception:
                    logging.exception("Failed to execute job %s", job_id)

    except Exception:
        logging.exception("Cron trigger failed")
