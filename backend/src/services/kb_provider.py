"""Knowledge Base provider abstraction — Bedrock (production) or Local (development).

The provider protocol defines the interface for KB search operations. At startup,
the active provider is selected based on configuration:
- If knowledge_base.id is set → BedrockKBProvider (AWS API calls)
- If knowledge_base.local_path is set → LocalKBProvider (local file search)
- If neither → NullKBProvider (graceful no-op)

The local provider reads documents from a directory structured identically to S3:
  knowledge-base/
    sd-wan/
      hub-spoke/
        architecture.md
        sizing.md
      dual-hub/
        architecture.md
    inspection/
      gwlb/
        architecture.md
        components.md

This means the same metadata extraction logic (use_case/deployment_type/doc_type)
works in both modes, ensuring local development accurately reflects production behavior.
"""

import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

import boto3

from src.config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model (matches existing KBResult contract)
# ---------------------------------------------------------------------------


class KBSearchResult:
    """A single KB search result — provider-agnostic."""

    __slots__ = ("text", "source_uri", "score", "use_case", "deployment_type", "document_type")

    def __init__(
        self,
        text: str,
        source_uri: str,
        score: float = 0.0,
        use_case: str | None = None,
        deployment_type: str | None = None,
        document_type: str | None = None,
    ):
        self.text = text
        self.source_uri = source_uri
        self.score = score
        self.use_case = use_case
        self.deployment_type = deployment_type
        self.document_type = document_type


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class KnowledgeBaseProvider(Protocol):
    """Interface for knowledge base search operations."""

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        use_case: str | None = None,
        deployment_type: str | None = None,
        document_type: str | list[str] | None = None,
    ) -> list[KBSearchResult]:
        """Search the knowledge base with optional metadata filtering."""
        ...

    @property
    def is_available(self) -> bool:
        """Whether this provider is configured and ready."""
        ...


# ---------------------------------------------------------------------------
# Bedrock KB Provider (production)
# ---------------------------------------------------------------------------


class BedrockKBProvider:
    """Production provider — calls AWS Bedrock Knowledge Base APIs."""

    def __init__(self, knowledge_base_id: str, region: str):
        self._kb_id = knowledge_base_id
        self._region = region
        self._client = boto3.client("bedrock-agent-runtime", region_name=region)

    @property
    def is_available(self) -> bool:
        return True

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        use_case: str | None = None,
        deployment_type: str | None = None,
        document_type: str | list[str] | None = None,
    ) -> list[KBSearchResult]:
        kb_filter = self._build_filter(use_case, deployment_type, document_type)
        search_config: dict[str, Any] = {"numberOfResults": max_results}
        if kb_filter:
            search_config["filter"] = kb_filter

        logger.info(
            "BedrockKB search query=%r use_case=%s deployment_type=%s",
            query, use_case, deployment_type,
        )

        response = self._client.retrieve(
            knowledgeBaseId=self._kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": search_config},
        )

        results: list[KBSearchResult] = []
        for r in response.get("retrievalResults", []):
            text = r.get("content", {}).get("text", "")
            if not text:
                continue
            score = r.get("score", 0)
            source = r.get("location", {}).get("s3Location", {}).get("uri", "")
            meta = _extract_metadata_from_uri(source)

            results.append(KBSearchResult(
                text=text,
                source_uri=source,
                score=score,
                use_case=meta.get("use_case"),
                deployment_type=meta.get("deployment_type"),
                document_type=meta.get("document_type"),
            ))

        logger.info("BedrockKB: %d results for query=%r", len(results), query)
        return results

    @staticmethod
    def _build_filter(
        use_case: str | None,
        deployment_type: str | None,
        document_type: str | list[str] | None,
    ) -> dict[str, Any] | None:
        conditions: list[dict] = []
        if use_case:
            conditions.append({"equals": {"key": "use_case", "value": use_case}})
        if deployment_type:
            conditions.append({"equals": {"key": "deployment_type", "value": deployment_type}})
        if document_type:
            if isinstance(document_type, list):
                conditions.append({"in": {"key": "document_type", "value": document_type}})
            else:
                conditions.append({"equals": {"key": "document_type", "value": document_type}})
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"andAll": conditions}


# ---------------------------------------------------------------------------
# Local KB Provider (development)
# ---------------------------------------------------------------------------

