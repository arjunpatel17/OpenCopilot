"""Cron job API — endpoints called by the Azure Function timer to execute due jobs.

Runs are asynchronous: starting one returns 202 with a run ID, and callers poll
``GET /api/cron/runs/{run_id}`` for the outcome. Executing inline is not viable —
Container Apps closes any HTTP request idle for 240s, and the resulting drop in
the HTTP scale metric lets KEDA deactivate the replica mid-run.
"""

import hmac
import logging
from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException, status

from app.config import settings
from app.services import cron_runner, cron_store, run_store

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


@router.post("/run/{job_id}", status_code=status.HTTP_202_ACCEPTED)
async def run_job(job_id: str, x_cron_secret: str = Header(...)):
    """Queue a cron job for execution and return immediately with a run ID."""
    _verify_secret(x_cron_secret)

    job = cron_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not job.enabled:
        return {"status": "skipped", "reason": "job disabled"}

    run = await cron_runner.start_run(job)
    return {
        "status": "accepted",
        "job_id": job.id,
        "run_id": run.id,
        "poll_url": f"/api/cron/runs/{run.id}",
    }


@router.get("/runs")
async def list_runs(
    x_cron_secret: str = Header(...),
    job_id: str | None = None,
    limit: int = 20,
):
    """List recent runs, most recent first."""
    _verify_secret(x_cron_secret)
    limit = max(1, min(limit, 100))
    return {"runs": [asdict(r) for r in run_store.list_runs(job_id=job_id, limit=limit)]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, x_cron_secret: str = Header(...)):
    """Poll the status and result of a single run."""
    _verify_secret(x_cron_secret)

    run = run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return asdict(run)
