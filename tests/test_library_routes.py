from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vocast import library
from vocast.ingest.api import ServiceState
from vocast.ingest.config import Config, DatabaseConfig, ServerConfig, StorageConfig
from vocast.ingest.context import AppContext
from vocast.ingest.models import EntryStatus, FeedEntry
from vocast.ingest.timeutils import utcnow
from vocast.server import create_app


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
    title: str = "An article",
    origin: str = "Example Publication",
    status: EntryStatus = EntryStatus.READY,
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
            article_url="https://articles.example.com/read?one=1&two=2",
            published_at=utcnow(),
            author="Ada Author",
            origin_name=origin,
        )
    )
    if status is EntryStatus.READY:
        context.entries.mark_ready(
            entry.id,
            episode_id=f"episode-{entry.id}",
            duration_seconds=90,
            audio_bytes=100,
        )
    else:
        context.entries.set_status(entry.id, status)
    return entry


def test_library_is_server_rendered_and_mobile_friendly(
    client: TestClient, context: AppContext
):
    _add_entry(context)

    response = client.get("/library")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<meta name="viewport"' in response.text
    assert 'class="rows"' in response.text
    assert "An article" in response.text
    assert "Example Publication" in response.text
    assert "Reading List" in response.text
    # Ready episodes are playable in place rather than via a bare link.
    assert "data-play" in response.text
    assert "data-player-audio" in response.text


def test_library_searches_and_filters(client: TestClient, context: AppContext):
    _add_entry(context, title="A matching title", origin="Science Weekly")
    _add_entry(context, title="Another item", origin="Daily News")

    by_title = client.get("/library", params={"search": "matching"})
    by_publication = client.get("/library", params={"search": "science weekly"})
    by_queue = client.get("/library", params={"queued": "yes"})

    assert "A matching title" in by_title.text
    assert "Another item" not in by_title.text
    assert "A matching title" in by_publication.text
    assert "Nothing matches" in by_queue.text


def test_library_escapes_untrusted_metadata(client: TestClient, context: AppContext):
    _add_entry(context, title="<script>alert('title')</script>", origin="A & B")

    body = client.get("/library").text

    assert "<script>alert('title')</script>" not in body
    assert "&lt;script&gt;alert" in body
    assert "A &amp; B" in body
    assert "one=1&amp;two=2" in body


def test_pagination_preserves_active_query(client: TestClient, context: AppContext):
    _add_entry(context, title="Article A")
    _add_entry(context, title="Article B")

    body = client.get("/library?search=Article&sort=title_asc&page_size=1&page=1").text

    assert "Page 1 of 2" in body

    # Asserted by content rather than by exact query-string order, which is an
    # implementation detail of how links are built.
    import re
    from urllib.parse import parse_qs, urlsplit

    hrefs = [
        h.replace("&amp;", "&") for h in re.findall(r'href="(/library\?[^"]+)"', body)
    ]
    next_pages = [
        parse_qs(urlsplit(h).query)
        for h in hrefs
        if parse_qs(urlsplit(h).query).get("page") == ["2"]
    ]
    assert next_pages, "no link to page 2"
    assert next_pages[0]["search"] == ["Article"]
    assert next_pages[0]["sort"] == ["title_asc"]
    assert next_pages[0]["page_size"] == ["1"]


def test_add_and_remove_actions_are_idempotent(client: TestClient, context: AppContext):
    entry = _add_entry(context)
    path = f"/api/playlists/listen-later/entries/{entry.id}"

    first = client.post(path)
    duplicate = client.post(path)
    rendered = client.get("/library")
    removed = client.delete(path)
    missing = client.delete(path)

    assert first.status_code == 201
    assert first.json() == {"entry_id": entry.id, "queued": True, "changed": True}
    assert duplicate.status_code == 200
    assert duplicate.json()["changed"] is False
    assert "Remove from Listen Later" in rendered.text
    assert removed.json()["changed"] is True
    assert missing.json()["changed"] is False
    assert "Add to Listen Later" in client.get("/library").text


@pytest.mark.parametrize("method", ["post", "delete"])
def test_actions_reject_unknown_entries(client: TestClient, method: str):
    response = getattr(client, method)("/api/playlists/listen-later/entries/999")
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown entry"


def test_queueing_needs_no_second_credential(client: TestClient, context: AppContext):
    """Loading the library already proved access.

    Demanding the admin token as well meant a prompt for a different secret on
    every click, on a page the caller had already authenticated to.
    """
    entry = _add_entry(context)
    context.config = replace(context.config, admin_token="admin-secret")

    response = client.post(
        f"/api/playlists/listen-later/entries/{entry.id}",
        headers={"origin": "http://testserver"},
    )

    assert response.status_code == 201
    assert response.json()["queued"] is True


