"""Interview plan cache — in-memory with persistent fallback.

Active plans are kept in a thread-safe dict for fast access.  Each plan
is also written to persistent storage (local JSON or S3) so sessions
survive server restarts.  Stale entries are evicted based on TTL.
"""

import logging
import threading
import time
from pathlib import Path

from src.config.settings import settings
from src.models.interview_plan import QuestionPlan
from src.utils.validation import validate_safe_id

logger = logging.getLogger(__name__)

_DATA_DIR = Path(".local-data")


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

        # Cache miss — try persistent storage
        plan = self._load_persistent(session_id)
        if plan:
            with self._lock:
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

    def _plan_path(self, session_id: str) -> Path | None:
        """Resolve the local file path for a session's plan.

        Session IDs follow the pattern 'interview-{tenant_id}-{project_id}'.
        """
        parts = session_id.split("-", 2)  # interview-{tenant}-{project}
        if len(parts) < 3 or parts[0] != "interview":
            return None
        tenant_id, project_id = parts[1], parts[2]
        try:
            validate_safe_id(tenant_id, "tenant_id")
            validate_safe_id(project_id, "project_id")
        except ValueError:
            return None
        path = (_DATA_DIR / tenant_id / project_id / "interview_plan.json").resolve()
        if not path.is_relative_to(_DATA_DIR.resolve()):
            return None
        return path

    def _load_persistent(self, session_id: str) -> QuestionPlan | None:
        if settings.storage_backend == "aws":
            return self._load_s3(session_id)
        return self._load_local(session_id)

    def _save_persistent(self, session_id: str, plan: QuestionPlan) -> None:
        if settings.storage_backend == "aws":
            self._save_s3(session_id, plan)
        else:
            self._save_local(session_id, plan)

    def _delete_persistent(self, session_id: str) -> None:
        if settings.storage_backend == "aws":
            self._delete_s3(session_id)
        else:
            self._delete_local(session_id)

    # --- local backend ---

    def _load_local(self, session_id: str) -> QuestionPlan | None:
        path = self._plan_path(session_id)
        if not path or not path.exists():
            return None
        try:
            return QuestionPlan.model_validate_json(path.read_text())
        except Exception:
            logger.warning("Failed to load plan from %s", path, exc_info=True)
            return None

    def _save_local(self, session_id: str, plan: QuestionPlan) -> None:
        path = self._plan_path(session_id)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.model_dump_json(indent=2))

    def _delete_local(self, session_id: str) -> None:
        path = self._plan_path(session_id)
        if path and path.exists():
            path.unlink()

    # --- AWS S3 backend ---

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
            import boto3

            obj = boto3.client("s3", region_name=settings.aws_region).get_object(
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
            import boto3

            boto3.client("s3", region_name=settings.aws_region).put_object(
                Bucket=settings.s3_artifacts_bucket,
                Key=key,
                Body=plan.model_dump_json().encode(),
                ContentType="application/json",
            )
        except Exception:
            logger.warning("Failed to persist plan to S3", exc_info=True)

    def _delete_s3(self, session_id: str) -> None:
        key = self._s3_key(session_id)
        if not key:
            return
        try:
            import boto3

            boto3.client("s3", region_name=settings.aws_region).delete_object(
                Bucket=settings.s3_artifacts_bucket, Key=key
            )
        except Exception:
            logger.debug("Failed to delete plan from S3", exc_info=True)


# Module-level singleton
plan_cache = PlanCache()