_TOKENIZE_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Simple lowercase tokenizer."""
    return _TOKENIZE_RE.findall(text.lower())


def _tfidf_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    """Compute a simple TF-IDF-like relevance score between query and document."""
    if not doc_tokens or not query_tokens:
        return 0.0

    doc_freq = Counter(doc_tokens)
    doc_len = len(doc_tokens)

    score = 0.0
    for token in query_tokens:
        tf = doc_freq.get(token, 0) / doc_len
        if tf > 0:
            # IDF approximation: treat each doc independently (log(2) baseline)
            idf = 1.0 + math.log(1.0 + tf)
            score += tf * idf

    return score


class LocalKBProvider:
    """Development provider — searches local document files using TF-IDF scoring.

    Expects directory structure matching S3 path convention:
      base_path/
        {use_case}/
          {deployment_type}/
            {document_type}.{ext}
    """

    SUPPORTED_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json"}

    def __init__(self, base_path: Path):
        self._base_path = base_path
        self._documents: list[dict[str, Any]] = []
        self._loaded = False

    @property
    def is_available(self) -> bool:
        return self._base_path.exists() and self._base_path.is_dir()

    def _load_documents(self) -> None:
        """Scan the knowledge base directory and index all documents."""
        if self._loaded:
            return

        if not self.is_available:
            logger.warning("Local KB path does not exist: %s", self._base_path)
            self._loaded = True
            return

        count = 0
        for file_path in self._base_path.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
                continue

            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            # Extract metadata from path structure
            rel = file_path.relative_to(self._base_path)
            parts = rel.parts

            use_case = parts[0] if len(parts) > 1 else None
            deployment_type = parts[1] if len(parts) > 2 else None
            document_type = file_path.stem

            source_uri = f"local://{rel}"
            tokens = _tokenize(text)

            self._documents.append({
                "text": text,
                "tokens": tokens,
                "source_uri": source_uri,
                "use_case": use_case,
                "deployment_type": deployment_type,
                "document_type": document_type,
            })
            count += 1

        logger.info("LocalKB: indexed %d documents from %s", count, self._base_path)
        self._loaded = True

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        use_case: str | None = None,
        deployment_type: str | None = None,
        document_type: str | list[str] | None = None,
    ) -> list[KBSearchResult]:
        self._load_documents()

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        # Filter by metadata
        candidates = self._documents
        if use_case:
            candidates = [d for d in candidates if d["use_case"] == use_case]
        if deployment_type:
            candidates = [d for d in candidates if d["deployment_type"] == deployment_type]
        if document_type:
            if isinstance(document_type, list):
                candidates = [d for d in candidates if d["document_type"] in document_type]
            else:
                candidates = [d for d in candidates if d["document_type"] == document_type]

        # Score and rank
        scored: list[tuple[float, dict]] = []
        for doc in candidates:
            score = _tfidf_score(query_tokens, doc["tokens"])
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[KBSearchResult] = []
        for score, doc in scored[:max_results]:
            # Truncate text to a reasonable context window
            text = doc["text"][:2000]
            results.append(KBSearchResult(
                text=text,
                source_uri=doc["source_uri"],
                score=min(score, 1.0),
                use_case=doc["use_case"],
                deployment_type=doc["deployment_type"],
                document_type=doc["document_type"],
            ))

        logger.info(
            "LocalKB search: %d results for query=%r (filtered: use_case=%s, deployment_type=%s)",
            len(results), query, use_case, deployment_type,
        )
        return results


# ---------------------------------------------------------------------------
# Null provider (no KB configured)
# ---------------------------------------------------------------------------


class NullKBProvider:
    """No-op provider when KB is not configured."""

    @property
    def is_available(self) -> bool:
        return False

    def search(self, query: str, **kwargs: Any) -> list[KBSearchResult]:
        return []


# ---------------------------------------------------------------------------
# Provider factory & singleton
# ---------------------------------------------------------------------------

_S3_PATH_RE = re.compile(r"s3://[^/]+/([^/]+)/([^/]+)/([^/]+)\.[^.]+$")


def _extract_metadata_from_uri(uri: str) -> dict[str, str | None]:
    """Best-effort metadata extraction from S3 URI path structure."""
    m = _S3_PATH_RE.search(uri)
    if not m:
        return {}
    return {
        "use_case": m.group(1),
        "deployment_type": m.group(2),
        "document_type": m.group(3),
    }


_provider_instance: KnowledgeBaseProvider | None = None


def get_kb_provider() -> KnowledgeBaseProvider:
    """Get or create the singleton KB provider based on current configuration.

    Priority:
    1. Bedrock KB ID set (env or config.yaml) → BedrockKBProvider
    2. Local path set (env or config.yaml) → LocalKBProvider
    3. Neither → NullKBProvider
    """
    global _provider_instance
    if _provider_instance is not None:
        return _provider_instance

    # Try loading from config.yaml (non-fatal if not present yet during early boot)
    kb_id = settings.knowledge_base_id
    local_path: str | None = settings.knowledge_base_local_path

    try:
        from src.config.config_schema import load_app_config
        app_config = load_app_config()
        if not kb_id and app_config.knowledge_base.id:
            kb_id = app_config.knowledge_base.id
        if not local_path and app_config.knowledge_base.local_path:
            local_path = app_config.knowledge_base.local_path
    except (FileNotFoundError, Exception) as exc:
        logger.debug("config.yaml not loaded for KB provider init: %s", exc)

    if kb_id:
        logger.info("KB provider: Bedrock (id=%s)", kb_id)
        _provider_instance = BedrockKBProvider(kb_id, settings.aws_region)
    elif local_path:
        path = Path(local_path)
        if not path.is_absolute():
            path = Path(__file__).parent.parent.parent.parent / path
        logger.info("KB provider: Local (path=%s)", path)
        _provider_instance = LocalKBProvider(path)
    else:
        logger.info("KB provider: Null (no KB configured)")
        _provider_instance = NullKBProvider()

    return _provider_instance


def reset_kb_provider() -> None:
    """Reset the singleton — used in tests."""
    global _provider_instance
    _provider_instance = None
