"""Source adapters — turn an upstream feed into normalized FeedEntry values.

Everything protocol-specific stays behind the `SourceAdapter` interface so the
poller, queue, and worker never learn what kind of source an article came from.
"""

from __future__ import annotations

from ..models import FeedEntry, Source, SourceKind
from .base import FeedParseError, SourceAdapter
from .freshrss import FreshRSSAdapter
from .freshrss_api import FreshRSSAPIAdapter
from .rss import GenericRSSAdapter

_ADAPTERS: dict[str, type] = {
    SourceKind.RSS.value: GenericRSSAdapter,
    SourceKind.FRESHRSS_FEED.value: FreshRSSAdapter,
    SourceKind.FRESHRSS_API.value: FreshRSSAPIAdapter,
}


def supported_kinds() -> list[str]:
    return sorted(_ADAPTERS)


def build_adapter(source: Source, **kwargs) -> SourceAdapter:
    """Construct the adapter for a source's kind.

    Raises ValueError for an unknown kind so a typo in the config file is
    reported instead of silently skipping the source.
    """
    try:
        adapter_class = _ADAPTERS[source.kind]
    except KeyError:
        raise ValueError(
            f"unknown source kind {source.kind!r} for source {source.id} "
            f"({source.name}); supported kinds: {', '.join(supported_kinds())}"
        ) from None
    return adapter_class(source, **kwargs)


__all__ = [
    "FeedEntry",
    "FeedParseError",
    "FreshRSSAPIAdapter",
    "FreshRSSAdapter",
    "GenericRSSAdapter",
    "SourceAdapter",
    "build_adapter",
    "supported_kinds",
]
