from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vocast import library
from vocast.ingest.api import ServiceState
from vocast.ingest.config import Config, DatabaseConfig, ServerConfig, StorageConfig
from vocast.ingest.context import AppContext
from vocast.ingest.models import FeedEntry
from vocast.ingest.timeutils import to_iso, utcnow
from vocast.server import create_app

FUNNEL = {"Tailscale-Funnel-Request": "?1"}

SOURCES_PAGE = "/m"
ARTICLES_PAGE = "/m/articles"


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    config = Config(
        database=DatabaseConfig(path=tmp_path / "state.db"),
        storage=StorageConfig(library_path=tmp_path / "library"),
        server=ServerConfig(public_base_url="https://podcast.example.com"),
    )
    context = AppContext.create(config)
    monkeypatch.setattr(library, "LIBRARY_PATH", config.storage.library_path)
    return context


@pytest.fixture
def client(context: AppContext) -> TestClient:
    return TestClient(create_app(ServiceState(context=context)))


def _add_entry(
    context: AppContext,
    *,
    title: str,
    origin: str = "Example Publication",
    url: str = "https://articles.example.com/read?one=1&two=2",
    read: bool = False,
    queued: bool = False,
):
    source = context.sources.find_by_url(
        kind="rss", url="https://source.example.com/feed"
    ) or context.sources.add(
        name="Reading List", kind="rss", url="https://source.example.com/feed"
    )
    entry = context.entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid=f"guid-{title}",
            title=title,
            article_url=url,
            published_at=utcnow(),
            author="Ada Author",
            origin_name=origin,
        )
    )
    if read:
        with context.db.transaction() as conn:
            conn.execute(
                "UPDATE entries SET read_at = ? WHERE id = ?",
                (to_iso(utcnow()), entry.id),
            )
    if queued:
        context.playlists.add_entry("listen-later", entry.id)
    return entry


def _count_beside(body: str, label: str) -> int:
    """The number rendered in the row labelled `label` on the source list."""
    match = re.search(
        rf'<span class="label">{re.escape(label)}</span>\s*<span class="n">(\d+)</span>',
        body,
    )
    assert match, f"no row labelled {label!r}"
    return int(match.group(1))


def _tag(body: str, attribute: str) -> str:
    match = re.search(rf"<[a-z]+[^>]*{re.escape(attribute)}[^>]*>", body, re.DOTALL)
    assert match, f"no element with {attribute}"
    return match.group(0)


def _with_token(context: AppContext, token: str = "feed-secret") -> None:
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token=token),
    )


# --- reachability and gating -----------------------------------------------


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_mobile_pages_render_on_the_tailnet(
    client: TestClient, context: AppContext, path: str
):
    _add_entry(context, title="An article")

    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'name="viewport"' in response.text
    assert "viewport-fit=cover" in response.text


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_mobile_pages_need_the_token_from_the_internet(
    client: TestClient, context: AppContext, path: str
):
    """Funnel publishes every path, so a new page is public the day it lands."""
    _add_entry(context, title="An article")
    _with_token(context)

    assert client.get(path, headers=FUNNEL).status_code == 401
    assert client.get(f"{path}?token=feed-secret", headers=FUNNEL).status_code != 401


def test_a_token_in_the_url_becomes_a_cookie_so_navigation_keeps_working(
    client: TestClient, context: AppContext
):
    """Links between the two pages carry no token; the cookie is what does."""
    _add_entry(context, title="An article")
    _with_token(context)

    landing = client.get(
        f"{SOURCES_PAGE}?token=feed-secret", headers=FUNNEL, follow_redirects=False
    )

    assert landing.status_code == 303
    assert landing.headers["location"] == SOURCES_PAGE
    assert "feed-secret" not in landing.headers["location"]
    assert client.get(ARTICLES_PAGE, headers=FUNNEL).status_code == 200


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_the_feed_token_never_reaches_the_page(
    client: TestClient, context: AppContext, path: str
):
    """The page is unauthenticated on the tailnet, so it must hold no secret."""
    _add_entry(context, title="An article")
    _with_token(context, "super-secret-token")

    body = client.get(f"{path}?token=super-secret-token", follow_redirects=True).text

    assert "super-secret-token" not in body


