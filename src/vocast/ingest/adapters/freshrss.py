"""FreshRSS adapter.

FreshRSS can publish any category or feed as a normal RSS/Atom document
("Manage > Categories > RSS feed", giving a URL such as
`https://freshrss.example.com/i/?a=rss&get=c_1&token=...`). Consuming that
document needs no FreshRSS-specific parsing at all, so this adapter is a thin
specialization of the generic one: it exists to give the source kind a name, to
document how FreshRSS auth is configured, and to keep a seam for the richer API.

Authentication, in order of preference:

1. A user token embedded in the generated feed URL (`&token=...`). This is what
   FreshRSS itself suggests and needs no extra configuration here.
2. HTTP Basic Auth, when the instance sits behind a protected reverse proxy —
   set `username`/`password`, or pass a pre-built `Authorization` header.

Extension point — Google Reader compatible API
----------------------------------------------
FreshRSS also exposes a Google Reader API at `/api/greader.php`, which would
add unread-only filtering and category listing. It needs a ClientLogin token
exchange plus paginated `stream/contents` calls with `continuation` handling —
materially more code and more failure modes than reading a feed document.

It is deliberately not implemented. To add it later, introduce a
`freshrss_api` source kind with its own adapter class and register it in
`vocast.ingest.adapters._ADAPTERS`. Nothing outside this package needs to
change, because the rest of the service only sees `FeedEntry` values.
"""

from __future__ import annotations

from .rss import GenericRSSAdapter


class FreshRSSAdapter(GenericRSSAdapter):
    """Reads a FreshRSS-generated RSS/Atom feed.

    FreshRSS emits a stable `<guid>` per article and links each item to the
    original publisher URL, which is exactly what the pipeline needs: the guid
    deduplicates and the link is what gets extracted and narrated.
    """
