"""Podcast feed correctness: validity, GUID stability, escaping, filtering."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytest

from vocast import library
from vocast.ingest.db import Database, open_database
from vocast.ingest.feeds import FeedChannel, build_podcast_rss, collect_episodes
from vocast.ingest.models import FeedEntry
from vocast.ingest.repository import EntryRepository, SourceRepository
from vocast.ingest.timeutils import utcnow

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
BASE = "https://podcast.example.com"


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "library"
    path.mkdir()
    monkeypatch.setattr(library, "LIBRARY_PATH", path)
    return path


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return open_database(tmp_path / "state.db")


@pytest.fixture
def sources(db: Database) -> SourceRepository:
    return SourceRepository(db)


@pytest.fixture
def entries(db: Database) -> EntryRepository:
    return EntryRepository(db)


def _make_episode(
    lib: Path,
    episode_id: str,
    title: str,
    *,
    synthesized_at: str = "2026-06-04T12:00:00+00:00",
    source: str | None = None,
    audio: bytes = b"fake-mp3-bytes",
) -> None:
    entry_dir = lib / episode_id
    entry_dir.mkdir(parents=True)
    (entry_dir / "audio.mp3").write_bytes(audio)
    (entry_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": episode_id,
                "title": title,
                "source": source,
                "synthesized_at": synthesized_at,
                "duration_seconds": 123.4,
                "voice": "af_heart",
                "engine": "kokoro",
            }
        )
    )


def _queue_ready(
    sources: SourceRepository,
    entries: EntryRepository,
    *,
    source_name: str,
    episode_id: str,
    guid: str = "g",
    article_url: str = "https://example.com/article",
    published_at=None,
) -> int:
    source = sources.find_by_url(
        kind="rss", url=f"https://example.com/{source_name}/feed.xml"
    ) or sources.add(
        name=source_name, kind="rss", url=f"https://example.com/{source_name}/feed.xml"
    )
    entry = entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid=guid,
            title=f"Article {guid}",
            article_url=article_url,
            published_at=published_at or utcnow(),
        )
    )
    entries.mark_ready(entry.id, episode_id=episode_id)
    return source.id


def _render(entries: EntryRepository | None, *, source_id: int | None = None) -> str:
    episodes = collect_episodes(entries, base_url=BASE, source_id=source_id)
    return build_podcast_rss(
        FeedChannel(title="vocast", link=BASE, description="d"), episodes
    )


def _items(xml: str) -> list[ElementTree.Element]:
    return ElementTree.fromstring(xml).findall("./channel/item")


# --- validity --------------------------------------------------------------


def test_feed_is_well_formed_xml(lib: Path, entries: EntryRepository):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    root = ElementTree.fromstring(_render(entries))
    assert root.tag == "rss"
    assert root.attrib["version"] == "2.0"


def test_empty_feed_is_still_valid(lib: Path, entries: EntryRepository):
    root = ElementTree.fromstring(_render(entries))
    assert root.find("./channel/title").text == "vocast"
    assert root.findall("./channel/item") == []


def test_required_channel_elements_are_present(lib: Path, entries: EntryRepository):
    channel = ElementTree.fromstring(_render(entries)).find("./channel")
    for tag in ("title", "link", "description", "language"):
        assert channel.find(tag) is not None


def test_item_has_every_required_element(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    _queue_ready(
        sources,
        entries,
        source_name="Tech",
        episode_id="20260604T120000Z_a_aaa111",
        article_url="https://example.com/alpha",
    )

    [item] = _items(_render(entries))

    assert item.find("title").text == "Alpha"
    assert item.find("description").text
    assert item.find("guid").text == "20260604T120000Z_a_aaa111"
    assert item.find("pubDate").text
    assert item.find("link").text == "https://example.com/alpha"
    assert item.find(f"{{{ITUNES_NS}}}duration").text == "123"
    assert item.find(f"{{{ITUNES_NS}}}author").text == "Tech"

    enclosure = item.find("enclosure")
    assert enclosure.attrib["type"] == "audio/mpeg"
    assert enclosure.attrib["length"] == str(len(b"fake-mp3-bytes"))


# --- enclosure URLs --------------------------------------------------------


def test_enclosure_url_is_absolute_and_uses_the_configured_base(
    lib: Path, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    [item] = _items(_render(entries))
    assert item.find("enclosure").attrib["url"] == (
        f"{BASE}/audio/20260604T120000Z_a_aaa111.mp3"
    )


def test_enclosure_length_reflects_the_real_file_size(
    lib: Path, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha", audio=b"x" * 4242)
    [item] = _items(_render(entries))
    assert item.find("enclosure").attrib["length"] == "4242"


def test_missing_audio_reports_zero_length(lib: Path, entries: EntryRepository):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    (lib / "20260604T120000Z_a_aaa111" / "audio.mp3").unlink()

    [item] = _items(_render(entries))
    assert item.find("enclosure").attrib["length"] == "0"


# --- GUID stability --------------------------------------------------------


def test_guid_is_the_library_id_and_is_not_a_permalink(
    lib: Path, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    [item] = _items(_render(entries))
    assert item.find("guid").attrib["isPermaLink"] == "false"
    assert item.find("guid").text == "20260604T120000Z_a_aaa111"


def test_guid_survives_a_rerender(lib: Path, entries: EntryRepository):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    first = _items(_render(entries))[0].find("guid").text
    second = _items(_render(entries))[0].find("guid").text
    assert first == second


def test_guid_does_not_change_when_metadata_changes(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    """Retitling an episode must not make clients re-download it."""
    episode_id = "20260604T120000Z_a_aaa111"
    _make_episode(lib, episode_id, "Original Title")
    _queue_ready(sources, entries, source_name="Tech", episode_id=episode_id)
    before = _items(_render(entries))[0].find("guid").text

    meta = lib / episode_id / "meta.json"
    data = json.loads(meta.read_text())
    data["title"] = "Renamed Title"
    meta.write_text(json.dumps(data))

    item = _items(_render(entries))[0]
    assert item.find("guid").text == before
    assert item.find("title").text == "Renamed Title"


# --- ordering --------------------------------------------------------------


def test_newest_episode_comes_first(lib: Path, entries: EntryRepository):
    _make_episode(
        lib,
        "20260604T120000Z_a_aaa111",
        "Older",
        synthesized_at="2026-06-04T12:00:00+00:00",
    )
    _make_episode(
        lib,
        "20260606T120000Z_b_bbb222",
        "Newer",
        synthesized_at="2026-06-06T12:00:00+00:00",
    )

    titles = [i.find("title").text for i in _items(_render(entries))]
    assert titles == ["Newer", "Older"]


def test_ordering_is_deterministic_across_renders(lib: Path, entries: EntryRepository):
    for index in range(5):
        _make_episode(lib, f"2026060{index}T120000Z_x_a{index}", f"Episode {index}")

    first = [i.find("guid").text for i in _items(_render(entries))]
    second = [i.find("guid").text for i in _items(_render(entries))]
    assert first == second


# --- source filtering ------------------------------------------------------


def test_source_feed_contains_only_that_sources_episodes(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "From Tech")
    _make_episode(lib, "20260605T120000Z_b_bbb222", "From News")
    tech_id = _queue_ready(
        sources, entries, source_name="Tech", episode_id="20260604T120000Z_a_aaa111"
    )
    _queue_ready(
        sources, entries, source_name="News", episode_id="20260605T120000Z_b_bbb222"
    )

    titles = [i.find("title").text for i in _items(_render(entries, source_id=tech_id))]
    assert titles == ["From Tech"]


def test_combined_feed_includes_manual_and_ingested_episodes(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    """`vocast add` episodes must not vanish once feeds are configured."""
    _make_episode(lib, "20260604T120000Z_m_manual", "Manually Added")
    _make_episode(lib, "20260605T120000Z_i_ingest", "From A Feed")
    _queue_ready(
        sources, entries, source_name="Tech", episode_id="20260605T120000Z_i_ingest"
    )

    titles = {i.find("title").text for i in _items(_render(entries))}
    assert titles == {"Manually Added", "From A Feed"}


def test_source_feed_excludes_manual_episodes(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_m_manual", "Manually Added")
    _make_episode(lib, "20260605T120000Z_i_ingest", "From A Feed")
    tech_id = _queue_ready(
        sources, entries, source_name="Tech", episode_id="20260605T120000Z_i_ingest"
    )

    titles = [i.find("title").text for i in _items(_render(entries, source_id=tech_id))]
    assert titles == ["From A Feed"]


def test_episode_not_yet_generated_is_absent_from_the_feed(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    source = sources.add(name="Tech", kind="rss", url="https://example.com/f.xml")
    entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid="pending",
            title="Not Yet",
            article_url="https://example.com/pending",
            published_at=utcnow(),
        )
    )
    assert _items(_render(entries)) == []


# --- escaping --------------------------------------------------------------


def test_special_characters_in_a_title_are_escaped(lib: Path, entries: EntryRepository):
    _make_episode(lib, "20260604T120000Z_a_aaa111", 'Tom & Jerry <b>"best"</b>')

    xml = _render(entries)
    assert "&amp;" in xml
    assert "<b>" not in xml
    # Parsing round-trips to the original text, proving escaping not stripping.
    assert _items(xml)[0].find("title").text == 'Tom & Jerry <b>"best"</b>'


def test_ampersand_in_an_article_url_is_escaped(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    _queue_ready(
        sources,
        entries,
        source_name="Tech",
        episode_id="20260604T120000Z_a_aaa111",
        article_url="https://example.com/a?x=1&y=2",
    )

    item = _items(_render(entries))[0]
    assert item.find("link").text == "https://example.com/a?x=1&y=2"


def test_quotes_in_a_source_name_do_not_break_attributes(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", 'A "quoted" title')
    _queue_ready(
        sources,
        entries,
        source_name='Bob\'s "Feed"',
        episode_id="20260604T120000Z_a_aaa111",
    )

    # Well-formedness is the assertion: a bad attribute quote would raise here.
    item = _items(_render(entries))[0]
    assert item.find(f"{{{ITUNES_NS}}}author").text == 'Bob\'s "Feed"'


def test_control_characters_in_a_title_do_not_produce_invalid_xml(
    lib: Path, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Weird\ttitle\nwith breaks")
    assert _items(_render(entries))


# --- description -----------------------------------------------------------


def test_description_names_the_source_and_links_the_original(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    _queue_ready(
        sources,
        entries,
        source_name="Tech Weekly",
        episode_id="20260604T120000Z_a_aaa111",
        article_url="https://example.com/alpha",
        published_at=utcnow() - timedelta(days=1),
    )

    description = _items(_render(entries))[0].find("description").text
    assert "Tech Weekly" in description
    assert "https://example.com/alpha" in description


def test_manual_episode_description_falls_back_to_its_source_url(
    lib: Path, entries: EntryRepository
):
    _make_episode(
        lib, "20260604T120000Z_a_aaa111", "Alpha", source="https://example.com/manual"
    )
    description = _items(_render(entries))[0].find("description").text
    assert "https://example.com/manual" in description


def test_feed_works_without_a_database(lib: Path):
    """`vocast serve` renders the library alone, with no ingestion state."""
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha")
    assert len(_items(_render(None))) == 1


def test_description_has_no_leading_or_trailing_blank_lines(
    lib: Path, sources: SourceRepository, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Alpha", source="https://ex.com/a")
    manual = _items(_render(entries))[0].find("description").text
    assert manual == manual.strip()

    _queue_ready(
        sources,
        entries,
        source_name="Tech",
        episode_id="20260604T120000Z_a_aaa111",
        article_url="https://example.com/a",
    )
    ingested = _items(_render(entries))[0].find("description").text
    assert ingested == ingested.strip()
    assert "\n\n\n" not in ingested


def test_description_falls_back_to_the_title_with_no_metadata(
    lib: Path, entries: EntryRepository
):
    _make_episode(lib, "20260604T120000Z_a_aaa111", "Just A Title", source=None)
    assert _items(_render(entries))[0].find("description").text == "Just A Title"
