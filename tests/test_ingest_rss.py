"""RSS/Atom normalization: identity, dates, relative links, malformed input."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vocast.ingest.adapters.base import FeedParseError
from vocast.ingest.adapters.rss import GenericRSSAdapter
from vocast.ingest.models import Source
from vocast.ingest.nethttp import FetchPolicy, Response

RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Example Blog</title>
  <link>https://example.com/</link>
  {items}
</channel></rss>
"""

ATOM_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Blog</title>
  <link href="https://example.com/"/>
  {items}
</feed>
"""


def _source(url: str = "https://example.com/feed.xml", **config) -> Source:
    return Source(
        id=1,
        name="Example",
        kind="rss",
        url=url,
        enabled=True,
        poll_interval_minutes=15,
        config=config,
        last_checked_at=None,
        last_success_at=None,
        last_error=None,
        created_at=None,
        updated_at=None,
    )


def _adapter(body: str, source: Source | None = None, **fetch_kwargs):
    """Build an adapter whose fetcher returns a canned document."""
    calls: list[dict] = []

    def fetcher(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return Response(url=url, status=200, body=body.encode("utf-8"))

    adapter = GenericRSSAdapter(source or _source(), fetcher=fetcher, **fetch_kwargs)
    return adapter, calls


# --- RSS 2.0 ---------------------------------------------------------------


def test_parses_rss_item():
    body = RSS_TEMPLATE.format(
        items="""
    <item>
      <title>Hello World</title>
      <link>https://example.com/hello</link>
      <guid>urn:uuid:1234</guid>
      <author>ada@example.com</author>
      <description>A summary</description>
      <pubDate>Wed, 04 Jun 2025 12:00:00 GMT</pubDate>
    </item>"""
    )
    adapter, _ = _adapter(body)

    [entry] = adapter.fetch_entries()
    assert entry.source_id == 1
    assert entry.title == "Hello World"
    assert entry.article_url == "https://example.com/hello"
    assert entry.external_guid == "urn:uuid:1234"
    assert entry.published_at == datetime(2025, 6, 4, 12, 0, tzinfo=timezone.utc)


def test_parses_atom_entry():
    body = ATOM_TEMPLATE.format(
        items="""
  <entry>
    <title>Atom Post</title>
    <link href="https://example.com/atom-post"/>
    <id>tag:example.com,2025:1</id>
    <updated>2025-06-04T12:00:00Z</updated>
  </entry>"""
    )
    adapter, _ = _adapter(body)

    [entry] = adapter.fetch_entries()
    assert entry.title == "Atom Post"
    assert entry.article_url == "https://example.com/atom-post"
    assert entry.external_guid == "tag:example.com,2025:1"
    assert entry.published_at == datetime(2025, 6, 4, 12, 0, tzinfo=timezone.utc)


# --- identity --------------------------------------------------------------


def test_falls_back_to_link_when_guid_missing():
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>No Guid</title><link>https://example.com/no-guid</link></item>"""
    )
    adapter, _ = _adapter(body)

    [entry] = adapter.fetch_entries()
    assert entry.external_guid == "https://example.com/no-guid"


def test_duplicate_guids_within_one_response_are_collapsed():
    item = """
    <item><title>Dup</title><link>https://example.com/a</link><guid>same</guid></item>"""
    adapter, _ = _adapter(RSS_TEMPLATE.format(items=item + item))

    assert len(adapter.fetch_entries()) == 1


def test_bare_guid_is_treated_as_a_permalink():
    """RSS 2.0 defaults guid to isPermaLink="true", so it doubles as the URL."""
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>Permalink</title><guid>https://example.com/via-guid</guid></item>"""
    )
    adapter, _ = _adapter(body)

    assert adapter.fetch_entries()[0].article_url == "https://example.com/via-guid"


def test_non_permalink_guid_without_a_link_is_skipped():
    body = RSS_TEMPLATE.format(
        items="""
    <item>
      <title>Unreachable</title>
      <guid isPermaLink="false">abc-123</guid>
    </item>"""
    )
    adapter, _ = _adapter(body)

    assert adapter.fetch_entries() == []


def test_item_without_any_link_is_skipped():
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>Unreachable</title><description>no url anywhere</description></item>"""
    )
    adapter, _ = _adapter(body)

    assert adapter.fetch_entries() == []


# --- optional and malformed fields -----------------------------------------


def test_missing_date_yields_none_rather_than_a_guess():
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>Undated</title><link>https://example.com/undated</link></item>"""
    )
    adapter, _ = _adapter(body)

    assert adapter.fetch_entries()[0].published_at is None


