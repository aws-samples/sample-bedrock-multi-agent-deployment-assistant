"""Project storage factory."""

import functools

from src.storage.protocol import ProjectStore

_override_store: ProjectStore | None = None


def set_store_override(store: ProjectStore | None) -> None:
    """Override the store singleton (for testing only)."""
    global _override_store
    _override_store = store
    get_store.cache_clear()


@functools.cache
def get_store() -> ProjectStore:
    """Return the configured project store (cached singleton)."""
    if _override_store is not None:
        return _override_store

    from src.storage.dynamo_s3 import DynamoS3ProjectStore

    return DynamoS3ProjectStore()
