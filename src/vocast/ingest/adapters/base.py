"""The source adapter contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import FeedEntry


class FeedParseError(Exception):
    """A feed was reachable but could not be understood."""


@runtime_checkable
class SourceAdapter(Protocol):
    """Fetches a source and reports the articles it currently advertises.

    Implementations report *everything* the feed currently lists; deciding what
    is new is the poller's job, backed by a database uniqueness constraint.
    Adapters therefore need no memory of previous polls.
    """

    def fetch_entries(self) -> list[FeedEntry]: ...