# --- page one: the source list ---------------------------------------------


def test_sources_page_lists_both_destinations_and_every_publication(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Space telescopes", origin="Science Weekly")
    _add_entry(context, title="Rain again", origin="Daily News")
    _add_entry(context, title="More rain", origin="Daily News")

    body = client.get(SOURCES_PAGE).text

    assert "Library" in body
    assert "Listen Later" in body
    assert "Science Weekly" in body
    assert "Daily News" in body
    assert f'href="{ARTICLES_PAGE}?filter=unread"' in body
    assert f"{ARTICLES_PAGE}?filter=unread&amp;playlist=listen-later" in body
    assert f"{ARTICLES_PAGE}?filter=unread&amp;origin_id=daily+news" in body


def test_publications_are_listed_alphabetically(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="One", origin="Zeta Report")
    _add_entry(context, title="Two", origin="Acme Digest")

    body = client.get(SOURCES_PAGE).text

    assert body.index("Acme Digest") < body.index("Zeta Report")


def test_source_counts_follow_the_active_filter(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Unread one", origin="Science Weekly")
    _add_entry(context, title="Read one", origin="Science Weekly", read=True)
    _add_entry(context, title="Starred", origin="Daily News", queued=True)

    unread = client.get(SOURCES_PAGE, params={"filter": "unread"}).text
    read = client.get(SOURCES_PAGE, params={"filter": "read"}).text
    everything = client.get(SOURCES_PAGE, params={"filter": "all"}).text

    assert _count_beside(unread, "Library") == 2
    assert _count_beside(unread, "Listen Later") == 1
    assert _count_beside(unread, "Science Weekly") == 1

    assert _count_beside(read, "Library") == 1
    assert _count_beside(read, "Listen Later") == 0
    assert _count_beside(read, "Science Weekly") == 1

    assert _count_beside(everything, "Library") == 3
    assert _count_beside(everything, "Science Weekly") == 2


# --- page two: the article list --------------------------------------------


def test_article_page_narrows_to_the_chosen_publication(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Space telescopes", origin="Science Weekly")
    _add_entry(context, title="Rain again", origin="Daily News")

    body = client.get(ARTICLES_PAGE, params={"origin_id": "science weekly"}).text

    assert "Space telescopes" in body
    assert "Rain again" not in body
    assert "Science Weekly" in body


def test_listen_later_shows_only_starred_articles(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Saved for later", queued=True)
    _add_entry(context, title="Just passing through")

    body = client.get(ARTICLES_PAGE, params={"playlist": "listen-later"}).text

    assert "Saved for later" in body
    assert "Just passing through" not in body


@pytest.mark.parametrize(
    ("active", "visible", "hidden"),
    [
        ("unread", "Still unread", "Already read"),
        ("read", "Already read", "Still unread"),
    ],
)
def test_the_read_filter_changes_which_articles_appear(
    client: TestClient, context: AppContext, active: str, visible: str, hidden: str
):
    _add_entry(context, title="Still unread")
    _add_entry(context, title="Already read", read=True)

    body = client.get(ARTICLES_PAGE, params={"filter": active}).text

    assert visible in body
    assert hidden not in body


def test_showing_all_includes_read_and_unread(client: TestClient, context: AppContext):
    _add_entry(context, title="Still unread")
    _add_entry(context, title="Already read", read=True)

    body = client.get(ARTICLES_PAGE, params={"filter": "all"}).text

    assert "Still unread" in body
    assert "Already read" in body


def test_the_filter_toggle_cycles_unread_read_all(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="An article")

    def next_filter(active: str) -> str:
        body = client.get(ARTICLES_PAGE, params={"filter": active}).text
        marker = 'class="filter" data-filter="' + active + '" href="'
        start = body.index(marker) + len(marker)
        return body[start : body.index('"', start)]

    assert next_filter("unread").endswith("filter=read")
    assert next_filter("read").endswith("filter=all")
    assert next_filter("all").endswith("filter=unread")


def test_an_unrecognised_filter_falls_back_to_unread(
    client: TestClient, context: AppContext
):
    """A stale bookmark should show the inbox, not an error page."""
    _add_entry(context, title="Still unread")
    _add_entry(context, title="Already read", read=True)

    body = client.get(ARTICLES_PAGE, params={"filter": "sideways"}).text

    assert "Still unread" in body
    assert "Already read" not in body


def test_cards_open_the_original_article_in_a_new_tab(
    client: TestClient, context: AppContext
):
    _add_entry(
        context, title="An article", url="https://articles.example.com/read?one=1&two=2"
    )

    card = _tag(client.get(ARTICLES_PAGE).text, 'class="entry"')

    assert 'href="https://articles.example.com/read?one=1&amp;two=2"' in card
    assert 'target="_blank"' in card
    assert 'rel="noopener noreferrer"' in card


def test_the_mobile_ui_offers_no_way_to_listen(client: TestClient, context: AppContext):
    """Triage only. Listening happens in a podcast client, not in this page."""
    _add_entry(context, title="An article")

    body = client.get(ARTICLES_PAGE).text

    assert "<audio" not in body
    assert "/audio/" not in body
    assert "data-play" not in body


def test_search_narrows_the_article_list(client: TestClient, context: AppContext):
    _add_entry(context, title="Space telescopes")
    _add_entry(context, title="Rain again")

    body = client.get(ARTICLES_PAGE, params={"search": "telescopes"}).text

    assert "Space telescopes" in body
    assert "Rain again" not in body


def test_search_stays_within_the_chosen_publication(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Rain in Rome", origin="Daily News")
    _add_entry(context, title="Rain on Mars", origin="Science Weekly")

    body = client.get(
        ARTICLES_PAGE, params={"search": "rain", "origin_id": "daily news"}
    ).text

    assert "Rain in Rome" in body
    assert "Rain on Mars" not in body
    assert 'name="origin_id" value="daily news"' in body


def test_both_swipe_actions_have_a_button_a_thumb_can_find(
    client: TestClient, context: AppContext
):
    """Swiping is the fast path, not the only one, and not usable by a reader."""
    _add_entry(context, title="Saved and read", read=True, queued=True)

    body = client.get(ARTICLES_PAGE, params={"filter": "all"}).text

    read_button = _tag(body, 'data-action="read"')
    star_button = _tag(body, 'data-action="star"')

    assert read_button.startswith("<button")
    assert 'aria-pressed="true"' in read_button
    assert "Mark as unread: Saved and read" in read_button
    assert star_button.startswith("<button")
    assert 'aria-pressed="true"' in star_button
    assert "Remove from Listen Later: Saved and read" in star_button


def test_article_rows_carry_the_state_the_swipe_toggles(
    client: TestClient, context: AppContext
):
    entry = _add_entry(context, title="An article")

    body = client.get(ARTICLES_PAGE).text

    assert f'data-entry-id="{entry.id}"' in body
    assert 'data-read="false"' in body
    assert 'data-queued="false"' in body


def test_untrusted_metadata_is_escaped(client: TestClient, context: AppContext):
    _add_entry(context, title="<script>alert('x')</script>", origin="A & B")

    body = client.get(ARTICLES_PAGE).text

    assert "<script>alert('x')</script>" not in body
    assert "&lt;script&gt;alert" in body
    assert "A &amp; B" in body


def test_the_article_page_offers_a_way_back(client: TestClient, context: AppContext):
    _add_entry(context, title="An article")

    body = client.get(ARTICLES_PAGE, params={"filter": "all"}).text

    assert f'class="back" href="{SOURCES_PAGE}?filter=all"' in body


def test_paging_keeps_the_selection_and_the_filter(
    client: TestClient, context: AppContext
):
    for number in range(51):
        _add_entry(context, title=f"Article {number:02d}", origin="Daily News")

    body = client.get(
        ARTICLES_PAGE, params={"origin_id": "daily news", "filter": "unread"}
    ).text

    assert "Page 1 of 2" in body
    assert "filter=unread&amp;origin_id=daily+news&amp;page=2" in body
