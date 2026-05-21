"""Strands Agent observability hooks — captures end-to-end workflow metrics.

Registers as a HookProvider on each Agent to capture:
- Model call latency (per-agent, per-invocation)
- Model call count per invocation (multi-turn tool-use loops)
- Tool call latency (per-tool, per-agent)
- Token usage (input/output per model call)
- Invocation-level latency (full agent turn including all model+tool calls)
- Error rates (model failures, tool failures)

All metrics published non-blocking to CloudWatch via MetricsPublisher.
"""

import logging
import time
from dataclasses import dataclass

from strands.hooks.events import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
)
from strands.hooks.registry import HookProvider, HookRegistry

logger = logging.getLogger(__name__)


@dataclass
class _InvocationState:
    """Per-invocation accumulator for metrics."""

    start_time: float = 0.0
    model_call_count: int = 0
    model_call_start: float = 0.0
    tool_call_start: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    model_errors: int = 0
    tool_errors: int = 0


_HOOK_STATE_KEY = "_observability_state"


class ObservabilityHook(HookProvider):
    """Captures agent execution metrics and publishes to CloudWatch."""

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before_invocation)
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)
        registry.add_callback(AfterModelCallEvent, self._after_model_call)
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)
        registry.add_callback(AfterInvocationEvent, self._after_invocation)

    def _get_state(self, invocation_state: dict) -> _InvocationState:
        if _HOOK_STATE_KEY not in invocation_state:
            invocation_state[_HOOK_STATE_KEY] = _InvocationState()
        return invocation_state[_HOOK_STATE_KEY]

    def _before_invocation(self, event: BeforeInvocationEvent, **kwargs) -> None:
        state = self._get_state(event.invocation_state)
        state.start_time = time.perf_counter()

    def _before_model_call(self, event: BeforeModelCallEvent, **kwargs) -> None:
        state = self._get_state(event.invocation_state)
        state.model_call_start = time.perf_counter()

    def _after_model_call(self, event: AfterModelCallEvent, **kwargs) -> None:
        from src.config.metrics import metrics

        state = self._get_state(event.invocation_state)
        state.model_call_count += 1
        latency_ms = (time.perf_counter() - state.model_call_start) * 1000

        agent_name = event.invocation_state.get("agent_name", "unknown")
        tenant_id = event.invocation_state.get("tenant_id", "default")

        if event.exception:
            state.model_errors += 1
            metrics._submit(
                metrics._put_metric, "AgentModelCallError", 1, "Count",
                [{"Name": "AgentName", "Value": agent_name}, {"Name": "TenantId", "Value": tenant_id}],
            )
        else:
            metrics._submit(
                metrics._put_metric, "AgentModelCallLatencyMs", latency_ms, "Milliseconds",
                [{"Name": "AgentName", "Value": agent_name}, {"Name": "TenantId", "Value": tenant_id}],
            )

        if event.stop_response and hasattr(event.stop_response.message, "usage"):
            usage = event.stop_response.message.usage
            if usage:
                input_tokens = getattr(usage, "input_tokens", 0) or 0
                output_tokens = getattr(usage, "output_tokens", 0) or 0
                state.total_input_tokens += input_tokens
                state.total_output_tokens += output_tokens

    def _before_tool_call(self, event: BeforeToolCallEvent, **kwargs) -> None:
        state = self._get_state(event.invocation_state)
        state.tool_call_start = time.perf_counter()

    def _after_tool_call(self, event: AfterToolCallEvent, **kwargs) -> None:
        from src.config.metrics import metrics

        state = self._get_state(event.invocation_state)
        latency_ms = (time.perf_counter() - state.tool_call_start) * 1000

        agent_name = event.invocation_state.get("agent_name", "unknown")
        tool_name = event.tool_use.get("name", "unknown") if isinstance(event.tool_use, dict) else getattr(event.tool_use, "name", "unknown")
        tenant_id = event.invocation_state.get("tenant_id", "default")

        if event.exception:
            state.tool_errors += 1
            metrics._submit(
                metrics._put_metric, "AgentToolCallError", 1, "Count",
                [
                    {"Name": "AgentName", "Value": agent_name},
                    {"Name": "ToolName", "Value": tool_name},
                    {"Name": "TenantId", "Value": tenant_id},
                ],
            )
        else:
            metrics._submit(
                metrics._put_metric, "AgentToolCallLatencyMs", latency_ms, "Milliseconds",
                [
                    {"Name": "AgentName", "Value": agent_name},
                    {"Name": "ToolName", "Value": tool_name},
                    {"Name": "TenantId", "Value": tenant_id},
                ],
            )

    def _after_invocation(self, event: AfterInvocationEvent, **kwargs) -> None:
        from src.config.metrics import metrics

        state = self._get_state(event.invocation_state)
        total_ms = (time.perf_counter() - state.start_time) * 1000

        agent_name = event.invocation_state.get("agent_name", "unknown")
        tenant_id = event.invocation_state.get("tenant_id", "default")

        dims = [{"Name": "AgentName", "Value": agent_name}, {"Name": "TenantId", "Value": tenant_id}]

        metrics._submit(metrics._put_metric, "AgentInvocationLatencyMs", total_ms, "Milliseconds", dims)
        metrics._submit(metrics._put_metric, "AgentModelCallsPerInvocation", state.model_call_count, "Count", dims)

        if state.total_input_tokens > 0:
            metrics._submit(metrics._put_metric, "AgentInputTokens", state.total_input_tokens, "Count", dims)
        if state.total_output_tokens > 0:
            metrics._submit(metrics._put_metric, "AgentOutputTokens", state.total_output_tokens, "Count", dims)

        # Clean up
        event.invocation_state.pop(_HOOK_STATE_KEY, None)


# Singleton instance — shared across all agents
observability_hook = ObservabilityHook()