def test_queueing_still_refuses_a_cross_origin_request(
    client: TestClient, context: AppContext
):
    entry = _add_entry(context)
    response = client.post(
        f"/api/playlists/listen-later/entries/{entry.id}",
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_administrative_endpoints_still_require_the_admin_token(
    client: TestClient, context: AppContext
):
    """Only playlist curation was relaxed."""
    context.config = replace(context.config, admin_token="admin-secret")
    assert client.get("/api/sources").status_code == 401


def test_actions_reject_cross_origin_browser_requests(
    client: TestClient, context: AppContext
):
    entry = _add_entry(context)
    path = f"/api/playlists/listen-later/entries/{entry.id}"

    rejected = client.post(path, headers={"Origin": "https://attacker.example"})
    accepted = client.post(path, headers={"Origin": "https://podcast.example.com"})

    assert rejected.status_code == 403
    assert accepted.status_code == 201


def test_library_links_support_a_public_path_prefix(
    client: TestClient, context: AppContext
):
    entry = _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(
            context.config.server,
            public_base_url="https://podcast.example.com/vocast",
        ),
    )

    body = client.get("/library").text
    accepted = client.post(
        f"/api/playlists/listen-later/entries/{entry.id}",
        headers={"Origin": "https://podcast.example.com"},
    )

    assert 'action="/vocast/library"' in body
    assert 'href="/vocast/library?' in body
    assert 'data-base-path="/vocast"' in body
    assert "basePath + '/api/playlists/listen-later/entries/'" in body
    assert accepted.status_code == 201


def test_private_feed_links_explain_token_without_exposing_it(
    client: TestClient, context: AppContext
):
    _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="private-feed-secret"),
    )

    secure_client = TestClient(client.app, base_url="https://podcast.example.com")
    body = secure_client.get("/public/library?token=private-feed-secret").text

    # The page offers a copy button rather than printing URLs, and must never
    # contain the secret: it is unauthenticated on the tailnet.
    assert "data-copy-feed" in body
    assert "private-feed-secret" not in body


def test_public_library_requires_feed_token_and_exchanges_it_for_a_cookie(
    client: TestClient, context: AppContext
):
    _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="library-secret"),
    )

    secure_client = TestClient(client.app, base_url="https://podcast.example.com")
    assert secure_client.get("/public/library").status_code == 401
    assert secure_client.get("/public/library?token=wrong").status_code == 401

    login = secure_client.get(
        "/public/library?token=library-secret&search=article",
        follow_redirects=False,
    )

    assert login.status_code == 303
    assert login.headers["location"] == "/library?search=article"
    assert "library-secret" not in login.headers["location"]
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
    assert "Secure" in login.headers["set-cookie"]

    page = secure_client.get(login.headers["location"])
    assert page.status_code == 200
    assert "library-secret" not in page.text


FUNNEL = {"Tailscale-Funnel-Request": "?1"}


def test_library_is_open_on_the_tailnet(client: TestClient, context: AppContext):
    """Browsing from inside the tailnet should not require a token."""
    _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="feed-secret"),
    )

    response = client.get("/library")

    assert response.status_code == 200
    assert "An article" in response.text


def test_library_requires_the_token_from_the_internet(
    client: TestClient, context: AppContext
):
    """Funnel publishes every path, so the library is reachable publicly."""
    _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="feed-secret"),
    )

    assert client.get("/library", headers=FUNNEL).status_code == 401
    assert client.get("/library?token=feed-secret", headers=FUNNEL).status_code != 401


def test_library_is_open_when_no_token_is_configured(
    client: TestClient, context: AppContext
):
    """A purely local deployment should not be forced to authenticate."""
    _add_entry(context)
    response = client.get("/library")
    assert response.status_code == 200
    assert "An article" in response.text
    assert "feed-secret" not in response.text


@pytest.mark.parametrize(
    "query",
    ["status=unknown", "queued=maybe", "read=maybe", "published_after=nope"],
)
def test_invalid_library_filters_return_clear_errors(client: TestClient, query: str):
    response = client.get(f"/library?{query}")
    assert response.status_code == 400


