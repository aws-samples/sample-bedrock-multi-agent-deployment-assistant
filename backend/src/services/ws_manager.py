"""Local WebSocket connection manager for dev — mirrors API Gateway WS protocol.

In-memory subscription map: ``"{tenant_id}#{project_id}"`` → connected sockets.
The local worker (running in a background thread) calls :func:`notify` which
bridges into the async event loop via ``run_coroutine_threadsafe``.
"""

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Maps "{tenant_id}#{project_id}" → set of subscribed WebSocket connections
_subscriptions: dict[str, set[WebSocket]] = defaultdict(set)

# Reverse map: WebSocket → set of subscription keys (for fast unsubscribe)
_ws_keys: dict[WebSocket, set[str]] = defaultdict(set)

# Captured event loop — set once during FastAPI startup
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the running event loop so worker threads can post messages."""
    global _loop
    _loop = loop


_MAX_SUBSCRIPTIONS_PER_KEY = 50  # Max concurrent WS connections per project
_MAX_KEYS_PER_WS = 10  # Max subscriptions per single WS connection


def subscribe(ws: WebSocket, tenant_id: str, project_id: str) -> None:
    """Register a WebSocket for project updates.

    Enforces connection limits to prevent resource exhaustion.
    """
    key = f"{tenant_id}#{project_id}"

    if len(_subscriptions[key]) >= _MAX_SUBSCRIPTIONS_PER_KEY:
        logger.warning("WS subscription limit reached for %s (%d), rejecting",
                        key, _MAX_SUBSCRIPTIONS_PER_KEY)
        return

    if len(_ws_keys[ws]) >= _MAX_KEYS_PER_WS:
        logger.warning("WS per-connection subscription limit reached (%d), rejecting",
                        _MAX_KEYS_PER_WS)
        return

    _subscriptions[key].add(ws)
    _ws_keys[ws].add(key)
    logger.debug("WS subscribed to %s (total: %d)", key, len(_subscriptions[key]))


def unsubscribe(ws: WebSocket) -> None:
    """Remove a WebSocket from all subscriptions (on disconnect)."""
    for key in _ws_keys.pop(ws, set()):
        _subscriptions[key].discard(ws)
        if not _subscriptions[key]:
            del _subscriptions[key]
    logger.debug("WS unsubscribed from all keys")


async def _broadcast(tenant_id: str, project_id: str, message: dict) -> None:
    """Send *message* to every socket subscribed to this project."""
    key = f"{tenant_id}#{project_id}"
    sockets = list(_subscriptions.get(key, []))
    if not sockets:
        return

    payload = json.dumps(message)
    for ws in sockets:
        try:
            await ws.send_text(payload)
        except Exception:
            logger.debug("Failed to send WS message — removing stale socket")
            unsubscribe(ws)


def notify(tenant_id: str, project_id: str, message: dict) -> None:
    """Thread-safe notify — callable from the local worker thread.

    Schedules the async broadcast on the captured event loop.
    """
    if _loop is None or _loop.is_closed():
        logger.debug("No event loop available for WS notification")
        return

    asyncio.run_coroutine_threadsafe(
        _broadcast(tenant_id, project_id, message),
        _loop,
    )
