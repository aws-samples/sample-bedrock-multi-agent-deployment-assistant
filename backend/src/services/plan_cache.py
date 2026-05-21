"""Interview plan cache — in-memory with S3 persistence.

Active plans are kept in a thread-safe dict for fast access.  Each plan
is also written to S3 so sessions survive server restarts.  Stale entries
are evicted based on TTL.
"""

import logging
import threading
import time

from src.config.settings import settings
from src.models.interview_plan import QuestionPlan
from src.utils.validation import validate_safe_id

logger = logging.getLogger(__name__)


class PlanCache:
    """Thread-safe plan cache with TTL eviction and persistent storage."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[QuestionPlan, float]] = {}  # session_id → (plan, last_access)
        self._lock = threading.Lock()

    # --- public interface ---

    def get(self, session_id: str) -> QuestionPlan | None:
        """Return the plan for a session, checking memory then persistent storage."""
        with self._lock:
            self._evict_stale()
            entry = self._cache.get(session_id)
            if entry:
                plan, _ = entry
                self._cache[session_id] = (plan, time.monotonic())
                return plan

            # Cache miss — load from S3 inside the lock to prevent concurrent
            # load+save from overwriting a fresh save with stale S3 data.
            plan = self._load_persistent(session_id)
            if plan:
                self._cache[session_id] = (plan, time.monotonic())
            return plan

    def save(self, session_id: str, plan: QuestionPlan) -> None:
        """Write plan to both memory cache and persistent storage."""
        with self._lock:
            self._cache[session_id] = (plan, time.monotonic())
        self._save_persistent(session_id, plan)

    def delete(self, session_id: str) -> None:
        """Remove plan from cache and persistent storage."""
        with self._lock:
            self._cache.pop(session_id, None)
        self._delete_persistent(session_id)

    # --- TTL eviction ---

    def _evict_stale(self) -> None:
        """Remove entries older than TTL. Must be called while holding _lock."""
        ttl_seconds = settings.interview_plan_cache_ttl_minutes * 60
        cutoff = time.monotonic() - ttl_seconds
        stale = [sid for sid, (_, ts) in self._cache.items() if ts < cutoff]
        for sid in stale:
            del self._cache[sid]
        if stale:
            logger.info("Evicted %d stale plan cache entries", len(stale))

    # --- persistent storage ---

    def _load_persistent(self, session_id: str) -> QuestionPlan | None:
        return self._load_s3(session_id)

    def _save_persistent(self, session_id: str, plan: QuestionPlan) -> None:
        self._save_s3(session_id, plan)

    def _delete_persistent(self, session_id: str) -> None:
        self._delete_s3(session_id)

    # --- S3 backend ---

    def _s3_key(self, session_id: str) -> str | None:
        parts = session_id.split("-", 2)
        if len(parts) < 3 or parts[0] != "interview":
            return None
        tenant_id, project_id = parts[1], parts[2]
        try:
            validate_safe_id(tenant_id, "tenant_id")
            validate_safe_id(project_id, "project_id")
        except ValueError:
            return None
        return f"{tenant_id}/{project_id}/state/interview_plan.json"

    def _load_s3(self, session_id: str) -> QuestionPlan | None:
        key = self._s3_key(session_id)
        if not key:
            return None
        try:
            from src.config.aws import aws_client

            obj = aws_client("s3").get_object(
                Bucket=settings.s3_artifacts_bucket, Key=key
            )
            return QuestionPlan.model_validate_json(obj["Body"].read())
        except Exception:
            return None

    def _save_s3(self, session_id: str, plan: QuestionPlan) -> None:
        key = self._s3_key(session_id)
        if not key:
            return
        try:
            from src.config.aws import aws_client, s3_encryption_kwargs

            aws_client("s3").put_object(
                Bucket=settings.s3_artifacts_bucket,
                Key=key,
                Body=plan.model_dump_json().encode(),
                ContentType="application/json",
                **s3_encryption_kwargs(),
            )
        except Exception:
            logger.warning("Failed to persist plan to S3", exc_info=True)

    def _delete_s3(self, session_id: str) -> None:
        key = self._s3_key(session_id)
        if not key:
            return
        try:
            from src.config.aws import aws_client

            aws_client("s3").delete_object(
                Bucket=settings.s3_artifacts_bucket, Key=key
            )
        except Exception:
            logger.debug("Failed to delete plan from S3", exc_info=True)


# Module-level singleton
plan_cache = PlanCache()
