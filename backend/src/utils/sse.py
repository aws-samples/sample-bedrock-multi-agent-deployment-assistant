"""SSE streaming utilities — heartbeats, cancellation helpers, and event formatting."""

import asyncio
import json
import logging
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


def circuit_breaker_error_message(retry_after: float) -> str:
    """Build a user-facing error message for circuit breaker open state."""
    seconds = int(retry_after) if retry_after else 30
    return f"The AI service is temporarily unavailable. Please retry in {seconds} seconds."


# SSE comment line used as a keep-alive heartbeat
SSE_HEARTBEAT = ": heartbeat\n\n"

# Default interval between heartbeats (seconds)
HEARTBEAT_INTERVAL_S = 15


def sse_event(event_type: str, data: dict) -> str:
    """Format a dict as an SSE event string.

    Returns a string like: ``event: progress\\ndata: {"step": 1}\\n\\n``
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def sse_error(message: str, *, recoverable: bool = False) -> str:
    """Format an error as an SSE event string.

    Consistent error format: ``event: error\\ndata: {"message": "..."}\\n\\n``
    The ``recoverable`` flag tells the client whether the stream will continue.
    """
    payload = {"message": message, "recoverable": recoverable} if recoverable else {"message": message}
    return f"event: error\ndata: {json.dumps(payload)}\n\n"


async def with_heartbeats(
    source: AsyncGenerator[str, None],
    *,
    interval: float = HEARTBEAT_INTERVAL_S,
    label: str = "sse",
) -> AsyncGenerator[str, None]:
    """Wrap an SSE async generator to emit heartbeat comments during idle periods.

    While *source* is producing events, those events are forwarded immediately.
    If no event arrives within *interval* seconds, a ``: heartbeat`` SSE comment
    is emitted to keep the connection alive and prevent proxy timeouts.

    On cancellation (client disconnect), a log message is emitted and the
    source generator is cleaned up.
    """
    # We pull items from *source* via an asyncio.Queue so we can race
    # between "next event" and "heartbeat timer" using asyncio.wait_for.
    _SENTINEL = object()
    _ERROR_PREFIX = "__sse_error__:"
    queue: asyncio.Queue[str | object] = asyncio.Queue()

    async def _reader() -> None:
        """Drain source into the queue; push sentinel when done."""
        try:
            async for event in source:
                await queue.put(event)
        except GeneratorExit:
            pass
        except Exception:
            logger.exception("%s: source generator raised", label)
            # Sanitize: don't leak internal exception details to the client
            await queue.put(f"{_ERROR_PREFIX}Internal server error")
        finally:
            await queue.put(_SENTINEL)

    reader_task = asyncio.create_task(_reader())

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield SSE_HEARTBEAT
                continue

            if item is _SENTINEL:
                break
            if isinstance(item, str) and item.startswith(_ERROR_PREFIX):
                yield sse_error(item[len(_ERROR_PREFIX):])
                break
            yield item
    except GeneratorExit:
        logger.info("%s: client disconnected — cancelling stream", label)
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
        await source.aclose()
