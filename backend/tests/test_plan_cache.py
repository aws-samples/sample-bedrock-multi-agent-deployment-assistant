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


class TestPlanPathResolution:
    def test_valid_session_id(self):
        cache = PlanCache()
        path = cache._plan_path("interview-tenant1-project1")
        assert path is not None
        assert "tenant1" in str(path)
        assert "project1" in str(path)
        assert str(path).endswith("interview_plan.json")

    def test_invalid_prefix_returns_none(self):
        cache = PlanCache()
        assert cache._plan_path("design-t1-p1") is None

    def test_too_few_parts_returns_none(self):
        cache = PlanCache()
        assert cache._plan_path("interview-onlyone") is None

    def test_path_traversal_returns_none(self):
        cache = PlanCache()
        assert cache._plan_path("interview-../etc-passwd") is None

    def test_unsafe_id_returns_none(self):
        cache = PlanCache()
        assert cache._plan_path("interview-t1;drop-p1") is None


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


class TestPlanCacheLocalPersistence:
    def test_save_and_load_local(self, tmp_path):
        cache = PlanCache()
        plan = _sample_plan()

        with patch.object(cache, "_plan_path", return_value=tmp_path / "t1" / "p1" / "interview_plan.json"):
            cache._save_local("interview-t1-p1", plan)
            loaded = cache._load_local("interview-t1-p1")

        assert loaded is not None
        assert loaded.entries[0].field_path == "role"
        assert loaded.populated_fields["gpu_budget"] == "moderate"

    def test_load_local_missing_returns_none(self, tmp_path):
        cache = PlanCache()
        with patch.object(cache, "_plan_path", return_value=tmp_path / "nonexistent" / "plan.json"):
            assert cache._load_local("interview-t1-p1") is None

    def test_delete_local_removes_file(self, tmp_path):
        cache = PlanCache()
        plan_file = tmp_path / "plan.json"
        plan_file.write_text(_sample_plan().model_dump_json())

        with patch.object(cache, "_plan_path", return_value=plan_file):
            cache._delete_local("interview-t1-p1")

        assert not plan_file.exists()

    def test_delete_local_missing_file_no_error(self, tmp_path):
        cache = PlanCache()
        with patch.object(cache, "_plan_path", return_value=tmp_path / "nonexistent.json"):
            cache._delete_local("interview-t1-p1")  # should not raise
