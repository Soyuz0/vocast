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
    assert 'class="cards"' in response.text
    assert "An article" in response.text
    assert "Example Publication" in response.text
    assert "Reading List" in response.text
    assert "Open audio" in response.text


def test_library_searches_and_filters(client: TestClient, context: AppContext):
    _add_entry(context, title="A matching title", origin="Science Weekly")
    _add_entry(context, title="Another item", origin="Daily News")

    by_title = client.get("/library", params={"search": "matching"})
    by_publication = client.get("/library", params={"search": "science weekly"})
    by_queue = client.get("/library", params={"queued": "yes"})

    assert "A matching title" in by_title.text
    assert "Another item" not in by_title.text
    assert "A matching title" in by_publication.text
    assert "No matching articles" in by_queue.text


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

    body = client.get(
        "/library?search=Article&sort=title_asc&page_size=1&page=1"
    ).text

    assert "Page 1 of 2" in body
    assert (
        'href="/library?search=Article&amp;sort=title_asc&amp;page_size=1&amp;page=2"'
        in body
    )


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


def test_actions_require_existing_admin_authentication(
    client: TestClient, context: AppContext
):
    entry = _add_entry(context)
    context.config = replace(context.config, admin_token="not-for-html")
    path = f"/api/playlists/listen-later/entries/{entry.id}"

    assert client.post(path).status_code == 401
    accepted = client.post(path, headers={"Authorization": "Bearer not-for-html"})

    assert accepted.status_code == 201
    body = client.get("/library").text
    assert "not-for-html" not in body
    assert "kept only in this browser tab" in body


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
    assert 'href="/vocast/feeds/listen-later.xml"' in body
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

    assert "append your configured feed token" in body
    assert "feed token required" in body
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


def test_direct_tailnet_library_is_open_when_feed_token_is_configured(
    client: TestClient, context: AppContext
):
    _add_entry(context)
    context.config = replace(
        context.config,
        server=replace(context.config.server, feed_token="feed-secret"),
    )

    response = client.get("/library")

    assert response.status_code == 200
    assert "An article" in response.text
    assert "feed-secret" not in response.text


@pytest.mark.parametrize(
    "query",
    ["status=unknown", "queued=maybe", "downloaded=maybe", "published_after=nope"],
)
def test_invalid_library_filters_return_clear_errors(client: TestClient, query: str):
    response = client.get(f"/library?{query}")
    assert response.status_code == 400
