"""Shared formatting utilities."""


class PartialFormatMap(dict):
    """Dict that returns '{key}' for missing keys — enables partial .format_map()."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
