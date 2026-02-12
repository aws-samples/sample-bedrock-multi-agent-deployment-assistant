"""Unit tests for agent tools.

All tests mock AWS clients and external dependencies so they run
without credentials or network access.
"""

import json
from unittest.mock import MagicMock, patch



# ===================================================================
# kb_search tool
# ===================================================================


class TestKbSearch:
    """Tests for src.tools.kb_search.kb_search."""

    def test_returns_fallback_when_no_knowledge_base(self):
        """kb_search returns fallback message when knowledge_base_id is None."""
        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = None
            mock_settings.aws_region = "us-east-1"
            from src.tools.kb_search import kb_search

            result = kb_search(query="test query")
            assert "not configured" in result.lower()
            assert "built-in" in result.lower()

    def test_returns_fallback_when_knowledge_base_empty(self):
        """kb_search returns fallback message when knowledge_base_id is empty string."""
        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = ""
            mock_settings.aws_region = "us-east-1"
            from src.tools.kb_search import kb_search

            result = kb_search(query="test query")
            assert "not configured" in result.lower()

    @patch("src.tools.kb_search.boto3")
    def test_returns_formatted_results(self, mock_boto3):
        """kb_search formats results from knowledge base."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "FortiGate best practices..."},
                    "score": 0.95,
                    "location": {
                        "s3Location": {"uri": "s3://bucket/docs/best-practices.pdf"}
                    },
                },
                {
                    "content": {"text": "Hub-spoke topology guide..."},
                    "score": 0.88,
                    "location": {
                        "s3Location": {"uri": "s3://bucket/docs/topology.pdf"}
                    },
                },
            ]
        }

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-test-123"
            mock_settings.aws_region = "us-east-1"
            from src.tools.kb_search import kb_search

            result = kb_search(query="FortiGate deployment")

        assert "FortiGate best practices" in result
        assert "Hub-spoke topology guide" in result
        assert "0.95" in result
        assert "---" in result  # separator between results

    @patch("src.tools.kb_search.boto3")
    def test_returns_no_results_message(self, mock_boto3):
        """kb_search returns appropriate message when no results found."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-test-123"
            mock_settings.aws_region = "us-east-1"
            from src.tools.kb_search import kb_search

            result = kb_search(query="nonexistent topic")

        assert "no results" in result.lower()

    @patch("src.tools.kb_search.boto3")
    def test_uses_correct_max_results(self, mock_boto3):
        """kb_search passes max_results to the API."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-test-123"
            mock_settings.aws_region = "us-east-1"
            from src.tools.kb_search import kb_search

            kb_search(query="test", max_results=3)

        call_kwargs = mock_client.retrieve.call_args[1]
        vector_config = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
        assert vector_config["numberOfResults"] == 3

    @patch("src.tools.kb_search.boto3")
    def test_handles_missing_source_location(self, mock_boto3):
        """kb_search handles results without s3Location gracefully."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Some content"},
                    "score": 0.9,
                    "location": {},
                }
            ]
        }

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-test-123"
            mock_settings.aws_region = "us-east-1"
            from src.tools.kb_search import kb_search

            result = kb_search(query="test")

        assert "Some content" in result
        assert "unknown source" in result

    @patch("src.tools.kb_search.boto3")
    def test_skips_empty_content(self, mock_boto3):
        """kb_search skips results with empty text."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": ""},
                    "score": 0.5,
                    "location": {},
                },
                {
                    "content": {"text": "Valid content"},
                    "score": 0.8,
                    "location": {},
                },
            ]
        }

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-test-123"
            mock_settings.aws_region = "us-east-1"
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

    @patch("src.tools.save_artifact.boto3")
    def test_saves_to_s3_when_configured(self, mock_boto3):
        """save_artifact uploads to S3 when bucket is configured."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch("src.tools.save_artifact.settings") as mock_settings:
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
