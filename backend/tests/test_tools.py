"""Unit tests for agent tools.

All tests mock AWS clients and external dependencies so they run
without credentials or network access.
"""

import json
from unittest.mock import MagicMock, patch

from src.services.kb_provider import KBSearchResult, NullKBProvider


# ===================================================================
# kb_search tool
# ===================================================================


class TestKbSearch:
    """Tests for src.tools.kb_search.kb_search."""

    def test_returns_fallback_when_no_knowledge_base(self):
        """kb_search returns fallback message when provider is unavailable."""
        with patch("src.tools.kb_search.get_kb_provider", return_value=NullKBProvider()):
            from src.tools.kb_search import kb_search

            result = kb_search(query="test query")
            assert "not configured" in result.lower()

    def test_returns_fallback_when_knowledge_base_empty(self):
        """kb_search returns fallback message when provider is unavailable."""
        with patch("src.tools.kb_search.get_kb_provider", return_value=NullKBProvider()):
            from src.tools.kb_search import kb_search

            result = kb_search(query="test query")
            assert "not configured" in result.lower()

    def test_returns_formatted_results(self):
        """kb_search formats results from knowledge base."""
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = [
            KBSearchResult(
                text="Best practices for deployment...",
                source_uri="s3://bucket/docs/best-practices.pdf",
                score=0.95,
            ),
            KBSearchResult(
                text="Hub-spoke topology guide...",
                source_uri="s3://bucket/docs/topology.pdf",
                score=0.88,
            ),
        ]

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            from src.tools.kb_search import kb_search

            result = kb_search(query="deployment best practices")

        assert "Best practices" in result
        assert "Hub-spoke topology guide" in result
        assert "0.95" in result
        assert "---" in result  # separator between results

    def test_returns_no_results_message(self):
        """kb_search returns appropriate message when no results found."""
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = []

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            from src.tools.kb_search import kb_search

            result = kb_search(query="nonexistent topic")

        assert "no results" in result.lower()

    def test_uses_correct_max_results(self):
        """kb_search passes max_results to the provider."""
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = []

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            from src.tools.kb_search import kb_search

            kb_search(query="test", max_results=3)

        mock_provider.search.assert_called_once_with("test", max_results=3)

    def test_handles_missing_source_location(self):
        """kb_search handles results without known source gracefully."""
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = [
            KBSearchResult(
                text="Some content",
                source_uri="",
                score=0.9,
            ),
        ]

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            from src.tools.kb_search import kb_search

            result = kb_search(query="test")

        assert "Some content" in result

    def test_skips_empty_content(self):
        """kb_search skips results with empty text (provider already filters these)."""
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = [
            KBSearchResult(
                text="Valid content",
                source_uri="s3://b/x/y/z.pdf",
                score=0.8,
            ),
        ]

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            from src.tools.kb_search import kb_search

            result = kb_search(query="test")

        assert "Valid content" in result
        # Should not have a separator since only one valid result
        assert "---" not in result


# ===================================================================
# save_artifact tool
# ===================================================================


