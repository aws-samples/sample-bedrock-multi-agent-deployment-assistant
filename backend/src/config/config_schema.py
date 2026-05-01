"""Minimal product config schema — the only hand-edited config file.

Defines the product identity and optional policy overrides. The Knowledge Base ID
and local KB path are the bridge between this config and the catalog discovery agent.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ProductConfig(BaseModel):
    name: str = Field(description="Product display name (e.g., 'NovaMind Inference Server')")
    vendor: str = Field(description="Vendor name (e.g., 'NovaMind AI')")


class KnowledgeBaseConfig(BaseModel):
    id: str | None = Field(default=None, description="Bedrock KB ID (production)")
    local_path: str | None = Field(
        default=None,
        description="Path to local knowledge-base directory (local dev fallback)",
    )


class ValidationOverrides(BaseModel):
    cfn_guard_rules_file: str | None = None
    checkov_skip: list[str] = Field(default_factory=list)
    checkov_skip_rationale: dict[str, str] = Field(default_factory=dict)


class Overrides(BaseModel):
    validation: ValidationOverrides = Field(default_factory=ValidationOverrides)


class AppConfig(BaseModel):
    """Root schema for config.yaml."""

    product: ProductConfig
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)
    overrides: Overrides = Field(default_factory=Overrides)


def load_app_config(config_path: Path | None = None) -> AppConfig:
    """Load and validate config.yaml from the given path or project root."""
    if config_path is None:
        config_path = Path(__file__).parent.parent.parent.parent / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. "
            "Create one from config.yaml.example or run `ai-deploy catalog init`."
        )

    raw: dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    return AppConfig.model_validate(raw)