def test_login_cookie_is_usable_over_plain_http(
    client: TestClient, context: AppContext
):
    """The tailnet address is HTTP while public_base_url is HTTPS.

    Deriving the cookie's Secure flag from the configured URL rather than the
    actual connection would stop the browser ever sending it back, leaving the
    page redirecting to itself.
    """
    _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(
            context.config.server,
            feed_token="feed-secret",
            public_base_url="https://podcast.example.com",
        ),
    )

    response = client.get("/library?token=feed-secret", follow_redirects=False)
    assert response.status_code == 303
    assert "secure" not in response.headers["set-cookie"].lower()

    assert client.get("/library").status_code == 200


def test_login_cookie_is_secure_behind_a_tls_proxy(
    client: TestClient, context: AppContext
):
    _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="feed-secret"),
    )

    response = client.get(
        "/library?token=feed-secret",
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    )
    assert "secure" in response.headers["set-cookie"].lower()


# --- the public surface as a whole -----------------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/library", "/api/health", "/feeds/all.xml", "/feed.xml"]
)
def test_every_path_needs_the_token_from_the_internet(
    client: TestClient, context: AppContext, path: str
):
    """The guard is app-wide on purpose: Funnel exposes every path, including
    ones added later, so allow-listing individual routes would leak by
    omission."""
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="feed-secret"),
    )
    assert client.get(path, headers=FUNNEL).status_code == 401


@pytest.mark.parametrize("path", ["/", "/library", "/api/health"])
def test_those_same_paths_stay_open_on_the_tailnet(
    client: TestClient, context: AppContext, path: str
):
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="feed-secret"),
    )
    assert client.get(path).status_code == 200


def test_the_marker_cannot_be_used_to_bypass_anything(
    client: TestClient, context: AppContext
):
    """Claiming to be a Funnel request only ever adds a requirement."""
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="feed-secret"),
    )
    assert (
        client.get(
            "/api/health", headers={"Tailscale-Funnel-Request": "?0"}
        ).status_code
        == 401
    )


def test_no_token_configured_leaves_the_internet_path_open(
    client: TestClient, context: AppContext
):
    """Nothing to enforce, so a deployment without a token is unchanged."""
    assert client.get("/api/health", headers=FUNNEL).status_code == 200


# --- the mobile direction --------------------------------------------------


def test_mobile_patterns_are_present(client: TestClient, context: AppContext):
    """The mobile design is its own layout, not a narrower desktop.

    A bottom tab bar, a filter sheet and a pill search are what make it usable
    one-handed, so their absence is a regression worth catching.
    """
    _add_entry(context)
    body = client.get("/library").text

    assert 'class="tabbar"' in body  # bottom navigation
    assert "data-open-filters" in body  # opens the sheet
    assert "data-close-filters" in body  # Done button inside it
    assert "sheetchrome" in body  # grab handle and title
    assert "@media (max-width:860px)" in body


def test_filter_count_badge_reflects_active_filters(
    client: TestClient, context: AppContext
):
    _add_entry(context)

    plain = client.get("/library?search=Article").text
    filtered = client.get("/library?search=Article&status=ready&queued=no").text

    # Searching and sorting are not "filters" for badge purposes; narrowing is.
    assert '<span class="filtern">' not in plain
    assert '<span class="filtern">2</span>' in filtered


def test_status_dots_appear_beside_each_status(client: TestClient, context: AppContext):
    """Colour is the fastest way to read state in a long list."""
    _add_entry(context)
    body = client.get("/library").text

    assert 'class="dot-status ready"' in body
    assert ".dot-status.ready{background:var(--color-success)}" in body
    assert ".dot-status.failed{background:var(--color-error)}" in body
    assert ".dot-status.processing{background:var(--color-warning)" in body


def test_mobile_header_offers_refresh(client: TestClient, context: AppContext):
    """The list is server-rendered, so seeing new episodes means reloading."""
    _add_entry(context)
    body = client.get("/library").text

    assert "data-refresh" in body
    assert "location.reload()" in body


def test_sidebar_does_not_report_generation_progress(
    client: TestClient, context: AppContext
):
    """Removed deliberately: it competed with the status facet, which already
    shows a processing count, and reported nothing actionable."""
    _add_entry(context, status=EntryStatus.PROCESSING)
    body = client.get("/library").text

    assert "Generating" not in body
    assert 'class="gen"' not in body


def _processing_with_progress(context: AppContext, done: int, total: int, **kwargs):
    entry = _add_entry(context, status=EntryStatus.PROCESSING, **kwargs)
    context.entries.record_progress(entry.id, done, total)
    return entry


def test_processing_entry_shows_a_determinate_bar(
    client: TestClient, context: AppContext
):
    """Chunks synthesized over chunks total, so the width is a real measurement."""
    _processing_with_progress(context, done=3, total=4)
    body = client.get("/library").text

    assert 'role="progressbar"' in body
    assert 'aria-valuenow="75"' in body
    assert "width: 75%" in body
    assert "processing 75%" in body


