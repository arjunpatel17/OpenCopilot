"""Keeps a replica alive for work that isn't driven by an HTTP request.

Telegram agent runs are started from a background task: the webhook returns in
milliseconds, so the HTTP scale metric drops to zero and KEDA can deactivate the
replica mid-stream. Holding a lease puts a message in the run queue for the
duration of the work, which keeps queue depth above zero and the replica alive.

The lease message is sent already-invisible so the run consumer never dequeues
it, and carries a TTL so a crashed replica can't pin the app at one instance.
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from app.config import settings
from app.services import job_queue

logger = logging.getLogger(__name__)


async def acquire(reason: str):
    client = job_queue.get_client()
    if client is None:
        return None
    ttl = settings.activity_lease_ttl
    try:
        handle = await asyncio.to_thread(
            client.send_message,
            json.dumps({"type": "lease", "reason": reason, "at": time.time()}),
            visibility_timeout=ttl - 60,
            time_to_live=ttl,
        )
    except Exception:
        logger.exception("Could not acquire replica lease for %s", reason)
        return None
    logger.info("Holding replica lease %s (%s)", handle.id, reason)
    return handle


async def release(handle) -> None:
    client = job_queue.get_client()
    if client is None or handle is None:
        return
    try:
        await asyncio.to_thread(client.delete_message, handle.id, handle.pop_receipt)
    except Exception:
        # Not fatal: the lease TTL expires on its own.
        logger.warning("Could not release replica lease %s", handle.id, exc_info=True)


@asynccontextmanager
async def hold(reason: str):
    """Hold a replica open for the duration of the block."""
    handle = await acquire(reason)
    try:
        yield
    finally:
        await release(handle)
