"""Azure Queue Storage hand-off for agent runs.

Queue depth is also the KEDA scale signal for the Container App. A run message
stays in the queue — invisible, with its visibility timeout renewed — for the
whole run, so the replica is never deactivated mid-run. The default HTTP scale
rule can't do this: its metric drops to zero the moment the client connection
closes, which the 240s ingress timeout guarantees on any long run.
"""

import asyncio
import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_client: Any = None
_initialized = False


def is_enabled() -> bool:
    return bool(settings.azure_storage_connection_string)


def _get_client():
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True
    if not is_enabled():
        return None
    try:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.queue import QueueClient

        client = QueueClient.from_connection_string(
            settings.azure_storage_connection_string, settings.agent_queue_name
        )
        try:
            client.create_queue()
        except ResourceExistsError:
            pass
        _client = client
    except Exception:
        logger.exception("Failed to initialise agent run queue client")
        _client = None
    return _client


def get_client():
    """Shared queue client, for callers that need raw message operations."""
    return _get_client()


async def enqueue(payload: dict[str, Any]) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        await asyncio.to_thread(client.send_message, json.dumps(payload))
        return True
    except Exception:
        logger.exception("Failed to enqueue agent run %s", payload)
        return False


async def receive_one():
    """Dequeue a single run message, or None when the queue is empty."""
    client = _get_client()
    if client is None:
        return None
    return await asyncio.to_thread(
        client.receive_message,
        visibility_timeout=settings.agent_queue_visibility_timeout,
    )


async def renew(message):
    """Extend a message's visibility timeout; returns the updated message."""
    client = _get_client()
    if client is None:
        return message
    return await asyncio.to_thread(
        client.update_message,
        message,
        visibility_timeout=settings.agent_queue_visibility_timeout,
    )


async def delete(message) -> None:
    client = _get_client()
    if client is None:
        return
    await asyncio.to_thread(client.delete_message, message)
