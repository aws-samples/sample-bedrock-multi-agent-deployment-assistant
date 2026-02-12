"""CloudWatch custom metrics publisher for AI-LCM.

Publishes application-level metrics to the AI-LCM namespace:
- BedrockInvocationLatencyMs — agent call duration by agent name
- RateLimitExceeded — rate limiter rejections by endpoint
- RetryAttempt — tenacity retries by agent name

All publishing is non-blocking via a background ThreadPoolExecutor.
Gracefully degrades when AWS credentials are unavailable (local dev).
"""

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_NAMESPACE = "AI-LCM"


class MetricsPublisher:
    """Lazy-initialized, non-blocking CloudWatch metrics publisher."""

    def __init__(self) -> None:
        self._client = None
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._disabled = False
        self._shutdown = False

    def _get_client(self) -> object:
        if self._client is None:
            import boto3
            from src.config.settings import settings

            self._client = boto3.client(
                "cloudwatch", region_name=settings.aws_region
            )
        return self._client

    def shutdown(self, wait: bool = True) -> None:
        """Gracefully shut down the metrics publisher, draining pending publishes."""
        self._shutdown = True
        self._executor.shutdown(wait=wait)
        logger.info("MetricsPublisher shut down")

    def _put_metric(
        self,
        metric_name: str,
        value: float,
        unit: str,
        dimensions: list[dict],
    ) -> None:
        """Publish a single metric data point. Runs in background thread."""
        from src.config.settings import settings

        if self._disabled or self._shutdown or not settings.metrics_enabled:
            return
        try:
            self._get_client().put_metric_data(
                Namespace=_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": metric_name,
                        "Value": value,
                        "Unit": unit,
                        "Dimensions": dimensions,
                    }
                ],
            )
        except Exception as exc:
            exc_type = type(exc).__name__
            if exc_type in ("NoCredentialsError", "PartialCredentialsError"):
                logger.info(
                    "AWS credentials unavailable — disabling CloudWatch metrics"
                )
                self._disabled = True
            else:
                logger.debug("Failed to publish metric %s: %s", metric_name, exc)

    def _submit(self, fn, *args) -> None:
        """Submit work to the executor with the caller's contextvars propagated."""
        ctx = contextvars.copy_context()
        self._executor.submit(ctx.run, fn, *args)

    def record_latency(
        self, agent_name: str, duration_ms: float, tenant_id: str = "default"
    ) -> None:
        """Record Bedrock invocation latency for an agent."""
        self._submit(
            self._put_metric,
            "BedrockInvocationLatencyMs",
            duration_ms,
            "Milliseconds",
            [
                {"Name": "AgentName", "Value": agent_name},
                {"Name": "TenantId", "Value": tenant_id},
            ],
        )

    def record_rate_limit(self, endpoint: str, client_ip: str) -> None:
        """Record a rate-limit exceeded event."""
        self._submit(
            self._put_metric,
            "RateLimitExceeded",
            1,
            "Count",
            [{"Name": "Endpoint", "Value": endpoint}],
        )

    def record_retry(self, agent_name: str, attempt_number: int) -> None:
        """Record a tenacity retry attempt."""
        self._submit(
            self._put_metric,
            "RetryAttempt",
            attempt_number,
            "Count",
            [{"Name": "AgentName", "Value": agent_name}],
        )

    def record_iac_path(self, path: str, tenant_id: str = "default") -> None:
        """Record which IaC template resolution path was selected."""
        self._submit(
            self._put_metric,
            "IaCTemplatePath",
            1,
            "Count",
            [
                {"Name": "Path", "Value": path},
                {"Name": "TenantId", "Value": tenant_id},
            ],
        )

    def record_validation_result(
        self, passed: bool, fix_attempts: int, path: str, tenant_id: str = "default"
    ) -> None:
        """Record IaC validation outcome and fix attempt count."""
        self._submit(
            self._put_metric,
            "IaCValidationPassed",
            1 if passed else 0,
            "Count",
            [
                {"Name": "Path", "Value": path},
                {"Name": "TenantId", "Value": tenant_id},
            ],
        )
        self._submit(
            self._put_metric,
            "IaCFixAttempts",
            fix_attempts,
            "Count",
            [
                {"Name": "Path", "Value": path},
                {"Name": "TenantId", "Value": tenant_id},
            ],
        )

    def record_layer_count(
        self, layer_count: int, pattern_name: str, tenant_id: str = "default"
    ) -> None:
        """Record how many layers were generated for a template."""
        self._submit(
            self._put_metric,
            "IaCLayerCount",
            layer_count,
            "Count",
            [
                {"Name": "Pattern", "Value": pattern_name},
                {"Name": "TenantId", "Value": tenant_id},
            ],
        )

    def record_layer_plan_cache(
        self, hit: bool, pattern_name: str, tenant_id: str = "default"
    ) -> None:
        """Record layer plan cache hit/miss."""
        self._submit(
            self._put_metric,
            "IaCLayerPlanCacheHit",
            1 if hit else 0,
            "Count",
            [
                {"Name": "Pattern", "Value": pattern_name},
                {"Name": "TenantId", "Value": tenant_id},
            ],
        )

    def record_validation_layer_errors(
        self, layer: str, error_count: int, tenant_id: str = "default"
    ) -> None:
        """Record per-layer error counts for IaC validation observability."""
        self._submit(
            self._put_metric,
            "IaCValidationLayerErrors",
            error_count,
            "Count",
            [
                {"Name": "Layer", "Value": layer},
                {"Name": "TenantId", "Value": tenant_id},
            ],
        )


metrics = MetricsPublisher()
