"""Project storage factory."""

import functools

from src.config.settings import settings
from src.storage.protocol import ProjectStore


@functools.cache
def get_store() -> ProjectStore:
    """Return the configured project store (cached singleton)."""
    if settings.storage_backend == "aws":
        from src.storage.dynamo_s3 import DynamoS3ProjectStore

        return DynamoS3ProjectStore()

    from src.storage.local import LocalProjectStore

    return LocalProjectStore()
