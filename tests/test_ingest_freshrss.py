"""FreshRSS sources.

FreshRSS publishes categories as ordinary Atom, so the value here is proving
that its real feed shape maps correctly and that credentials are applied and
never leaked -- not re-testing feedparser.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from vocast.ingest.adapters import build_adapter, supported_kinds
from vocast.ingest.adapters.freshrss import FreshRSSAdapter
from vocast.ingest.models import Source
from vocast.ingest.nethttp import Response, redact_headers

# Shaped after a real FreshRSS "user query" feed: Atom, a FreshRSS-owned <id>
# per entry, and an alternate link pointing at the original publisher.
FRESHRSS_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>FreshRSS - Tech</title>
  <link rel="self" href="https://freshrss.example.com/i/?a=rss&amp;get=c_1"/>
  <link rel="alternate" href="https://freshrss.example.com/i/"/>
  <updated>2025-06-05T08:00:00Z</updated>
  <entry>
    <title>Understanding SQLite WAL</title>
    <author><name>Ada Lovelace</name></author>
    <link rel="alternate" href="https://blog.example.org/sqlite-wal"/>
    <id>https://freshrss.example.com/?entry=17592186044417</id>
    <updated>2025-06-04T12:00:00Z</updated>
    <published>2025-06-04T11:00:00Z</published>
    <summary type="html">&lt;p&gt;A look at write-ahead logging.&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>Feed Readers In 2025</title>
    <link rel="alternate" href="https://other.example.net/feed-readers"/>
    <id>https://freshrss.example.com/?entry=17592186044418</id>
    <published>2025-06-05T07:30:00Z</published>
  </entry>
</feed>
"""


def _source(**config) -> Source:
    return Source(
        id=7,
        name="FreshRSS Tech",
        kind="freshrss_feed",
        url="https://freshrss.example.com/i/?a=rss&get=c_1&token=abc",
        enabled=True,
        poll_interval_minutes=15,
        config=config,
        last_checked_at=None,
        last_success_at=None,
        last_error=None,
        created_at=None,
        updated_at=None,
    )


def _adapter(source: Source, body: str = FRESHRSS_ATOM):
    calls: list[dict] = []

    def fetcher(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return Response(url=url, status=200, body=body.encode("utf-8"))

    return FreshRSSAdapter(source, fetcher=fetcher), calls


# --- registration ----------------------------------------------------------


def test_freshrss_feed_is_a_supported_kind():
    assert "freshrss_feed" in supported_kinds()


def test_factory_builds_the_freshrss_adapter():
    adapter = build_adapter(_source(), fetcher=lambda url, **kw: None)
    assert isinstance(adapter, FreshRSSAdapter)


def test_unknown_kind_names_the_supported_ones():
    """A typo, or the not-yet-built API kind, must be reported not ignored."""
    bad = replace(_source(), kind="freshrss_api")
    with pytest.raises(ValueError, match="freshrss_feed"):
        build_adapter(bad)


# --- parsing ---------------------------------------------------------------


def test_entries_link_to_the_original_publisher_not_freshrss():
    """The article URL must be the publisher's, or we would narrate FreshRSS."""
    adapter, _ = _adapter(_source())

    urls = [e.article_url for e in adapter.fetch_entries()]

    assert urls == [
        "https://blog.example.org/sqlite-wal",
        "https://other.example.net/feed-readers",
    ]


def test_freshrss_entry_id_is_used_as_the_dedup_guid():
    adapter, _ = _adapter(_source())
    first = adapter.fetch_entries()[0]
    assert first.external_guid == "https://freshrss.example.com/?entry=17592186044417"


def test_published_date_is_normalized_to_utc():
    adapter, _ = _adapter(_source())
    first = adapter.fetch_entries()[0]
    assert first.published_at == datetime(2025, 6, 4, 11, 0, tzinfo=timezone.utc)


def test_author_and_summary_are_carried_through():
    adapter, _ = _adapter(_source())
    first = adapter.fetch_entries()[0]
    assert first.author == "Ada Lovelace"
    assert "write-ahead logging" in first.summary


def test_entry_without_an_author_is_still_accepted():
    adapter, _ = _adapter(_source())
    assert adapter.fetch_entries()[1].author is None


def test_source_id_is_attached_so_entries_are_scoped_to_the_source():
    adapter, _ = _adapter(_source())
    assert {e.source_id for e in adapter.fetch_entries()} == {7}


def test_polling_the_same_feed_twice_yields_identical_guids():
    """Stable guids are what make deduplication work across polls."""
    adapter, _ = _adapter(_source())
    first = [e.external_guid for e in adapter.fetch_entries()]
    second = [e.external_guid for e in adapter.fetch_entries()]
    assert first == second


# --- authentication --------------------------------------------------------


def test_token_in_the_url_needs_no_extra_configuration():
    adapter, calls = _adapter(_source())
    adapter.fetch_entries()

    assert calls[0]["url"].endswith("token=abc")
    assert "Authorization" not in calls[0]["headers"]


def test_basic_auth_is_sent_when_credentials_are_configured():
    adapter, calls = _adapter(_source(username="ada", password="hunter2"))
    adapter.fetch_entries()

    assert calls[0]["headers"]["Authorization"] == "Basic YWRhOmh1bnRlcjI="


def test_prebuilt_authorization_header_is_honored():
    adapter, calls = _adapter(_source(headers={"Authorization": "Basic cHJlYnVpbHQ="}))
    adapter.fetch_entries()

    assert calls[0]["headers"]["Authorization"] == "Basic cHJlYnVpbHQ="


def test_self_hosted_instance_on_the_lan_requires_an_opt_in():
    adapter, calls = _adapter(_source(allow_private_urls=True))
    adapter.fetch_entries()

    assert calls[0]["policy"].allow_private is True


def test_credentials_are_redacted_before_logging():
    adapter, calls = _adapter(_source(username="ada", password="hunter2"))
    adapter.fetch_entries()

    redacted = redact_headers(calls[0]["headers"])
    assert redacted["Authorization"] == "<redacted>"
    assert "hunter2" not in str(redacted)