def test_unparseable_date_is_treated_as_missing():
    body = RSS_TEMPLATE.format(
        items="""
    <item>
      <title>Bad Date</title>
      <link>https://example.com/bad-date</link>
      <pubDate>not a date at all</pubDate>
    </item>"""
    )
    adapter, _ = _adapter(body)

    assert adapter.fetch_entries()[0].published_at is None


def test_missing_title_gets_a_placeholder():
    body = RSS_TEMPLATE.format(
        items="""
    <item><link>https://example.com/untitled</link></item>"""
    )
    adapter, _ = _adapter(body)

    assert adapter.fetch_entries()[0].title == "untitled"


def test_missing_author_and_summary_are_none():
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>Sparse</title><link>https://example.com/sparse</link></item>"""
    )
    adapter, _ = _adapter(body)

    entry = adapter.fetch_entries()[0]
    assert entry.author is None
    assert entry.summary is None


def test_relative_link_is_resolved_against_the_feed():
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>Relative</title><link>/posts/relative</link></item>"""
    )
    adapter, _ = _adapter(body)

    assert (
        adapter.fetch_entries()[0].article_url == "https://example.com/posts/relative"
    )


def test_relative_link_falls_back_to_the_source_url_without_a_channel_link():
    body = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>No Link</title>
  <item><title>Relative</title><link>/posts/x</link></item>
</channel></rss>"""
    adapter, _ = _adapter(body, _source(url="https://blog.example.org/feed.xml"))

    assert adapter.fetch_entries()[0].article_url == "https://blog.example.org/posts/x"


def test_empty_feed_is_not_an_error():
    adapter, _ = _adapter(RSS_TEMPLATE.format(items=""))
    assert adapter.fetch_entries() == []


def test_unparseable_document_raises_feed_parse_error():
    adapter, _ = _adapter("this is not xml at all, nor is it html")
    with pytest.raises(FeedParseError, match="could not parse feed"):
        adapter.fetch_entries()


def test_slightly_malformed_feed_still_yields_its_items():
    """A stray raw ampersand makes feedparser flag the document but recover."""
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>Tom & Jerry</title><link>https://example.com/t</link></item>"""
    )
    adapter, _ = _adapter(body)

    assert len(adapter.fetch_entries()) == 1


def test_non_http_link_is_rejected():
    body = RSS_TEMPLATE.format(
        items="""
    <item><title>Script</title><link>javascript:alert(1)</link></item>"""
    )
    adapter, _ = _adapter(body)

    assert adapter.fetch_entries() == []


# --- limits and request shaping --------------------------------------------


def test_max_entries_per_poll_caps_results():
    items = "".join(
        f"<item><title>P{i}</title><link>https://example.com/{i}</link></item>"
        for i in range(10)
    )
    adapter, _ = _adapter(
        RSS_TEMPLATE.format(items=items), _source(max_entries_per_poll=3)
    )

    assert len(adapter.fetch_entries()) == 3


def test_custom_headers_are_sent():
    adapter, calls = _adapter(
        RSS_TEMPLATE.format(items=""), _source(headers={"User-Agent": "custom-agent"})
    )
    adapter.fetch_entries()

    assert calls[0]["headers"]["User-Agent"] == "custom-agent"


def test_username_and_password_become_a_basic_auth_header():
    adapter, calls = _adapter(
        RSS_TEMPLATE.format(items=""), _source(username="ada", password="hunter2")
    )
    adapter.fetch_entries()

    # base64("ada:hunter2")
    assert calls[0]["headers"]["Authorization"] == "Basic YWRhOmh1bnRlcjI="


def test_explicit_authorization_header_wins_over_username_and_password():
    adapter, calls = _adapter(
        RSS_TEMPLATE.format(items=""),
        _source(
            headers={"Authorization": "Basic preexisting"},
            username="ada",
            password="hunter2",
        ),
    )
    adapter.fetch_entries()

    assert calls[0]["headers"]["Authorization"] == "Basic preexisting"


def test_source_config_overrides_the_fetch_policy():
    source = _source(timeout_seconds=5, max_bytes=1234, allow_private_urls=True)
    adapter, calls = _adapter(
        RSS_TEMPLATE.format(items=""), source, policy=FetchPolicy(timeout=30)
    )
    adapter.fetch_entries()

    policy = calls[0]["policy"]
    assert policy.timeout == 5.0
    assert policy.max_bytes == 1234
    assert policy.allow_private is True


def test_invalid_max_entries_config_falls_back_to_the_default():
    adapter, _ = _adapter(
        RSS_TEMPLATE.format(items=""), _source(max_entries_per_poll="lots")
    )
    assert adapter.fetch_entries() == []
