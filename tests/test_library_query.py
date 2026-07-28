from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from vocast.ingest.db import open_database
from vocast.ingest.library_query import (
    LibraryQuery,
    LibraryQueryService,
    alphabetical_key,
)
from vocast.ingest.models import EntryStatus, FeedEntry
from vocast.ingest.repository import (
    EntryRepository,
    PlaylistRepository,
    SourceRepository,
)
from vocast.ingest.timeutils import to_iso, utcnow


@pytest.fixture
def library_data(tmp_path: Path):
    db = open_database(tmp_path / "library.db")
    sources = SourceRepository(db)
    entries = EntryRepository(db)
    first_source = sources.add(
        name="FreshRSS", kind="freshrss_api", url="https://reader.example.com"
    )
    second_source = sources.add(
        name="Direct Feed", kind="rss", url="https://direct.example.com/feed"
    )
    now = utcnow()

    def add(
        source_id: int,
        guid: str,
        title: str,
        *,
        origin: str,
        author: str,
        published_offset: int,
        duration: int,
        status: EntryStatus = EntryStatus.READY,
        read: bool = False,
    ):
        entry = entries.insert_if_new(
            FeedEntry(
                source_id=source_id,
                external_guid=guid,
                title=title,
                article_url=f"https://articles.example.com/{guid}",
                published_at=now + timedelta(days=published_offset),
                author=author,
                origin_name=origin,
            )
        )
        if status is EntryStatus.READY:
            entries.mark_ready(
                entry.id,
                episode_id=f"episode-{guid}",
                duration_seconds=duration,
                audio_bytes=duration * 100,
            )
        else:
            entries.set_status(entry.id, status)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE entries SET duration_seconds = ? WHERE id = ?",
                    (duration, entry.id),
                )
        if read:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE entries SET read_at = ? WHERE id = ?",
                    (to_iso(now), entry.id),
                )
        return entry

    alpha = add(
        first_source.id,
        "alpha",
        "Alpha systems",
        origin="The Daily Planet",
        author="Ada Lovelace",
        published_offset=-2,
        duration=600,
    )
    beta = add(
        first_source.id,
        "beta",
        "Beta release",
        origin="Science Weekly",
        author="Grace Hopper",
        published_offset=-1,
        duration=1200,
        read=True,
    )
    gamma = add(
        second_source.id,
        "gamma",
        "Gamma notes",
        origin="Direct Publication",
        author="Linus Torvalds",
        published_offset=0,
        duration=1800,
        status=EntryStatus.FAILED,
    )
    PlaylistRepository(db).add_entry("listen-later", beta.id)
    return (
        db,
        LibraryQueryService(db),
        first_source,
        second_source,
        alpha,
        beta,
        gamma,
        now,
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (LibraryQuery(search=" alpha "), ["Alpha systems"]),
        (LibraryQuery(search="daily planet"), ["Alpha systems"]),
        (LibraryQuery(search="Grace Hopper"), ["Beta release"]),
        (LibraryQuery(search="Direct Feed"), ["Gamma notes"]),
        (LibraryQuery(search="%"), []),
    ],
)
def test_search_matches_library_metadata(library_data, query, expected):
    _, service, *_ = library_data
    assert [item.title for item in service.search(query).items] == expected


def test_source_and_origin_filters(library_data):
    _, service, first_source, _, *_ = library_data
    by_source = service.search(LibraryQuery(source_id=first_source.id))
    by_origin = service.search(LibraryQuery(origin_id="science weekly"))

    assert {item.title for item in by_source.items} == {"Alpha systems", "Beta release"}
    assert [item.title for item in by_origin.items] == ["Beta release"]


