"""Strands callback handler that logs to the structured JSON logger.

Replaces the default PrintingCallbackHandler to prevent console noise
and route agent execution events through the application's structured
logging pipeline.
"""

import logging
import threading
from typing import Any

logger = logging.getLogger("strands.agent")


class LoggingCallbackHandler:
    """Route Strands agent events to the structured logger.

    Logs tool invocations and completed text at DEBUG level — silent in
    production (INFO), visible when AI_DEPLOY_DEBUG=true.

    Uses thread-local storage for the tool counter so concurrent agent
    invocations across threads do not cross-contaminate metrics.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    @property
    def _tool_count(self) -> int:
        return getattr(self._local, "tool_count", 0)

    @_tool_count.setter
    def _tool_count(self, value: int) -> None:
        self._local.tool_count = value

    def reset(self) -> None:
        """Reset per-thread counters. Call at the start of each agent invocation."""
        self._local.tool_count = 0

    def __call__(self, **kwargs: Any) -> None:
        data: str = kwargs.get("data", "")
        complete: bool = kwargs.get("complete", False)
        reasoning: str = kwargs.get("reasoningText", "")

        tool_use = (
            kwargs.get("event", {})
            .get("contentBlockStart", {})
            .get("start", {})
            .get("toolUse")
        )
        if tool_use:
            self._tool_count += 1
            logger.debug("Tool #%d: %s", self._tool_count, tool_use.get("name", "unknown"))

        if complete and data:
            preview = data[:200] + ("..." if len(data) > 200 else "")
            logger.debug("Agent output (%d chars): %s", len(data), preview)

        if reasoning:
            logger.debug("Reasoning: %s", reasoning[:100])


# Module-level singleton — safe for concurrent use via thread-local counters
logging_callback_handler = LoggingCallbackHandler()
