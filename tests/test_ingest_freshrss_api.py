"""FreshRSS Google Reader API adapter.

Shaped after real responses from a FreshRSS 1.27 instance: the stream is ordered
by crawl time, paginated with `continuation`, and unread filtering is a `xt`
exclusion of the read state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from vocast.ingest.adapters.base import FeedParseError
from vocast.ingest.adapters.freshrss_api import FreshRSSAPIAdapter
from vocast.ingest.models import Source
from vocast.ingest.nethttp import FetchError, Response

AUTH_BODY = "SID=reader/abc\nLSID=null\nAuth=reader/tok123\n"


def _item(
    guid: str,
    *,
    title: str = "An Article",
    href: str = "https://blog.example.org/post",
    published: int = 1784970678,
    author: str | None = "ada",
) -> dict:
    item = {
        "id": f"tag:google.com,2005:reader/item/{guid}",
        "title": title,
        "published": published,
        "updated": published,
        "crawlTimeMsec": str(published * 1000),
        "timestampUsec": str(published * 1000000),
        "alternate": [{"href": href, "type": "text/html"}],
        "origin": {"streamId": "feed/11", "title": "LessWrong"},
        "categories": ["user/-/state/com.google/reading-list"],
        "summary": {"content": "<p>Some show notes.</p>"},
    }
    if author is not None:
        item["author"] = author
    return item


def _source(**config) -> Source:
    defaults = {"username": "reader", "api_password": "secret-api-pw"}
    defaults.update(config)
    return Source(
        id=9,
        name="FreshRSS Unreads",
        kind="freshrss_api",
        url="http://freshrss:80",
        enabled=True,
        poll_interval_minutes=15,
        config=defaults,
        last_checked_at=None,
        last_success_at=None,
        last_error=None,
        created_at=None,
        updated_at=None,
    )


class FakeAPI:
    """Serves ClientLogin then a scripted sequence of stream pages."""

    def __init__(self, pages: list[dict], *, login_body: str = AUTH_BODY) -> None:
        self.pages = pages
        self.login_body = login_body
        self.calls: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if "ClientLogin" in url:
            return Response(url=url, status=200, body=self.login_body.encode())
        index = sum(1 for c in self.calls if "stream/contents" in c["url"]) - 1
        payload = self.pages[min(index, len(self.pages) - 1)]
        return Response(url=url, status=200, body=json.dumps(payload).encode())

    @property
    def stream_calls(self) -> list[dict]:
        return [c for c in self.calls if "stream/contents" in c["url"]]


def _adapter(api: FakeAPI, source: Source | None = None, **kwargs):
    return FreshRSSAPIAdapter(source or _source(), fetcher=api, **kwargs)


# --- authentication --------------------------------------------------------


def test_client_login_is_posted_and_token_is_used():
    api = FakeAPI([{"items": [_item("a")]}])
    _adapter(api).fetch_entries()

    login = api.calls[0]
    assert login["url"].endswith("/accounts/ClientLogin")
    assert login["data"] == b"Email=reader&Passwd=secret-api-pw"
    assert api.stream_calls[0]["headers"]["Authorization"] == (
        "GoogleLogin auth=reader/tok123"
    )


def test_missing_credentials_is_an_actionable_error():
    api = FakeAPI([{"items": []}])
    adapter = FreshRSSAPIAdapter(
        Source(**{**_source().__dict__, "config": {}}), fetcher=api
    )
    with pytest.raises(FeedParseError, match="api_password"):
        adapter.fetch_entries()


def test_login_without_an_auth_line_is_reported():
    api = FakeAPI([{"items": []}], login_body="Error=BadAuthentication\n")
    with pytest.raises(FeedParseError, match="no Auth token"):
        _adapter(api).fetch_entries()


def test_login_failure_does_not_leak_the_password():
    def failing(url, **kwargs):
        raise FetchError("HTTP 401 Unauthorized from http://freshrss:80")

    adapter = FreshRSSAPIAdapter(_source(), fetcher=failing)
    with pytest.raises(FeedParseError) as excinfo:
        adapter.fetch_entries()
    assert "secret-api-pw" not in str(excinfo.value)


# --- request shaping -------------------------------------------------------


def test_unread_only_excludes_the_read_state():
    api = FakeAPI([{"items": []}])
    _adapter(api).fetch_entries()
    assert "xt=user%2F-%2Fstate%2Fcom.google%2Fread" in api.stream_calls[0]["url"]


def test_unread_filter_can_be_turned_off():
    api = FakeAPI([{"items": []}])
    _adapter(api, _source(unread_only=False)).fetch_entries()
    assert "xt=" not in api.stream_calls[0]["url"]


def test_reading_list_is_the_default_stream():
    api = FakeAPI([{"items": []}])
    _adapter(api).fetch_entries()
    assert "state/com.google/reading-list" in api.stream_calls[0]["url"]


def test_page_size_is_capped_at_the_api_maximum():
    api = FakeAPI([{"items": []}])
    _adapter(api, _source(page_size=99999)).fetch_entries()
    assert "n=1000" in api.stream_calls[0]["url"]


# --- pagination ------------------------------------------------------------


def test_continuation_is_followed_across_pages():
    api = FakeAPI(
        [
            {"items": [_item("a")], "continuation": "cursor-1"},
            {"items": [_item("b")], "continuation": "cursor-2"},
            {"items": [_item("c")]},
        ]
    )
    entries = _adapter(api).fetch_entries()

    assert len(entries) == 3
    assert "c=cursor-1" in api.stream_calls[1]["url"]
    assert "c=cursor-2" in api.stream_calls[2]["url"]


def test_pagination_stops_without_a_continuation():
    api = FakeAPI([{"items": [_item("a")]}, {"items": [_item("b")]}])
    assert len(_adapter(api).fetch_entries()) == 1
    assert len(api.stream_calls) == 1


def test_empty_page_ends_pagination():
    api = FakeAPI([{"items": [], "continuation": "more"}])
    assert _adapter(api).fetch_entries() == []


def test_max_entries_per_poll_caps_the_backlog():
    api = FakeAPI(
        [
            {"items": [_item(str(i)) for i in range(50)], "continuation": "next"},
            {"items": [_item(str(i)) for i in range(50, 100)]},
        ]
    )
    assert len(_adapter(api, _source(max_entries_per_poll=60)).fetch_entries()) == 60


def test_duplicate_ids_across_pages_are_collapsed():
    api = FakeAPI(
        [
            {"items": [_item("dup")], "continuation": "next"},
            {"items": [_item("dup")]},
        ]
    )
    assert len(_adapter(api).fetch_entries()) == 1


# --- steady-state efficiency ----------------------------------------------


def test_pagination_stops_once_a_whole_page_is_already_known():
    """Otherwise every poll would re-download the entire backlog."""
    api = FakeAPI(
        [
            {"items": [_item("a"), _item("b")], "continuation": "deeper"},
            {"items": [_item("c")]},
        ]
    )
    known = {
        "tag:google.com,2005:reader/item/a",
        "tag:google.com,2005:reader/item/b",
    }
    adapter = _adapter(api, known_guids=lambda guids: known & set(guids))

    adapter.fetch_entries()

    assert len(api.stream_calls) == 1


def test_pagination_continues_while_new_articles_appear():
    api = FakeAPI(
        [
            {"items": [_item("a"), _item("new")], "continuation": "deeper"},
            {"items": [_item("c")]},
        ]
    )
    known = {"tag:google.com,2005:reader/item/a"}
    adapter = _adapter(api, known_guids=lambda guids: known & set(guids))

    adapter.fetch_entries()

    assert len(api.stream_calls) == 2


def test_first_ever_poll_walks_the_whole_backlog():
    api = FakeAPI(
        [
            {"items": [_item("a")], "continuation": "p2"},
            {"items": [_item("b")], "continuation": "p3"},
            {"items": [_item("c")]},
        ]
    )
    adapter = _adapter(api, known_guids=lambda guids: set())

    assert len(adapter.fetch_entries()) == 3
    assert len(api.stream_calls) == 3


# --- mapping ---------------------------------------------------------------


def test_entry_maps_to_the_publisher_url_not_freshrss():
    api = FakeAPI([{"items": [_item("a", href="https://blog.example.org/real")]}])
    [entry] = _adapter(api).fetch_entries()
    assert entry.article_url == "https://blog.example.org/real"


def test_freshrss_item_id_is_the_dedup_guid():
    api = FakeAPI([{"items": [_item("0006576c8d8a4ad3")]}])
    [entry] = _adapter(api).fetch_entries()
    assert entry.external_guid == "tag:google.com,2005:reader/item/0006576c8d8a4ad3"


def test_published_timestamp_is_converted_to_utc():
    api = FakeAPI([{"items": [_item("a", published=1748000000)]}])
    [entry] = _adapter(api).fetch_entries()
    assert entry.published_at == datetime.fromtimestamp(1748000000, tz=timezone.utc)


def test_author_and_summary_are_carried_through():
    api = FakeAPI([{"items": [_item("a")]}])
    [entry] = _adapter(api).fetch_entries()
    assert entry.author == "ada"
    assert "show notes" in entry.summary


def test_item_without_an_author_is_accepted():
    api = FakeAPI([{"items": [_item("a", author=None)]}])
    [entry] = _adapter(api).fetch_entries()
    assert entry.author is None


def test_item_without_a_usable_link_is_skipped():
    bad = _item("a")
    bad["alternate"] = [{"href": "javascript:alert(1)"}]
    api = FakeAPI([{"items": [bad, _item("b")]}])

    assert len(_adapter(api).fetch_entries()) == 1


def test_canonical_link_is_used_when_alternate_is_absent():
    item = _item("a")
    del item["alternate"]
    item["canonical"] = [{"href": "https://blog.example.org/canonical"}]
    api = FakeAPI([{"items": [item]}])

    [entry] = _adapter(api).fetch_entries()
    assert entry.article_url == "https://blog.example.org/canonical"


def test_missing_title_gets_a_placeholder():
    item = _item("a")
    del item["title"]
    api = FakeAPI([{"items": [item]}])
    assert _adapter(api).fetch_entries()[0].title == "untitled"


def test_entries_are_scoped_to_the_source():
    api = FakeAPI([{"items": [_item("a")]}])
    assert _adapter(api).fetch_entries()[0].source_id == 9


def test_non_dict_items_are_ignored():
    api = FakeAPI([{"items": ["nonsense", None, _item("a")]}])
    assert len(_adapter(api).fetch_entries()) == 1


# --- failure handling ------------------------------------------------------


def test_invalid_json_is_reported_clearly():
    def fetcher(url, **kwargs):
        if "ClientLogin" in url:
            return Response(url=url, status=200, body=AUTH_BODY.encode())
        return Response(url=url, status=200, body=b"<html>gateway error</html>")

    adapter = FreshRSSAPIAdapter(_source(), fetcher=fetcher)
    with pytest.raises(FeedParseError, match="invalid JSON"):
        adapter.fetch_entries()


def test_stream_request_failure_is_wrapped():
    def fetcher(url, **kwargs):
        if "ClientLogin" in url:
            return Response(url=url, status=200, body=AUTH_BODY.encode())
        raise FetchError("HTTP 503 Service Unavailable")

    adapter = FreshRSSAPIAdapter(_source(), fetcher=fetcher)
    with pytest.raises(FeedParseError, match="API request failed"):
        adapter.fetch_entries()


def test_lan_instance_needs_the_private_url_opt_in():
    api = FakeAPI([{"items": []}])
    adapter = _adapter(api, _source(allow_private_urls=True))
    adapter.fetch_entries()
    assert api.calls[0]["policy"].allow_private is True


# --- per-publication artwork ----------------------------------------------


SUBS = {
    "subscriptions": [
        {
            "id": "feed/11",
            "title": "LessWrong",
            "url": "https://lesswrong.com/feed.xml",
            "iconUrl": "http://127.0.0.1:8082/f.php?h=abc123",
        },
        {"id": "feed/12", "title": "No Icon", "url": "https://x.com/f", "iconUrl": ""},
    ]
}


class FakeAPIWithSubs(FakeAPI):
    """Also answers the subscription list, as a real instance does."""

    def __call__(self, url, **kwargs):
        if "subscription/list" in url:
            self.calls.append({"url": url, **kwargs})
            return Response(url=url, status=200, body=json.dumps(SUBS).encode())
        return super().__call__(url, **kwargs)


def test_episode_artwork_comes_from_the_feed_icon():
    api = FakeAPIWithSubs([{"items": [_item("a")]}])
    [entry] = FreshRSSAPIAdapter(_source(), fetcher=api).fetch_entries()
    assert entry.origin_image_url == "http://freshrss:80/f.php?h=abc123"


def test_icon_host_is_rewritten_to_the_reachable_one():
    """FreshRSS builds icon URLs from its own base, which may be unreachable."""
    api = FakeAPIWithSubs([{"items": [_item("a")]}])
    [entry] = FreshRSSAPIAdapter(_source(), fetcher=api).fetch_entries()
    assert "127.0.0.1:8082" not in entry.origin_image_url
    assert entry.origin_image_url.startswith("http://freshrss:80/")


def test_feed_without_an_icon_yields_none_so_the_default_is_used():
    item = _item("a")
    item["origin"] = {"streamId": "feed/12", "title": "No Icon"}
    api = FakeAPIWithSubs([{"items": [item]}])
    [entry] = FreshRSSAPIAdapter(_source(), fetcher=api).fetch_entries()
    assert entry.origin_image_url is None


def test_unknown_feed_yields_no_artwork():
    item = _item("a")
    item["origin"] = {"streamId": "feed/999", "title": "Unlisted"}
    api = FakeAPIWithSubs([{"items": [item]}])
    assert (
        FreshRSSAPIAdapter(_source(), fetcher=api).fetch_entries()[0].origin_image_url
        is None
    )


def test_subscription_list_is_fetched_once_per_poll():
    api = FakeAPIWithSubs(
        [{"items": [_item("a")], "continuation": "c"}, {"items": [_item("b")]}]
    )
    FreshRSSAPIAdapter(_source(), fetcher=api).fetch_entries()
    assert sum(1 for c in api.calls if "subscription/list" in c["url"]) == 1


def test_artwork_failure_does_not_stop_discovery():
    """Artwork is cosmetic; articles matter more."""

    def fetcher(url, **kwargs):
        if "ClientLogin" in url:
            return Response(url=url, status=200, body=AUTH_BODY.encode())
        if "subscription/list" in url:
            raise FetchError("HTTP 500 from freshrss")
        return Response(
            url=url, status=200, body=json.dumps({"items": [_item("a")]}).encode()
        )

    entries = FreshRSSAPIAdapter(_source(), fetcher=fetcher).fetch_entries()
    assert len(entries) == 1
    assert entries[0].origin_image_url is None


def test_feed_icons_can_be_turned_off():
    api = FakeAPIWithSubs([{"items": [_item("a")]}])
    adapter = FreshRSSAPIAdapter(_source(use_feed_icons=False), fetcher=api)
    adapter.fetch_entries()
    assert not any("subscription/list" in c["url"] for c in api.calls)