def test_status_queue_and_read_filters(library_data):
    _, service, *_ = library_data
    assert [item.title for item in service.search(LibraryQuery(queued=True)).items] == [
        "Beta release"
    ]
    assert {
        item.title for item in service.search(LibraryQuery(queued=False)).items
    } == {
        "Alpha systems",
        "Gamma notes",
    }
    assert [
        item.title for item in service.search(LibraryQuery(read=True)).items
    ] == ["Beta release"]
    assert [
        item.title
        for item in service.search(LibraryQuery(status=EntryStatus.FAILED)).items
    ] == ["Gamma notes"]


def test_count_agrees_with_search_without_paging(library_data):
    """The mobile source list needs totals, not rows, per destination."""
    _, service, *_ = library_data

    assert service.count(LibraryQuery()) == 3
    assert service.count(LibraryQuery(read=False)) == 2
    assert service.count(LibraryQuery(queued=True, read=True)) == 1
    assert service.count(LibraryQuery(search="alpha", page_size=1)) == 1


def test_date_and_duration_ranges(library_data):
    _, service, *_, now = library_data
    dates = service.search(
        LibraryQuery(
            published_after=now - timedelta(days=1, hours=1),
            published_before=now - timedelta(hours=1),
        )
    )
    durations = service.search(
        LibraryQuery(min_duration_seconds=1000, max_duration_seconds=1500)
    )

    assert [item.title for item in dates.items] == ["Beta release"]
    assert [item.title for item in durations.items] == ["Beta release"]


def test_sorting_pagination_and_total_count(library_data):
    _, service, *_ = library_data
    first_page = service.search(LibraryQuery(sort="title_asc", page=1, page_size=2))
    second_page = service.search(LibraryQuery(sort="title_asc", page=2, page_size=2))

    assert [item.title for item in first_page.items] == [
        "Alpha systems",
        "Beta release",
    ]
    assert [item.title for item in second_page.items] == ["Gamma notes"]
    assert first_page.total == second_page.total == 3
    assert first_page.pages == 2


def test_invalid_page_values_are_bounded(library_data):
    db, _, *_ = library_data
    service = LibraryQueryService(db, default_page_size=2, max_page_size=2)

    defaulted = service.search(LibraryQuery(page=-4, page_size=0))
    capped = service.search(LibraryQuery(page_size=999))
    fallback_sort = service.search(LibraryQuery(sort="DROP TABLE entries"))

    assert defaulted.query.page == 1
    assert defaulted.query.page_size == capped.query.page_size == 2
    assert fallback_sort.query.sort == "published_desc"
    assert fallback_sort.total == 3


def test_filter_options_are_distinct_and_sorted(library_data):
    _, service, first_source, second_source, *_ = library_data
    assert [source.id for source in service.sources()] == [
        first_source.id,
        second_source.id,
    ]
    assert [origin.name for origin in service.origins()] == [
        "Direct Publication",
        "Science Weekly",
        "The Daily Planet",
    ]


def test_search_and_origin_ids_use_unicode_casefolding(library_data):
    db, service, first_source, *_ = library_data
    entries = EntryRepository(db)
    entry = entries.insert_if_new(
        FeedEntry(
            source_id=first_source.id,
            external_guid="unicode",
            title="Ecole des donnees".replace("E", "É", 1),
            article_url="https://example.com/unicode",
            published_at=utcnow(),
            origin_name="Cafe Revue".replace("e", "é", 1),
        )
    )

    by_title = service.search(LibraryQuery(search="éCOLE"))
    by_origin = service.search(LibraryQuery(origin_id="CAFÉ REVUE"))

    assert [item.entry_id for item in by_title.items] == [entry.id]
    assert [item.entry_id for item in by_origin.items] == [entry.id]
    assert by_origin.items[0].origin_id == "café revue"