def test_progress_bar_absent_before_the_first_chunk_lands(
    client: TestClient, context: AppContext
):
    """A just-claimed entry has no progress; an empty bar would imply stalled."""
    _add_entry(context, status=EntryStatus.PROCESSING)
    body = client.get("/library").text

    assert 'role="progressbar"' not in body


def test_single_chunk_article_gets_no_bar(client: TestClient, context: AppContext):
    """A bar that can only read 0% or 100% conveys nothing."""
    _processing_with_progress(context, done=1, total=1)

    assert 'role="progressbar"' not in client.get("/library").text


def test_finished_entry_shows_no_progress(client: TestClient, context: AppContext):
    """Progress is cleared on completion, so a ready row cannot show a stale bar."""
    entry = _processing_with_progress(context, done=2, total=4)
    context.entries.mark_ready(
        entry.id,
        episode_id="ep-1",
        content_hash="abc",
        duration_seconds=90.0,
        audio_bytes=1024,
    )

    stored = context.entries.get(entry.id)
    assert stored.progress_done is None
    assert 'role="progressbar"' not in client.get("/library").text


def test_refresh_is_reachable_from_both_layouts(
    client: TestClient, context: AppContext
):
    """The mobile header and the desktop toolbar each need their own control:
    each is hidden at the other breakpoint."""
    _add_entry(context)
    body = client.get("/library").text

    assert 'class="iconbtn" type="button" data-refresh' in body
    assert 'class="ghost deskonly" type="button" data-refresh' in body
    assert "querySelectorAll('[data-refresh]')" in body


def test_publications_are_filterable_on_mobile(client: TestClient, context: AppContext):
    """The publications facet lived only in the desktop sidebar, which is hidden
    on mobile, leaving no way to filter by publication on a phone."""
    _add_entry(context, title="One", origin="The Publication")
    body = client.get("/library").text

    assert "scrollfacet" in body
    assert body.count("origin_id=the+publication") >= 2


def test_selecting_a_publication_on_mobile_filters_the_list(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Kept", origin="Wanted Publication")
    _add_entry(context, title="Dropped", origin="Other Publication")

    body = client.get("/library?origin_id=wanted publication").text

    assert "Kept" in body
    assert "Dropped" not in body


def test_a_link_posts_episode_points_at_the_post_not_the_target(
    client: TestClient, context: AppContext
):
    """The narration is the link blogger's own commentary, so "Original" must
    lead to that post; the outbound link is a different article entirely."""
    entry = _add_entry(context, title="A link post")
    with context.db.transaction() as conn:
        conn.execute(
            "UPDATE entries SET post_url = ? WHERE id = ?",
            ("https://daringfireball.net/linked/2026/07/24/a-post", entry.id),
        )

    body = client.get("/library").text

    assert "https://daringfireball.net/linked/2026/07/24/a-post" in body
    assert "https://articles.example.com/read?one=1&amp;two=2" not in body


def test_read_control_is_present_at_both_breakpoints(
    client: TestClient, context: AppContext
):
    """The desktop status cell is hidden on mobile, so the mobile row carries
    its own control rather than leaving the marker unreachable on a phone."""
    _add_entry(context)
    body = client.get("/library").text

    assert 'class="linkish" type="button" data-read-toggle' in body
    assert 'class="dl" type="button" data-read-toggle' in body


def test_read_control_reflects_the_current_state(
    client: TestClient, context: AppContext
):
    entry = _add_entry(context)
    context.consumption.set_read(entry.id, read=True)

    body = client.get("/library").text

    assert body.count('data-read="true"') == 4  # two controls, state and aria
    assert ">mark read</button>" not in body


def test_unnarrated_entries_still_carry_a_read_control(
    client: TestClient, context: AppContext
):
    """An article can be read in the reader whether or not it ever narrated,
    and a comic that failed is exactly the case that must not look unread."""
    _add_entry(context, status=EntryStatus.FAILED)

    assert 'type="button" data-read-toggle' in client.get("/library").text


def test_library_filters_on_read_state(client: TestClient, context: AppContext):
    read = _add_entry(context, title="Already read")
    _add_entry(context, title="Still unread")
    context.consumption.set_read(read.id, read=True)

    unread_page = client.get("/library?read=no").text
    read_page = client.get("/library?read=yes").text

    assert "Still unread" in unread_page and "Already read" not in unread_page
    assert "Already read" in read_page and "Still unread" not in read_page
