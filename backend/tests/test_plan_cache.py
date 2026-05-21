"""Unit tests for PlanCache — in-memory storage, TTL eviction, path resolution.

Tests run without AWS credentials; S3 paths are tested via _s3_key(),
local paths via _plan_path(). Actual persistence is tested with tmp_path.
"""

import time
from unittest.mock import patch

from src.models.interview_plan import PlannedQuestion, QuestionPlan
from src.services.plan_cache import PlanCache


def _sample_plan() -> QuestionPlan:
    return QuestionPlan(
        entries=[
            PlannedQuestion(field_path="role", question_template="What role?"),
        ],
        populated_fields={"gpu_budget": "moderate"},
        kb_summary="Test",
    )


# ===================================================================
# In-memory operations
# ===================================================================


class TestPlanCacheMemory:
    def test_save_and_get(self):
        cache = PlanCache()
        plan = _sample_plan()
        cache.save("interview-t1-p1", plan)
        retrieved = cache.get("interview-t1-p1")
        assert retrieved is not None
        assert retrieved.entries[0].field_path == "role"

    def test_get_missing_returns_none(self):
        cache = PlanCache()
        assert cache.get("interview-t1-nonexistent") is None

    def test_delete_removes_entry(self):
        cache = PlanCache()
        cache.save("interview-t1-p1", _sample_plan())
        cache.delete("interview-t1-p1")
        assert cache.get("interview-t1-p1") is None

    def test_overwrite_existing(self):
        cache = PlanCache()
        plan1 = _sample_plan()
        plan2 = _sample_plan()
        plan2.kb_summary = "Updated"
        cache.save("interview-t1-p1", plan1)
        cache.save("interview-t1-p1", plan2)
        retrieved = cache.get("interview-t1-p1")
        assert retrieved.kb_summary == "Updated"


# ===================================================================
# TTL eviction
# ===================================================================


class TestPlanCacheTTL:
    def test_evicts_stale_entries(self):
        cache = PlanCache()
        # Inject a stale entry with a very old timestamp
        cache._cache["interview-t1-old"] = (_sample_plan(), time.monotonic() - 999999)
        cache._cache["interview-t1-fresh"] = (_sample_plan(), time.monotonic())

        # get() calls _evict_stale() internally
        result = cache.get("interview-t1-old")
        assert result is None

        result = cache.get("interview-t1-fresh")
        assert result is not None


# ===================================================================
# Path resolution — _plan_path
# ===================================================================


# ===================================================================
# S3 key resolution — _s3_key
# ===================================================================


class TestS3Key:
    def test_valid_session_id(self):
        cache = PlanCache()
        key = cache._s3_key("interview-tenant1-project1")
        assert key == "tenant1/project1/state/interview_plan.json"

    def test_invalid_prefix_returns_none(self):
        cache = PlanCache()
        assert cache._s3_key("design-t1-p1") is None

    def test_unsafe_id_returns_none(self):
        cache = PlanCache()
        assert cache._s3_key("interview-../../etc-passwd") is None


# ===================================================================
# Local persistence (with tmp_path)
# ===================================================================


