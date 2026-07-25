"""Feed URL to playable podcast episode, with network and TTS mocked.

This walks the acceptance path: poll a feed, insert once, generate, publish,
serve the enclosure, restart, and confirm nothing is regenerated.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import pytest
from fastapi.testclient import TestClient

from vocast import library
from vocast.engines import AudioChunk
from vocast.ingest import generator as generator_module
from vocast.ingest.adapters.rss import GenericRSSAdapter
from vocast.ingest.api import ServiceState
from vocast.ingest.config import (
    Config,
    DatabaseConfig,
    ServerConfig,
    StorageConfig,
    WorkerConfig,
)
from vocast.ingest.context import AppContext
from vocast.ingest.generator import VocastEpisodeGenerator
from vocast.ingest.nethttp import Response
from vocast.ingest.poller import Poller
from vocast.ingest.worker import Worker
from vocast.server import create_app

BASE = "https://podcast.example.com"

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Example Blog</title>
  <link>https://example.com/</link>
  <item>
    <title>The Bitter Lesson</title>
    <link>https://example.com/bitter-lesson</link>
    <guid>post-1</guid>
    <pubDate>Wed, 04 Jun 2025 12:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

SECOND_ITEM = """
  <item>
    <title>Why RSS Still Matters</title>
    <link>https://example.com/why-rss</link>
    <guid>post-2</guid>
    <pubDate>Thu, 05 Jun 2025 12:00:00 GMT</pubDate>
  </item>"""

ARTICLE_TEXT = "This is a real article body. " * 40


class FakeEngine:
    sample_rate = 24000
    max_chars = 1800
    default_voice = "af_heart"

    def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
        return AudioChunk(np.zeros(24000, dtype=np.float32), self.sample_rate)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseConfig(path=tmp_path / "state.db"),
        storage=StorageConfig(library_path=tmp_path / "library"),
        server=ServerConfig(public_base_url=BASE),
        worker=WorkerConfig(base_retry_minutes=1),
    )


@pytest.fixture
def feed_body() -> dict[str, str]:
    """Mutable holder so a test can change what the feed serves."""
    return {"xml": FEED_XML}


@pytest.fixture
def adapter_factory(feed_body: dict[str, str]):
    def fetcher(url, **kwargs):
        return Response(url=url, status=200, body=feed_body["xml"].encode("utf-8"))

    def factory(source, **kwargs):
        return GenericRSSAdapter(source, fetcher=fetcher)

    return factory


@pytest.fixture
def stub_article(monkeypatch: pytest.MonkeyPatch):
    """Stand in for trafilatura extraction over the network."""
    fetched: list[str] = []

    def fake_fetch_article(url, *, html_fetcher=None):
        fetched.append(url)
        return f"Extracted {url}", ARTICLE_TEXT, None

    monkeypatch.setattr(generator_module, "fetch_article", fake_fetch_article)
    return fetched


def _build(config: Config, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    context = AppContext.create(config)
    monkeypatch.setattr(library, "LIBRARY_PATH", config.storage.library_path)
    return context


def _worker(context: AppContext) -> Worker:
    return Worker(
        entries=context.entries,
        generator=VocastEpisodeGenerator(engine=FakeEngine()),
        config=context.config.worker,
    )


def _items(xml: str):
    return ElementTree.fromstring(xml).findall("./channel/item")


def test_feed_url_becomes_a_playable_episode(
    config: Config,
    adapter_factory,
    stub_article: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    context = _build(config, monkeypatch)
    source = context.sources.add(
        name="Example Blog", kind="rss", url="https://example.com/feed.xml"
    )
    poller = Poller(
        sources=context.sources,
        entries=context.entries,
        adapter_factory=adapter_factory,
    )

    # 1. The poller discovers the article exactly once.
    assert poller.poll_due().inserted == 1
    assert poller.poll_all().inserted == 0

    # 2. The worker extracts it and generates audio.
    outcome = _worker(context).process_next()
    assert outcome.ok
    assert stub_article == ["https://example.com/bitter-lesson"]

    # 3. It appears in the combined and per-source feeds.
    client = TestClient(create_app(ServiceState(context=context)))
    combined = client.get("/feeds/all.xml")
    assert combined.status_code == 200
    [item] = _items(combined.text)
    assert item.find("title").text == "Example Blog - The Bitter Lesson"
    assert item.find("link").text == "https://example.com/bitter-lesson"

    per_source = client.get(f"/feeds/source/{source.id}.xml")
    assert len(_items(per_source.text)) == 1

    # 4. The enclosure URL is absolute and actually serves audio.
    enclosure = item.find("enclosure").attrib["url"]
    assert enclosure.startswith(f"{BASE}/audio/")
    assert int(enclosure_length := item.find("enclosure").attrib["length"]) > 0

    audio = client.get("/audio/" + enclosure.rsplit("/", 1)[1])
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"
    assert len(audio.content) == int(enclosure_length)


def test_restart_preserves_state_and_does_not_regenerate(
    config: Config,
    adapter_factory,
    stub_article: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    context = _build(config, monkeypatch)
    context.sources.add(
        name="Example Blog", kind="rss", url="https://example.com/feed.xml"
    )
    poller = Poller(
        sources=context.sources,
        entries=context.entries,
        adapter_factory=adapter_factory,
    )
    poller.poll_all()
    _worker(context).drain()
    context.close()

    # Reopen everything, as a container restart would.
    reopened = _build(config, monkeypatch)
    reopened_poller = Poller(
        sources=reopened.sources,
        entries=reopened.entries,
        adapter_factory=adapter_factory,
    )

    assert reopened_poller.poll_all().inserted == 0
    assert _worker(reopened).drain() == []
    assert len(stub_article) == 1
    assert len(library.list_entries()) == 1


def test_newly_published_article_becomes_a_second_episode(
    config: Config,
    adapter_factory,
    feed_body: dict[str, str],
    stub_article: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    context = _build(config, monkeypatch)
    context.sources.add(
        name="Example Blog", kind="rss", url="https://example.com/feed.xml"
    )
    poller = Poller(
        sources=context.sources,
        entries=context.entries,
        adapter_factory=adapter_factory,
    )
    poller.poll_all()
    _worker(context).drain()

    feed_body["xml"] = FEED_XML.replace("</channel>", SECOND_ITEM + "\n</channel>")
    assert poller.poll_all().inserted == 1
    _worker(context).drain()

    client = TestClient(create_app(ServiceState(context=context)))
    titles = {i.find("title").text for i in _items(client.get("/feeds/all.xml").text)}
    assert titles == {
        "Example Blog - The Bitter Lesson",
        "Example Blog - Why RSS Still Matters",
    }


def test_transient_failure_is_retried_then_succeeds(
    config: Config,
    adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    from datetime import timedelta

    from vocast.ingest.nethttp import FetchError
    from vocast.ingest.timeutils import utcnow

    context = _build(config, monkeypatch)
    context.sources.add(
        name="Example Blog", kind="rss", url="https://example.com/feed.xml"
    )
    Poller(
        sources=context.sources,
        entries=context.entries,
        adapter_factory=adapter_factory,
    ).poll_all()

    attempts: list[str] = []

    def flaky_fetch_article(url, *, html_fetcher=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise FetchError("HTTP 503 Service Unavailable from " + url)
        return "Recovered", ARTICLE_TEXT, None

    monkeypatch.setattr(generator_module, "fetch_article", flaky_fetch_article)
    worker = _worker(context)

    first = worker.process_next()
    assert first.retrying
    assert context.entries.get(first.entry_id).status.value == "pending"

    second = worker.process_next(now=utcnow() + timedelta(minutes=5))
    assert second.ok
    assert len(attempts) == 2


def test_failed_entry_is_visible_and_can_be_retried_by_hand(
    config: Config,
    adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    context = _build(config, monkeypatch)
    context.sources.add(
        name="Example Blog", kind="rss", url="https://example.com/feed.xml"
    )
    Poller(
        sources=context.sources,
        entries=context.entries,
        adapter_factory=adapter_factory,
    ).poll_all()

    def paywalled(url, *, html_fetcher=None):
        return "Paywall", "Subscribe to continue reading.", None

    monkeypatch.setattr(generator_module, "fetch_article", paywalled)
    worker = _worker(context)
    outcome = worker.process_next()

    assert not outcome.ok
    client = TestClient(create_app(ServiceState(context=context)))
    failed = client.get("/api/entries?status=failed").json()
    assert len(failed) == 1
    assert "below the" in failed[0]["error_message"]
    assert client.get("/api/health").json()["failed"] == 1

    # Once the article is readable, a manual retry produces the episode.
    monkeypatch.setattr(
        generator_module,
        "fetch_article",
        lambda url, *, html_fetcher=None: ("Now Readable", ARTICLE_TEXT, None),
    )
    assert client.post(f"/api/entries/{failed[0]['id']}/retry").status_code == 200
    assert worker.process_next().ok
    assert len(_items(client.get("/feeds/all.xml").text)) == 1


def test_manual_add_still_works_alongside_ingestion(
    config: Config,
    adapter_factory,
    stub_article: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    """A manually added article and an ingested one coexist in one feed."""
    context = _build(config, monkeypatch)
    context.sources.add(
        name="Example Blog", kind="rss", url="https://example.com/feed.xml"
    )
    Poller(
        sources=context.sources,
        entries=context.entries,
        adapter_factory=adapter_factory,
    ).poll_all()
    _worker(context).drain()

    manual = library.add_entry(
        title="Hand Added",
        chunk=AudioChunk(np.zeros(2400, dtype=np.float32), 24000),
        voice="af_heart",
        engine="kokoro",
        source="https://example.com/manual",
    )

    client = TestClient(create_app(ServiceState(context=context)))
    titles = {i.find("title").text for i in _items(client.get("/feeds/all.xml").text)}
    assert titles == {"Example Blog - The Bitter Lesson", "Hand Added"}
    assert client.get(f"/audio/{manual.id}.mp3").status_code == 200
