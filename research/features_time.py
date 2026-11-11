"""Shared explicit-UTC timestamp parsing for source feature validation."""
from datetime import datetime, timezone


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('A timezone is required')
    return result.astimezone(timezone.utc)
