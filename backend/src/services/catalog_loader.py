"""CatalogLoader — singleton service providing typed access to catalog.lock.yaml.

Loaded once at startup, replaces the hardcoded USE_CASE_REGISTRY, UseCases enum,
and other scattered constants. Every agent and service queries the catalog for:
- Available products and their use cases
- Interview field definitions (blocking vs optional)
- Deployment patterns and their layers
- Appliance configuration (interfaces, instance types)
- KB search templates

The catalog is deterministic and version-controlled — the LLM cannot change what
fields exist, only how it phrases questions about them.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from src.config.catalog_schema import (
    ApplianceConfig,
    CatalogLock,
    CatalogProduct,
    FieldDefinition,
    GuardrailsConfig,
    KBSearchConfig,
    PatternConfig,
    UseCaseSchema,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UseCaseSpec equivalent (runtime convenience layer)
# ---------------------------------------------------------------------------


class UseCaseSpec:
    """Runtime representation of a use case — compatible with existing code that uses UseCaseSpec."""

    def __init__(self, schema: UseCaseSchema):
        self._schema = schema

    @property
    def value(self) -> str:
        return self._schema.value

    @property
    def label(self) -> str:
        return self._schema.label

    @property
    def required_fields(self) -> set[str]:
        return {f.name for f in self._schema.required_fields}

    @property
    def optional_fields(self) -> set[str]:
        return {f.name for f in self._schema.optional_fields}

    @property
    def all_fields(self) -> set[str]:
        return self.required_fields | self.optional_fields

    @property
    def field_doc_types(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for f in self._schema.required_fields + self._schema.optional_fields:
            mapping[f.name] = f.doc_type
        return mapping

    def get_field_definition(self, name: str) -> FieldDefinition | None:
        for f in self._schema.required_fields + self._schema.optional_fields:
            if f.name == name:
                return f
        return None

    def get_required_field_definitions(self) -> list[FieldDefinition]:
        return list(self._schema.required_fields)

    def get_optional_field_definitions(self) -> list[FieldDefinition]:
        return list(self._schema.optional_fields)


# ---------------------------------------------------------------------------
# Dynamic Pydantic model generation from field definitions
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "enum": str,
    "list_str": list,
    "object": dict,
}


def build_use_case_model(fields: list[FieldDefinition], model_name: str = "DynamicModel") -> type[BaseModel]:
    """Dynamically create a Pydantic model from catalog field definitions."""
    from pydantic import create_model

    field_definitions: dict[str, Any] = {}
    for f in fields:
        python_type = _TYPE_MAP.get(f.type, str)
        default = f.default if f.default is not None else _default_for_type(f.type)
        field_info = Field(default=default, description=f.description or f.name)
        field_definitions[f.name] = (python_type, field_info)

    return create_model(model_name, **field_definitions)


def _default_for_type(type_str: str) -> Any:
    defaults: dict[str, Any] = {
        "str": "",
        "int": 0,
        "float": 0.0,
        "bool": False,
        "enum": "",
        "list_str": [],
        "object": {},
    }
    return defaults.get(type_str, "")


# ---------------------------------------------------------------------------
# CatalogLoader — the singleton
# ---------------------------------------------------------------------------


class CatalogLoader:
    """Provides typed access to the catalog lock file."""

    def __init__(self, catalog: CatalogLock):
        self._catalog = catalog
        self._products_by_id: dict[str, CatalogProduct] = {
            p.id: p for p in catalog.products
        }
        self._use_case_specs: dict[str, dict[str, UseCaseSpec]] = {}
        self._use_case_models: dict[str, dict[str, type[BaseModel]]] = {}

        for product in catalog.products:
            self._use_case_specs[product.id] = {}
            self._use_case_models[product.id] = {}
            for uc in product.interview.use_cases:
                spec = UseCaseSpec(uc)
                self._use_case_specs[product.id][uc.value] = spec
                all_fields = list(uc.required_fields) + list(uc.optional_fields)
                if all_fields:
                    model = build_use_case_model(
                        all_fields,
                        model_name=f"{product.id}_{uc.value}_Model",
                    )
                    self._use_case_models[product.id][uc.value] = model

    # --- Product access ---

    @property
    def products(self) -> list[CatalogProduct]:
        return self._catalog.products

    def get_product(self, product_id: str) -> CatalogProduct | None:
        return self._products_by_id.get(product_id)

    @property
    def default_product(self) -> CatalogProduct | None:
        return self._catalog.products[0] if self._catalog.products else None

    # --- Use case access ---

    def get_use_cases(self, product_id: str | None = None) -> list[UseCaseSpec]:
        pid = product_id or (self.default_product.id if self.default_product else "")
        specs = self._use_case_specs.get(pid, {})
        return list(specs.values())

    def get_use_case_spec(self, use_case_value: str, product_id: str | None = None) -> UseCaseSpec | None:
        pid = product_id or (self.default_product.id if self.default_product else "")
        return self._use_case_specs.get(pid, {}).get(use_case_value)

    def get_use_case_values(self, product_id: str | None = None) -> list[str]:
        return [uc.value for uc in self.get_use_cases(product_id)]

    def get_use_case_model(self, use_case_value: str, product_id: str | None = None) -> type[BaseModel] | None:
        pid = product_id or (self.default_product.id if self.default_product else "")
        return self._use_case_models.get(pid, {}).get(use_case_value)

    # --- Field access ---

    def get_blocking_base_fields(self, product_id: str | None = None) -> list[FieldDefinition]:
        product = self._get_product_or_default(product_id)
        if not product:
            return []
        return list(product.interview.base_fields.blocking)

    def get_soft_base_fields(self, product_id: str | None = None) -> list[FieldDefinition]:
        product = self._get_product_or_default(product_id)
        if not product:
            return []
        return list(product.interview.base_fields.soft)

    def get_fields_for_use_case(
        self, use_case_value: str, product_id: str | None = None
    ) -> tuple[list[FieldDefinition], list[FieldDefinition]]:
        """Returns (required_fields, optional_fields) for a use case."""
        product = self._get_product_or_default(product_id)
        if not product:
            return [], []
        for uc in product.interview.use_cases:
            if uc.value == use_case_value:
                return list(uc.required_fields), list(uc.optional_fields)
        return [], []

    # --- Pattern access ---

    def get_patterns(self, product_id: str | None = None) -> list[PatternConfig]:
        product = self._get_product_or_default(product_id)
        if not product:
            return []
        return list(product.patterns)

    def resolve_pattern(self, name_or_alias: str, product_id: str | None = None) -> PatternConfig | None:
        """Find a pattern by name or alias."""
        for pattern in self.get_patterns(product_id):
            if pattern.name == name_or_alias or name_or_alias in pattern.aliases:
                return pattern
        return None

    # --- Appliance access ---

    def get_appliance_config(self, product_id: str | None = None) -> ApplianceConfig | None:
        product = self._get_product_or_default(product_id)
        return product.appliance if product else None

    def get_interface_roles(self, product_id: str | None = None) -> dict[str, Any]:
        product = self._get_product_or_default(product_id)
        if not product:
            return {}
        return {name: role.model_dump() for name, role in product.appliance.interface_roles.items()}

    def get_approved_instance_types(self, product_id: str | None = None) -> list[str]:
        product = self._get_product_or_default(product_id)
        if not product:
            return []
        return list(product.appliance.approved_instance_types)

    # --- KB config ---

    @property
    def kb_search_config(self) -> KBSearchConfig:
        return self._catalog.knowledge_base

    def format_search_query(self, use_case: str = "", product_id: str | None = None) -> str:
        """Format a KB search query using the catalog template."""
        product = self._get_product_or_default(product_id)
        product_name = product.name if product else "Unknown"
        return self.kb_search_config.search_template.format(
            product_name=product_name,
            use_case=use_case,
        )

    # --- Prompt template context ---

    def get_prompt_context(self, product_id: str | None = None) -> dict[str, str]:
        """Return template variables for prompt formatting."""
        product = self._get_product_or_default(product_id)
        if not product:
            return {"product_name": "Unknown", "vendor_name": "Unknown"}
        appliance_config = product.appliance
        return {
            "product_name": product.name,
            "vendor_name": product.vendor,
            "interface_naming_pattern": appliance_config.interface_naming,
            "approved_instance_types": ", ".join(appliance_config.approved_instance_types),
        }

    # --- Guardrails ---

    @property
    def guardrails(self) -> GuardrailsConfig:
        return self._catalog.guardrails

    # --- Use case config for API endpoint ---

    def get_use_case_config(self, product_id: str | None = None) -> list[dict]:
        """Return use-case metadata for the /api/config endpoint."""
        specs = self.get_use_cases(product_id)
        return [
            {
                "value": spec.value,
                "label": spec.label,
                "available": True,
                "extra_fields": len(spec.required_fields),
            }
            for spec in specs
        ]

    # --- Internal helpers ---

    def _get_product_or_default(self, product_id: str | None) -> CatalogProduct | None:
        if product_id:
            return self._products_by_id.get(product_id)
        return self.default_product


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_catalog_instance: CatalogLoader | None = None


def load_catalog(lock_path: Path | None = None) -> CatalogLoader:
    """Load the catalog from disk and return the singleton instance."""
    global _catalog_instance

    if lock_path is None:
        lock_path = Path(__file__).parent.parent.parent.parent / "catalog.lock.yaml"

    if not lock_path.exists():
        logger.warning(
            "catalog.lock.yaml not found at %s — using empty catalog. "
            "Run `ai-deploy catalog generate` to create one.",
            lock_path,
        )
        _catalog_instance = CatalogLoader(CatalogLock())
        return _catalog_instance

    raw = yaml.safe_load(lock_path.read_text()) or {}
    catalog = CatalogLock.model_validate(raw)
    _catalog_instance = CatalogLoader(catalog)

    logger.info(
        "Catalog loaded: %d products, %d total use cases",
        len(catalog.products),
        sum(len(p.interview.use_cases) for p in catalog.products),
    )
    return _catalog_instance


def get_catalog() -> CatalogLoader:
    """Get the catalog singleton. Loads from disk if not yet initialized."""
    global _catalog_instance
    if _catalog_instance is None:
        return load_catalog()
    return _catalog_instance


def reset_catalog() -> None:
    """Reset the singleton — used in tests."""
    global _catalog_instance
    _catalog_instance = None
