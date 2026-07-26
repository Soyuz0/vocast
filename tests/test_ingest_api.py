"""HTTP surface: feed routes, health, admin auth, and backward compatibility."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vocast import library
from vocast.ingest.api import ServiceState
from vocast.ingest.config import Config, DatabaseConfig, ServerConfig, StorageConfig
from vocast.ingest.context import AppContext
from vocast.ingest.models import FeedEntry
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
def state(context: AppContext) -> ServiceState:
    return ServiceState(context=context)


@pytest.fixture
def client(state: ServiceState) -> TestClient:
    return TestClient(create_app(state))


def _make_episode(lib: Path, episode_id: str, title: str) -> None:
    entry_dir = lib / episode_id
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "audio.mp3").write_bytes(b"fake-mp3")
    (entry_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": episode_id,
                "title": title,
                "source": None,
                "synthesized_at": "2026-06-04T12:00:00+00:00",
                "duration_seconds": 60.0,
                "voice": "af_heart",
                "engine": "kokoro",
            }
        )
    )


def _add_ready_entry(context: AppContext, *, source_name: str, episode_id: str) -> int:
    source = context.sources.add(
        name=source_name, kind="rss", url=f"https://example.com/{source_name}.xml"
    )
    entry = context.entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid=f"guid-{episode_id}",
            title="Article",
            article_url="https://example.com/article",
            published_at=utcnow(),
        )
    )
    context.entries.mark_ready(entry.id, episode_id=episode_id)
    return source.id


# --- feed routes -----------------------------------------------------------


def test_all_feed_is_served_as_rss(client: TestClient):
    response = client.get("/feeds/all.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")


def test_feed_xml_and_all_xml_agree(client: TestClient, context: AppContext):
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")
    assert client.get("/feed.xml").text == client.get("/feeds/all.xml").text


def test_source_feed_is_served(client: TestClient, context: AppContext):
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")
    source_id = _add_ready_entry(
        context, source_name="Tech", episode_id="20260604T120000Z_a_aaa1"
    )

    response = client.get(f"/feeds/source/{source_id}.xml")
    assert response.status_code == 200
    assert "<title>A</title>" in response.text


def test_unknown_source_feed_is_404(client: TestClient):
    assert client.get("/feeds/source/999.xml").status_code == 404


def test_configured_public_base_url_is_used_for_enclosures(
    client: TestClient, context: AppContext
):
    """Behind a TLS proxy the request looks like internal http, so the
    configured base must win or clients get unreachable URLs."""
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")

    body = client.get("/feeds/all.xml").text
    assert "https://podcast.example.com/audio/20260604T120000Z_a_aaa1.mp3" in body
    assert "testserver" not in body


def test_request_base_url_is_used_when_none_is_configured(context: AppContext):
    state = ServiceState(
        context=replace(context, config=replace(context.config, server=ServerConfig()))
    )
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")

    body = TestClient(create_app(state)).get("/feeds/all.xml").text
    assert "http://testserver/audio/20260604T120000Z_a_aaa1.mp3" in body


def test_head_request_on_the_feed_is_allowed(client: TestClient):
    assert client.head("/feeds/all.xml").status_code == 200


def test_listen_later_feed_contains_only_queued_ready_entries(
    client: TestClient, context: AppContext
):
    source = context.sources.add(
        name="Tech", kind="rss", url="https://example.com/listen-later.xml"
    )
    entry = context.entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid="queued",
            title="Selected article",
            article_url="https://example.com/selected",
            published_at=utcnow(),
        )
    )
    context.entries.mark_ready(
        entry.id, episode_id="selected-episode", duration_seconds=60, audio_bytes=8
    )
    context.playlists.add_entry("listen-later", entry.id)

    response = client.get("/feeds/listen-later.xml")

    assert response.status_code == 200
    assert "selected-episode" in response.text
    assert "Vocast - Listen Later" in response.text


def test_existing_feed_routes_are_unchanged_by_listen_later(
    client: TestClient, context: AppContext
):
    _make_episode(context.config.storage.library_path, "existing-episode", "Existing")
    source_id = _add_ready_entry(
        context, source_name="Existing", episode_id="existing-episode"
    )
    before = {
        path: client.get(path).text
        for path in ("/feed.xml", "/feeds/all.xml", f"/feeds/source/{source_id}.xml")
    }
    [entry] = context.entries.all()
    context.playlists.add_entry("listen-later", entry.id)

    assert {
        path: client.get(path).text
        for path in ("/feed.xml", "/feeds/all.xml", f"/feeds/source/{source_id}.xml")
    } == before


# --- health ----------------------------------------------------------------


def test_health_reports_status_and_counts(client: TestClient, context: AppContext):
    source = context.sources.add(name="A", kind="rss", url="https://example.com/f.xml")
    context.entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid="g",
            title="T",
            article_url="https://example.com/a",
            published_at=utcnow(),
        )
    )

    payload = client.get("/api/health").json()

    assert payload["status"] == "ok"
    assert payload["database"] == "ok"
    assert payload["sources"] == 1
    assert payload["pending"] == 1
    assert payload["failed"] == 0
    assert payload["worker"] == "stopped"
    assert payload["poller"] == "stopped"
    assert payload["last_successful_poll"] is None


def test_health_reports_running_background_threads(
    state: ServiceState, client: TestClient
):
    state.worker_running = True
    state.poller_running = True
    payload = client.get("/api/health").json()
    assert payload["worker"] == "running"
    assert payload["poller"] == "running"


def test_health_reports_last_successful_poll(client: TestClient, context: AppContext):
    source = context.sources.add(name="A", kind="rss", url="https://example.com/f.xml")
    context.sources.mark_success(source.id)
    assert client.get("/api/health").json()["last_successful_poll"] is not None


def test_health_does_not_leak_secrets(client: TestClient, context: AppContext):
    context.config = replace(context.config, admin_token="super-secret")
    body = client.get("/api/health").text
    assert "super-secret" not in body


def test_health_degrades_instead_of_raising(client: TestClient, context: AppContext):
    context.db.close()
    context.db.path = Path("/nonexistent-directory/state.db")

    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


# --- admin API -------------------------------------------------------------


def test_sources_can_be_created_and_listed(client: TestClient):
    created = client.post(
        "/api/sources",
        json={"name": "Example", "url": "https://example.com/feed.xml"},
    )
    assert created.status_code == 201
    assert created.json()["feed"] == f"/feeds/source/{created.json()['id']}.xml"

    listed = client.get("/api/sources").json()
    assert [s["name"] for s in listed] == ["Example"]


def test_duplicate_source_is_a_conflict(client: TestClient):
    payload = {"name": "Example", "url": "https://example.com/feed.xml"}
    client.post("/api/sources", json=payload)
    assert client.post("/api/sources", json=payload).status_code == 409


def test_unknown_kind_is_rejected(client: TestClient):
    response = client.post(
        "/api/sources",
        json={"name": "X", "url": "https://example.com/f.xml", "kind": "pigeon"},
    )
    assert response.status_code == 400


def test_private_url_is_rejected(client: TestClient):
    response = client.post(
        "/api/sources", json={"name": "X", "url": "http://127.0.0.1/feed.xml"}
    )
    assert response.status_code == 400


def test_non_http_url_is_rejected(client: TestClient):
    response = client.post(
        "/api/sources", json={"name": "X", "url": "file:///etc/passwd"}
    )
    assert response.status_code == 400


def test_source_can_be_patched(client: TestClient):
    source_id = client.post(
        "/api/sources", json={"name": "Old", "url": "https://example.com/f.xml"}
    ).json()["id"]

    patched = client.patch(
        f"/api/sources/{source_id}", json={"name": "New", "enabled": False}
    ).json()

    assert patched["name"] == "New"
    assert patched["enabled"] is False


def test_source_can_be_deleted(client: TestClient):
    source_id = client.post(
        "/api/sources", json={"name": "X", "url": "https://example.com/f.xml"}
    ).json()["id"]

    assert client.delete(f"/api/sources/{source_id}").status_code == 204
    assert client.get("/api/sources").json() == []


def test_patching_an_unknown_source_is_404(client: TestClient):
    assert client.patch("/api/sources/999", json={"name": "X"}).status_code == 404


def test_source_credentials_are_not_echoed_back(client: TestClient):
    """Adapter options can hold passwords, so the API must not return them."""
    response = client.post(
        "/api/sources",
        json={
            "name": "X",
            "url": "https://example.com/f.xml",
            "options": {"password": "hunter2"},
        },
    )
    assert "hunter2" not in response.text
    assert "hunter2" not in client.get("/api/sources").text


def test_entries_can_be_listed_and_filtered(client: TestClient, context: AppContext):
    _add_ready_entry(context, source_name="Tech", episode_id="ep-1")

    assert len(client.get("/api/entries").json()) == 1
    assert len(client.get("/api/entries?status=ready").json()) == 1
    assert client.get("/api/entries?status=failed").json() == []


def test_unknown_entry_status_filter_is_rejected(client: TestClient):
    assert client.get("/api/entries?status=banana").status_code == 400


def test_entry_can_be_retried(client: TestClient, context: AppContext):
    source = context.sources.add(name="A", kind="rss", url="https://example.com/f.xml")
    entry = context.entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid="g",
            title="T",
            article_url="https://example.com/a",
            published_at=utcnow(),
        )
    )
    context.entries.mark_failed(entry.id, error="boom")

    response = client.post(f"/api/entries/{entry.id}/retry")

    assert response.status_code == 200
    assert context.entries.get(entry.id).status.value == "pending"


def test_retrying_an_unknown_entry_is_404(client: TestClient):
    assert client.post("/api/entries/999/retry").status_code == 404


# --- admin token -----------------------------------------------------------


@pytest.fixture
def guarded_client(context: AppContext) -> TestClient:
    context.config = replace(context.config, admin_token="s3cret")
    return TestClient(create_app(ServiceState(context=context)))


def test_admin_endpoints_require_the_token_when_configured(
    guarded_client: TestClient,
):
    assert guarded_client.get("/api/sources").status_code == 401


def test_correct_token_is_accepted(guarded_client: TestClient):
    response = guarded_client.get(
        "/api/sources", headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200


def test_wrong_token_is_rejected(guarded_client: TestClient):
    response = guarded_client.get(
        "/api/sources", headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401


def test_feeds_and_health_stay_public_with_a_token_configured(
    guarded_client: TestClient,
):
    """Podcast clients cannot send an Authorization header, so feeds stay open."""
    assert guarded_client.get("/feeds/all.xml").status_code == 200
    assert guarded_client.get("/feed.xml").status_code == 200
    assert guarded_client.get("/api/health").status_code == 200


# --- backward compatibility ------------------------------------------------


def test_app_without_ingestion_state_still_serves_the_original_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(library, "LIBRARY_PATH", tmp_path / "library")
    _make_episode(tmp_path / "library", "20260604T120000Z_a_aaa1", "A")
    client = TestClient(create_app())

    assert client.get("/feed.xml").status_code == 200
    assert client.get("/audio/20260604T120000Z_a_aaa1.mp3").status_code == 200
    assert client.get("/cover.jpg").status_code == 200
    assert client.get("/").status_code == 200


def test_ingestion_routes_are_absent_without_state(tmp_path: Path):
    client = TestClient(create_app())
    assert client.get("/feeds/all.xml").status_code == 404
    assert client.get("/api/health").status_code == 404


def test_audio_endpoint_serves_the_enclosure(client: TestClient, context: AppContext):
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")
    response = client.get("/audio/20260604T120000Z_a_aaa1.mp3")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"


def test_audio_endpoint_rejects_path_traversal(client: TestClient):
    """Entry ids come from URLs, so they must not escape the library."""
    assert client.get("/audio/..%2F..%2Fetc%2Fpasswd.mp3").status_code == 404


@pytest.mark.parametrize(
    "entry_id",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "a/../../b",
        "..",
        "sub/dir",
        "back\\slash",
    ],
)
def test_unsafe_entry_ids_never_resolve(entry_id: str):
    assert library.get_entry(entry_id) is None
    assert library.is_valid_entry_id(entry_id) is False


def test_real_entry_ids_are_considered_safe():
    assert library.is_valid_entry_id("20260604T120000Z_the_bitter_lesson_a8f31c")


# --- feed token ------------------------------------------------------------


@pytest.fixture
def tokened_client(context: AppContext) -> TestClient:
    context.config = replace(
        context.config, server=replace(context.config.server, feed_token="feed-s3cret")
    )
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")
    return TestClient(create_app(ServiceState(context=context)))


@pytest.mark.parametrize(
    "path",
    [
        "/feed.xml",
        "/feeds/all.xml",
        "/feeds/listen-later.xml",
        "/audio/20260604T120000Z_a_aaa1.mp3",
    ],
)
def test_feed_and_audio_require_the_token(tokened_client: TestClient, path: str):
    assert tokened_client.get(path).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/feed.xml",
        "/feeds/all.xml",
        "/feeds/listen-later.xml",
        "/audio/20260604T120000Z_a_aaa1.mp3",
    ],
)
def test_correct_feed_token_is_accepted(tokened_client: TestClient, path: str):
    assert tokened_client.get(f"{path}?token=feed-s3cret").status_code == 200


def test_wrong_feed_token_is_rejected(tokened_client: TestClient):
    assert tokened_client.get("/feeds/all.xml?token=nope").status_code == 401


def test_enclosure_urls_carry_the_token(tokened_client: TestClient):
    """Otherwise a client could read the feed but not download any episode."""
    body = tokened_client.get("/feeds/all.xml?token=feed-s3cret").text
    assert "audio/20260604T120000Z_a_aaa1.mp3?token=feed-s3cret" in body


def test_cover_art_url_carries_the_token(tokened_client: TestClient):
    body = tokened_client.get("/feeds/all.xml?token=feed-s3cret").text
    assert "cover.jpg?token=feed-s3cret" in body


def test_health_stays_open_so_the_container_probe_works(tokened_client: TestClient):
    assert tokened_client.get("/api/health").status_code == 200


def test_no_token_configured_leaves_feeds_open(client: TestClient):
    assert client.get("/feeds/all.xml").status_code == 200


# --- split audio host ------------------------------------------------------


def test_audio_base_url_overrides_the_enclosure_host(context: AppContext):
    """Lets a public feed point at audio that stays on a private network."""
    context.config = replace(
        context.config,
        server=replace(
            context.config.server,
            public_base_url="https://public.example.ts.net",
            audio_base_url="http://100.64.0.1:3402",
        ),
    )
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")
    client = TestClient(create_app(ServiceState(context=context)))

    body = client.get("/feeds/all.xml").text
    assert "http://100.64.0.1:3402/audio/20260604T120000Z_a_aaa1.mp3" in body
    # The channel link and cover still come from the public host.
    assert "https://public.example.ts.net/cover.jpg" in body


def test_audio_base_url_defaults_to_the_feed_host(
    client: TestClient, context: AppContext
):
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")
    body = client.get("/feeds/all.xml").text
    assert "https://podcast.example.com/audio/20260604T120000Z_a_aaa1.mp3" in body


# --- pause / resume --------------------------------------------------------


def test_worker_can_be_paused_and_resumed_over_http(
    client: TestClient, context: AppContext
):
    assert client.post("/api/worker/pause").json()["worker_paused"] is True
    assert context.settings.worker_paused is True

    assert client.post("/api/worker/resume").json()["worker_paused"] is False
    assert context.settings.worker_paused is False


def test_health_reports_paused_distinctly_from_stopped(
    state: ServiceState, client: TestClient
):
    state.worker_running = True
    assert client.get("/api/health").json()["worker"] == "running"

    client.post("/api/worker/pause")
    payload = client.get("/api/health").json()
    assert payload["worker"] == "paused"
    assert payload["worker_paused"] is True


def test_pause_requires_the_admin_token_when_configured(
    guarded_client: TestClient,
):
    assert guarded_client.post("/api/worker/pause").status_code == 401


# --- download tracking and read sync ---------------------------------------


@pytest.fixture
def downloadable(context: AppContext) -> int:
    _make_episode(context.config.storage.library_path, "20260604T120000Z_a_aaa1", "A")
    source = context.sources.add(
        name="FreshRSS",
        kind="freshrss_api",
        url="https://freshrss.example.com",
        config={"username": "u", "api_password": "p"},
    )
    entry = context.entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid="tag:google.com,2005:reader/item/abc",
            title="A",
            article_url="https://example.com/a",
            published_at=utcnow(),
        )
    )
    context.entries.mark_ready(
        entry.id,
        episode_id="20260604T120000Z_a_aaa1",
        duration_seconds=60.0,
        audio_bytes=8,
    )
    return entry.id


def test_download_is_recorded(client: TestClient, context: AppContext, downloadable):
    assert client.get("/audio/20260604T120000Z_a_aaa1.mp3").status_code == 200
    assert context.entries.get(downloadable).downloaded_at is not None


def test_head_request_is_not_a_download(
    client: TestClient, context: AppContext, downloadable
):
    """Clients probe with HEAD for metadata; that is not consumption."""
    client.head("/audio/20260604T120000Z_a_aaa1.mp3")
    assert context.entries.get(downloadable).downloaded_at is None


def test_small_range_probe_is_not_a_download(
    client: TestClient, context: AppContext, downloadable
):
    client.get("/audio/20260604T120000Z_a_aaa1.mp3", headers={"Range": "bytes=0-1023"})
    assert context.entries.get(downloadable).downloaded_at is None


def test_download_time_is_not_overwritten_by_a_refetch(
    client: TestClient, context: AppContext, downloadable
):
    client.get("/audio/20260604T120000Z_a_aaa1.mp3")
    first = context.entries.get(downloadable).downloaded_at
    client.get("/audio/20260604T120000Z_a_aaa1.mp3")
    assert context.entries.get(downloadable).downloaded_at == first


def test_no_upstream_write_unless_enabled(
    client: TestClient, context: AppContext, downloadable
):
    """Marking read changes state in another application, so it is opt-in."""
    client.get("/audio/20260604T120000Z_a_aaa1.mp3")
    assert context.entries.get(downloadable).marked_read_at is None


# --- hiding downloaded episodes --------------------------------------------


def test_downloaded_episode_leaves_the_feed_after_the_delay(
    context: AppContext, downloadable
):
    context.config = replace(
        context.config,
        server=replace(context.config.server, hide_after_download_hours=24),
    )
    client = TestClient(create_app(ServiceState(context=context)))
    assert len(_feed_items(client)) == 1

    client.get("/audio/20260604T120000Z_a_aaa1.mp3")
    assert len(_feed_items(client)) == 1, "must stay while the delay has not elapsed"

    with context.db.transaction() as conn:
        conn.execute(
            "UPDATE entries SET downloaded_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", downloadable),
        )
    assert _feed_items(client) == []


def test_episodes_stay_listed_when_hiding_is_off(
    client: TestClient, context: AppContext, downloadable
):
    client.get("/audio/20260604T120000Z_a_aaa1.mp3")
    with context.db.transaction() as conn:
        conn.execute(
            "UPDATE entries SET downloaded_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", downloadable),
        )
    assert len(_feed_items(client)) == 1


def test_hidden_episode_audio_is_still_served(context: AppContext, downloadable):
    """The files are kept; only the listing changes."""
    context.config = replace(
        context.config,
        server=replace(context.config.server, hide_after_download_hours=1),
    )
    client = TestClient(create_app(ServiceState(context=context)))
    with context.db.transaction() as conn:
        conn.execute(
            "UPDATE entries SET downloaded_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", downloadable),
        )
    assert _feed_items(client) == []
    assert client.get("/audio/20260604T120000Z_a_aaa1.mp3").status_code == 200


def _feed_items(client: TestClient):
    from xml.etree import ElementTree

    return ElementTree.fromstring(client.get("/feeds/all.xml").text).findall(
        "./channel/item"
    )


def test_redownload_marks_the_article_read_again(
    client: TestClient, context: AppContext, downloadable
):
    """Marking an article unread by hand and fetching it again is deliberate.

    The first download must not permanently suppress the response to later ones.
    """

    client.get("/audio/20260604T120000Z_a_aaa1.mp3")
    first = context.entries.get(downloadable).downloaded_at
    assert first is not None

    # The reader is told it is read; the user then marks it unread by hand.
    context.consumption.mark_read_upstream(downloadable)
    with context.db.transaction() as conn:
        conn.execute(
            "UPDATE entries SET marked_read_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", downloadable),
        )

    assert context.consumption.record_download("20260604T120000Z_a_aaa1") is not None
    # The listing clock still runs from the first fetch, not the latest.
    assert context.entries.get(downloadable).downloaded_at == first


def test_repeated_requests_during_one_download_mark_read_once(
    client: TestClient, context: AppContext, downloadable
):
    """A client fetching in ranges must not trigger a write per range."""
    assert context.consumption.record_download("20260604T120000Z_a_aaa1") is not None
    context.consumption.mark_read_upstream(downloadable)

    assert context.consumption.record_download("20260604T120000Z_a_aaa1") is None
    assert context.consumption.record_download("20260604T120000Z_a_aaa1") is None


def test_download_of_an_unknown_episode_is_ignored(context: AppContext):
    assert context.consumption.record_download("no-such-episode") is None
