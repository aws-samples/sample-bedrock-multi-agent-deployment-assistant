"""Unit tests for kb_search_filtered, filter construction, and metadata extraction.

All tests mock boto3 so they run without AWS credentials.
"""

from unittest.mock import MagicMock, patch

from src.tools.kb_search import (
    KBResult,
    _build_kb_filter,
    _extract_metadata_from_uri,
    kb_search_filtered,
)


# ===================================================================
# _build_kb_filter
# ===================================================================


class TestBuildKBFilter:
    def test_no_criteria_returns_none(self):
        assert _build_kb_filter(None, None, None) is None

    def test_single_use_case(self):
        f = _build_kb_filter("sd-wan", None, None)
        assert f == {"equals": {"key": "use_case", "value": "sd-wan"}}

    def test_single_document_type_string(self):
        f = _build_kb_filter(None, None, "architecture")
        assert f == {"equals": {"key": "document_type", "value": "architecture"}}

    def test_document_type_list_uses_in_operator(self):
        f = _build_kb_filter(None, None, ["architecture", "components"])
        assert f == {"in": {"key": "document_type", "value": ["architecture", "components"]}}

    def test_multiple_criteria_uses_and_all(self):
        f = _build_kb_filter("sd-wan", "hub-spoke", "architecture")
        assert "andAll" in f
        conditions = f["andAll"]
        assert len(conditions) == 3
        keys = [list(c.values())[0]["key"] for c in conditions]
        assert "use_case" in keys
        assert "deployment_type" in keys
        assert "document_type" in keys

    def test_two_criteria_uses_and_all(self):
        f = _build_kb_filter("egress", None, "sizing")
        assert "andAll" in f
        assert len(f["andAll"]) == 2


# ===================================================================
# _extract_metadata_from_uri
# ===================================================================


class TestExtractMetadataFromUri:
    def test_valid_s3_uri(self):
        uri = "s3://my-bucket/sd-wan/hub-spoke/architecture.pdf"
        meta = _extract_metadata_from_uri(uri)
        assert meta["use_case"] == "sd-wan"
        assert meta["deployment_type"] == "hub-spoke"
        assert meta["document_type"] == "architecture"

    def test_different_extension(self):
        uri = "s3://bucket/inspection/centralized/sizing.md"
        meta = _extract_metadata_from_uri(uri)
        assert meta["use_case"] == "inspection"
        assert meta["document_type"] == "sizing"

    def test_non_s3_uri_returns_empty(self):
        meta = _extract_metadata_from_uri("https://example.com/doc.pdf")
        assert meta == {}

    def test_malformed_uri_returns_empty(self):
        meta = _extract_metadata_from_uri("not-a-uri")
        assert meta == {}


# ===================================================================
# kb_search_filtered
# ===================================================================


class TestKBSearchFiltered:
    def test_returns_empty_when_kb_not_configured(self):
        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = ""
            result = kb_search_filtered("test query")
            assert result == []

    @patch("src.tools.kb_search.boto3")
    def test_returns_kb_results(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Hub-spoke architecture overview"},
                    "score": 0.92,
                    "location": {
                        "s3Location": {"uri": "s3://bucket/sd-wan/hub-spoke/architecture.pdf"}
                    },
                },
            ]
        }

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-123"
            mock_settings.aws_region = "us-east-1"
            results = kb_search_filtered("sd-wan architecture", use_case="sd-wan")

        assert len(results) == 1
        assert isinstance(results[0], KBResult)
        assert results[0].text == "Hub-spoke architecture overview"
        assert results[0].score == 0.92
        assert results[0].use_case == "sd-wan"
        assert results[0].deployment_type == "hub-spoke"

    @patch("src.tools.kb_search.boto3")
    def test_passes_filter_to_api(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-123"
            mock_settings.aws_region = "us-east-1"
            kb_search_filtered(
                "test",
                use_case="egress",
                document_type=["architecture", "components"],
            )

        call_kwargs = mock_client.retrieve.call_args[1]
        search_config = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
        assert "filter" in search_config
        f = search_config["filter"]
        assert "andAll" in f

    @patch("src.tools.kb_search.boto3")
    def test_no_filter_when_no_metadata(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-123"
            mock_settings.aws_region = "us-east-1"
            kb_search_filtered("generic query")

        call_kwargs = mock_client.retrieve.call_args[1]
        search_config = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
        assert "filter" not in search_config

    @patch("src.tools.kb_search.boto3")
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

        with patch("src.tools.kb_search.settings") as mock_settings:
            mock_settings.knowledge_base_id = "kb-123"
            mock_settings.aws_region = "us-east-1"
            results = kb_search_filtered("test")

        assert len(results) == 1
        assert results[0].text == "Valid content"