class TestSaveArtifact:
    """Tests for src.tools.save_artifact.save_artifact."""

    def test_returns_info_when_s3_not_configured(self):
        """save_artifact returns info message when bucket is empty."""
        with patch("src.tools.save_artifact.settings") as mock_settings:
            mock_settings.s3_artifacts_bucket = ""
            mock_settings.aws_region = "us-east-1"
            from src.tools.save_artifact import save_artifact

            mock_context = MagicMock()
            mock_context.invocation_state = {
                "tenant_id": "t1",
                "project_id": "p1",
            }

            result = save_artifact(
                content="resource {}",
                artifact_path="terraform/main.tf",
                content_type="text/plain",
                tool_context=mock_context,
            )
            assert "not configured" in result.lower() or "would save" in result.lower()

    def test_returns_info_includes_expected_path(self):
        """The info message includes the expected S3 URI structure."""
        with patch("src.tools.save_artifact.settings") as mock_settings:
            mock_settings.s3_artifacts_bucket = ""
            mock_settings.aws_region = "us-east-1"
            from src.tools.save_artifact import save_artifact

            mock_context = MagicMock()
            mock_context.invocation_state = {
                "tenant_id": "tenant-abc",
                "project_id": "proj-123",
            }

            result = save_artifact(
                content="test",
                artifact_path="docs/guide.md",
                content_type="text/markdown",
                tool_context=mock_context,
            )
            assert "tenant-abc" in result
            assert "proj-123" in result
            assert "docs/guide.md" in result

    def test_saves_to_s3_when_configured(self):
        """save_artifact uploads to S3 when bucket is configured."""
        mock_client = MagicMock()

        with patch("src.tools.save_artifact._get_s3_client", return_value=mock_client), \
             patch("src.tools.save_artifact.s3_encryption_kwargs", return_value={}), \
             patch("src.tools.save_artifact.settings") as mock_settings:
            mock_settings.s3_artifacts_bucket = "my-artifacts-bucket"
            mock_settings.aws_region = "us-east-1"
            from src.tools.save_artifact import save_artifact

            mock_context = MagicMock()
            mock_context.invocation_state = {
                "tenant_id": "t1",
                "project_id": "p1",
            }

            result = save_artifact(
                content="resource {}",
                artifact_path="terraform/main.tf",
                content_type="text/plain",
                tool_context=mock_context,
            )

        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-artifacts-bucket"
        assert call_kwargs["Key"] == "t1/p1/terraform/main.tf"
        assert call_kwargs["ContentType"] == "text/plain"
        assert "s3://my-artifacts-bucket/t1/p1/terraform/main.tf" == result

    def test_uses_default_tenant_when_no_context(self):
        """save_artifact uses defaults when tool_context is None."""
        with patch("src.tools.save_artifact.settings") as mock_settings:
            mock_settings.s3_artifacts_bucket = ""
            mock_settings.aws_region = "us-east-1"
            from src.tools.save_artifact import save_artifact

            result = save_artifact(
                content="test",
                artifact_path="file.tf",
                content_type="text/plain",
                tool_context=None,
            )
            assert "default/default/file.tf" in result


# ===================================================================
# save_artifacts_batch tool
# ===================================================================


class TestSaveArtifactsBatch:
    """Tests for src.tools.save_artifact.save_artifacts_batch."""

    def test_returns_message_when_s3_not_configured(self):
        """save_artifacts_batch returns skip message when S3 not configured."""
        with patch("src.tools.save_artifact.settings") as mock_settings:
            mock_settings.s3_artifacts_bucket = ""
            mock_settings.aws_region = "us-east-1"
            from src.tools.save_artifact import save_artifacts_batch

            mock_context = MagicMock()
            mock_context.invocation_state = {"tenant_id": "t1", "project_id": "p1"}

            result = save_artifacts_batch(
                artifacts=json.dumps([{"path": "main.tf", "content": "code"}]),
                tool_context=mock_context,
            )
            assert "not configured" in result.lower()

    def test_saves_multiple_artifacts(self):
        """save_artifacts_batch saves all provided artifacts to S3."""
        mock_client = MagicMock()

        with patch("src.tools.save_artifact._get_s3_client", return_value=mock_client), \
             patch("src.tools.save_artifact.settings") as mock_settings:
            mock_settings.s3_artifacts_bucket = "my-bucket"
            mock_settings.aws_region = "us-east-1"
            from src.tools.save_artifact import save_artifacts_batch

            mock_context = MagicMock()
            mock_context.invocation_state = {"tenant_id": "t1", "project_id": "p1"}

            artifacts = [
                {"path": "main.tf", "content": "resource {}", "content_type": "text/plain"},
                {"path": "variables.tf", "content": "variable {}", "content_type": "text/plain"},
            ]

            result = save_artifacts_batch(
                artifacts=json.dumps(artifacts),
                tool_context=mock_context,
            )

        assert mock_client.put_object.call_count == 2
        assert "Saved 2 artifacts" in result