def test_every_publication_appears_in_the_facet(library_data):
    """A cap here silently hid publications from the filter, which made them
    unreachable rather than merely unlisted."""
    db, service = library_data[0], library_data[1]
    sources, entries = SourceRepository(db), EntryRepository(db)
    source = sources.add(name="Bulk", kind="rss", url="https://bulk.example.com/feed")
    for n in range(60):
        entries.insert_if_new(
            FeedEntry(
                source_id=source.id,
                external_guid=f"bulk-{n}",
                title=f"Article {n}",
                article_url=f"https://articles.example.com/bulk-{n}",
                published_at=utcnow(),
                author="Author",
                origin_name=f"Publication {n:02d}",
            )
        )

    names = {origin.name for origin in service.facets().origins}

    assert len([n for n in names if n.startswith("Publication ")]) == 60
    assert "Publication 59" in names


def test_publications_are_listed_alphabetically(library_data):
    """With all 80-odd publications shown, alphabetical order is what makes a
    specific one findable; ordering by article count is only useful for a short
    top-N list."""
    db, service = library_data[0], library_data[1]
    entries = EntryRepository(db)
    source = SourceRepository(db).add(
        name="Bulk", kind="rss", url="https://bulk.example.com/feed"
    )
    for guid, origin in enumerate(["zebra weekly", "Ácme Review", "middle Post"]):
        for copy in range(guid + 2):
            entries.insert_if_new(
                FeedEntry(
                    source_id=source.id,
                    external_guid=f"{origin}-{copy}",
                    title="Article",
                    article_url=f"https://articles.example.com/{origin}-{copy}",
                    published_at=utcnow(),
                    author="Author",
                    origin_name=origin,
                )
            )

    names = [o.name for o in service.facets().origins]

    assert (
        names.index("Ácme Review")
        < names.index("middle Post")
        < names.index("zebra weekly")
    )


def test_accented_publications_sort_under_their_base_letter():
    """SQLite orders by codepoint, which would put any accented initial after
    "z" and so far from where it is looked for."""
    names = ["Zebra", "Ácme", "Middle", "Ørsted", "apple"]

    assert sorted(names, key=alphabetical_key) == [
        "Ácme",
        "apple",
        "Middle",
        "Ørsted",
        "Zebra",
    ]


def test_publication_counts_follow_the_active_filter(library_data):
    """A count that ignores the filter contradicts the list beside it: with
    failed selected, a publication showing 12 that yields one row is misleading."""
    service = library_data[1]

    unfiltered = {o.name: o.count for o in service.facets().origins}
    ready_only = {
        o.name: o.count
        for o in service.facets(LibraryQuery(status=EntryStatus.READY)).origins
    }

    assert unfiltered["Direct Publication"] == 1
    assert "Direct Publication" not in ready_only
    assert ready_only["The Daily Planet"] == 1


def test_selecting_a_publication_leaves_the_others_countable(library_data):
    """A facet must exclude its own dimension, or every other publication reads
    zero and there is no way to switch."""
    service = library_data[1]

    origins = service.facets(LibraryQuery(origin_id="the daily planet")).origins
    counts = {o.name: o.count for o in origins}

    assert counts["The Daily Planet"] == 1
    assert counts["Science Weekly"] == 1


def test_search_narrows_publication_counts(library_data):
    service = library_data[1]

    origins = service.facets(LibraryQuery(search="Alpha systems")).origins

    assert {o.name: o.count for o in origins} == {"The Daily Planet": 1}


def test_queue_filter_narrows_publication_counts(library_data):
    """The queued clause needs the playlist join, which the facet query must
    carry too or the SQL refers to a table it never joined."""
    service = library_data[1]

    origins = service.facets(LibraryQuery(queued=True)).origins

    assert {o.name: o.count for o in origins} == {"Science Weekly": 1}


def test_status_counts_stay_global_while_publications_narrow(library_data):
    """Status counts are navigation: they answer how much is left overall, so
    they must not shrink when a publication is selected."""
    service = library_data[1]

    facets = service.facets(LibraryQuery(origin_id="the daily planet"))

    assert facets.by_status == service.facets().by_status
    assert facets.total == service.facets().total
