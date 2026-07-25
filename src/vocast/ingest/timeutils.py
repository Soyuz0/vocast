"""UTC-normalized time helpers.

All timestamps are stored in SQLite as ISO-8601 text in UTC so that lexical
ordering matches chronological ordering and comparisons in SQL are valid.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """Return value as an aware UTC datetime, assuming UTC if naive."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return to_utc(value).isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Parse a stored timestamp, tolerating trailing 'Z' and missing offsets.

    Returns None for values that cannot be parsed so that a single corrupt
    row never makes the whole library unreadable.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return to_utc(parsed)
