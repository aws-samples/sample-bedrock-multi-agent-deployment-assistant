"""Unit tests for kb_search_filtered, filter construction, and metadata extraction.

Tests the provider abstraction layer and the kb_search tool interface.
"""

from unittest.mock import MagicMock, patch

from src.services.kb_provider import (
    BedrockKBProvider,
    KBSearchResult,
    LocalKBProvider,
    NullKBProvider,
    _extract_metadata_from_uri,
)
from src.tools.kb_search import (
    KBResult,
    kb_search_filtered,
)


# ===================================================================
# BedrockKBProvider._build_filter
# ===================================================================


class TestBuildKBFilter:
    def test_no_criteria_returns_none(self):
        assert BedrockKBProvider._build_filter(None, None, None) is None

    def test_single_use_case(self):
        f = BedrockKBProvider._build_filter("realtime-inference", None, None)
        assert f == {"equals": {"key": "use_case", "value": "realtime-inference"}}

    def test_single_document_type_string(self):
        f = BedrockKBProvider._build_filter(None, None, "architecture")
        assert f == {"equals": {"key": "document_type", "value": "architecture"}}

    def test_document_type_list_uses_in_operator(self):
        f = BedrockKBProvider._build_filter(None, None, ["architecture", "components"])
        assert f == {"in": {"key": "document_type", "value": ["architecture", "components"]}}

    def test_multiple_criteria_uses_and_all(self):
        f = BedrockKBProvider._build_filter("realtime-inference", "auto-scaling-fleet", "architecture")
        assert "andAll" in f
        conditions = f["andAll"]
        assert len(conditions) == 3
        keys = [list(c.values())[0]["key"] for c in conditions]
        assert "use_case" in keys
        assert "deployment_type" in keys
        assert "document_type" in keys

    def test_two_criteria_uses_and_all(self):
        f = BedrockKBProvider._build_filter("batch-inference", None, "sizing")
        assert "andAll" in f
        assert len(f["andAll"]) == 2


# ===================================================================
# _extract_metadata_from_uri
# ===================================================================


class TestExtractMetadataFromUri:
    def test_valid_s3_uri(self):
        uri = "s3://my-bucket/realtime-inference/auto-scaling-fleet/architecture.pdf"
        meta = _extract_metadata_from_uri(uri)
        assert meta["use_case"] == "realtime-inference"
        assert meta["deployment_type"] == "auto-scaling-fleet"
        assert meta["document_type"] == "architecture"

    def test_different_extension(self):
        uri = "s3://bucket/training/distributed-training/sizing.md"
        meta = _extract_metadata_from_uri(uri)
        assert meta["use_case"] == "training"
        assert meta["document_type"] == "sizing"

    def test_non_s3_uri_returns_empty(self):
        meta = _extract_metadata_from_uri("https://example.com/doc.pdf")
        assert meta == {}

    def test_malformed_uri_returns_empty(self):
        meta = _extract_metadata_from_uri("not-a-uri")
        assert meta == {}


# ===================================================================
# kb_search_filtered (via provider abstraction)
# ===================================================================


class TestKBSearchFiltered:
    def test_returns_empty_when_provider_unavailable(self):
        with patch("src.tools.kb_search.get_kb_provider") as mock_get:
            mock_get.return_value = NullKBProvider()
            result = kb_search_filtered("test query")
            assert result == []

    def test_returns_kb_results(self):
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = [
            KBSearchResult(
                text="Auto-scaling fleet architecture overview",
                source_uri="s3://bucket/realtime-inference/auto-scaling-fleet/architecture.pdf",
                score=0.92,
                use_case="realtime-inference",
                deployment_type="auto-scaling-fleet",
                document_type="architecture",
            ),
        ]

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            results = kb_search_filtered("realtime-inference architecture", use_case="realtime-inference")

        assert len(results) == 1
        assert isinstance(results[0], KBResult)
        assert results[0].text == "Auto-scaling fleet architecture overview"
        assert results[0].score == 0.92
        assert results[0].use_case == "realtime-inference"
        assert results[0].deployment_type == "auto-scaling-fleet"

    def test_passes_filter_params_to_provider(self):
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = []

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            kb_search_filtered(
                "test",
                use_case="batch-inference",
                document_type=["architecture", "components"],
                max_results=3,
            )

        mock_provider.search.assert_called_once_with(
            "test",
            max_results=3,
            use_case="batch-inference",
            deployment_type=None,
            document_type=["architecture", "components"],
        )

    def test_no_filter_when_no_metadata(self):
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.search.return_value = []

        with patch("src.tools.kb_search.get_kb_provider", return_value=mock_provider):
            kb_search_filtered("generic query")

        mock_provider.search.assert_called_once_with(
            "generic query",
            max_results=5,
            use_case=None,
            deployment_type=None,
            document_type=None,
        )


# ===================================================================
# BedrockKBProvider integration (mocked boto3)
# ===================================================================


class TestBedrockKBProvider:
    @patch("src.services.kb_provider.boto3")
    def test_search_calls_bedrock_api(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Valid content"},
                    "score": 0.8,
                    "location": {"s3Location": {"uri": "s3://b/x/y/z.pdf"}},
                },
            ]
        }

        provider = BedrockKBProvider("kb-123", "us-east-1")
        results = provider.search("test query", max_results=3)

        assert len(results) == 1
        assert results[0].text == "Valid content"
        assert results[0].score == 0.8

    @patch("src.services.kb_provider.boto3")
    def test_skips_empty_text_results(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {"content": {"text": ""}, "score": 0.5, "location": {}},
                {
                    "content": {"text": "Valid content"},
                    "score": 0.8,
                    "location": {"s3Location": {"uri": "s3://b/x/y/z.pdf"}},
                },
            ]
        }

        provider = BedrockKBProvider("kb-123", "us-east-1")
        results = provider.search("test")

        assert len(results) == 1
        assert results[0].text == "Valid content"

    @patch("src.services.kb_provider.boto3")
    def test_passes_filter_to_bedrock(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {"retrievalResults": []}

        provider = BedrockKBProvider("kb-123", "us-east-1")
        provider.search("test", use_case="batch-inference", document_type=["architecture", "components"])

        call_kwargs = mock_client.retrieve.call_args[1]
        search_config = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
        assert "filter" in search_config
        f = search_config["filter"]
        assert "andAll" in f
