from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vocast import library
from vocast.ingest.api import ServiceState
from vocast.ingest.config import Config, DatabaseConfig, ServerConfig, StorageConfig
from vocast.ingest.context import AppContext
from vocast.ingest.models import EntryStatus, FeedEntry
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
    published_at: datetime | None = None,
    status: EntryStatus = EntryStatus.PENDING,
    duration_seconds: float | None = None,
    progress: tuple[int, int] | None = None,
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
            published_at=published_at or utcnow(),
            author="Ada Author",
            origin_name=origin,
        )
    )
    if status is EntryStatus.READY:
        context.entries.mark_ready(
            entry.id,
            episode_id=f"episode-{entry.id}",
            duration_seconds=duration_seconds,
            audio_bytes=1000,
        )
    elif status is not EntryStatus.PENDING:
        context.entries.set_status(entry.id, status)
    if progress is not None:
        context.entries.record_progress(entry.id, *progress)
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


def _bar_title(body: str) -> str:
    match = re.search(r'<h1 class="bartitle">\s*(.*?)\s*</h1>', body, re.DOTALL)
    assert match, "no title in the navigation bar"
    return match.group(1)


def _nav_bar(body: str) -> str:
    return body.split('<header class="nav">', 1)[1].split("</header>", 1)[0]


def _leading_slot(body: str) -> str:
    return body.split('<div class="slot leading">', 1)[1].split("</div>", 1)[0].strip()


def _css_rule(body: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\{{(.*?)\}}", body, re.DOTALL)
    assert match, f"no rule for {selector}"
    return " ".join(match.group(1).split())


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
def test_the_phone_pages_are_not_reachable_from_the_internet(
    client: TestClient, context: AppContext, path: str
):
    """This is a reader for the tailnet. Only the podcast is published, and the
    token does not buy access to anything else."""
    _add_entry(context, title="An article")
    _with_token(context)

    assert client.get(path, headers=FUNNEL).status_code == 404
    assert client.get(f"{path}?token=feed-secret", headers=FUNNEL).status_code == 404


def test_a_token_in_the_url_becomes_a_cookie_so_navigation_keeps_working(
    client: TestClient, context: AppContext
):
    """Links between the two pages carry no token; the cookie is what does.

    Exercised on the tailnet, which is the only place these pages are served.
    The exchange still exists because a token in the URL should not linger in
    history wherever it is used.
    """
    _add_entry(context, title="An article")
    _with_token(context)

    landing = client.get(
        f"{SOURCES_PAGE}?token=feed-secret", follow_redirects=False
    )

    assert landing.status_code == 303
    assert landing.headers["location"] == SOURCES_PAGE
    assert "feed-secret" not in landing.headers["location"]
    assert client.get(ARTICLES_PAGE).status_code == 200


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


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_refresh_is_reachable_from_both_pages_and_reloads_without_javascript(
    client: TestClient, context: AppContext, path: str
):
    """Falls back to a plain reload; the script upgrades it to force the sync."""
    _add_entry(context, title="An article")

    body = client.get(path, params={"filter": "all"}).text
    control = _tag(body, "data-refresh")

    assert control.startswith("<a")
    assert f'href="{path}?filter=all"' in control
    assert 'aria-label="Refresh"' in control
    assert "/api/read-sync" in body


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_the_title_shares_the_bar_with_refresh(
    client: TestClient, context: AppContext, path: str
):
    _add_entry(context, title="An article")

    bar = _nav_bar(client.get(path).text)

    assert '<h1 class="bartitle">' in bar
    assert "data-refresh" in bar


def test_the_title_is_centred_on_the_bar_not_on_the_space_left_over(
    client: TestClient, context: AppContext
):
    """Centring it in the leftover space would put it off centre on page two,
    which has a back button, and drift as the selected title changed."""
    _add_entry(context, title="An article")

    rule = _css_rule(client.get(ARTICLES_PAGE).text, ".bartitle")

    assert "position:absolute" in rule
    assert "left:52px" in rule
    assert "right:52px" in rule


def test_the_leading_slot_is_empty_on_page_one_and_the_way_back_on_page_two(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="An article")

    assert _leading_slot(client.get(SOURCES_PAGE).text) == ""
    assert 'class="back"' in _leading_slot(client.get(ARTICLES_PAGE).text)


def test_a_long_title_truncates_rather_than_moving_the_controls(
    client: TestClient, context: AppContext
):
    publication = "The Exceedingly Long Quarterly Review of Everything At Once"
    _add_entry(context, title="An article", origin=publication)

    body = client.get(ARTICLES_PAGE, params={"origin_id": publication.casefold()}).text
    rule = _css_rule(body, ".bartitle")

    assert _bar_title(body) == publication
    assert "white-space:nowrap" in rule
    assert "text-overflow:ellipsis" in rule


def test_refresh_returns_to_the_page_it_was_pressed_on(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="An article", origin="Daily News")

    control = _tag(
        client.get(
            ARTICLES_PAGE, params={"origin_id": "daily news", "search": "article"}
        ).text,
        "data-refresh",
    )

    assert "origin_id=daily+news" in control
    assert "search=article" in control


def test_sources_page_lists_the_pipeline_statuses_with_counts(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Narrated", status=EntryStatus.READY)
    _add_entry(context, title="Broken", status=EntryStatus.FAILED)

    body = client.get(SOURCES_PAGE).text

    assert _count_beside(body, "Ready") == 1
    assert _count_beside(body, "Failed") == 1
    assert _count_beside(body, "Pending") == 0
    assert _count_beside(body, "Processing") == 0
    assert f"{ARTICLES_PAGE}?filter=unread&amp;status=ready" in body


def test_end_state_statuses_are_listed_only_when_they_hold_something(
    client: TestClient, context: AppContext
):
    """Nobody is waiting on an ignored article, so an empty row is furniture."""
    _add_entry(context, title="Narrated", status=EntryStatus.READY)

    without = client.get(SOURCES_PAGE).text
    _add_entry(context, title="Skipped", status=EntryStatus.IGNORED)
    with_ignored = client.get(SOURCES_PAGE).text

    assert "Ignored" not in without
    assert "Expired" not in without
    assert _count_beside(with_ignored, "Ignored") == 1
    assert "Expired" not in with_ignored


def test_the_status_group_sits_between_the_destinations_and_the_publications(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Narrated", origin="Daily News")

    body = client.get(SOURCES_PAGE).text

    assert body.index("Listen Later") < body.index(">Status<")
    assert body.index(">Status<") < body.index(">Publications<")


def test_status_counts_follow_the_active_filter(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="Narrated and read", status=EntryStatus.READY, read=True)
    _add_entry(context, title="Narrated", status=EntryStatus.READY)

    unread = client.get(SOURCES_PAGE, params={"filter": "unread"}).text
    read = client.get(SOURCES_PAGE, params={"filter": "read"}).text
    everything = client.get(SOURCES_PAGE, params={"filter": "all"}).text

    assert _count_beside(unread, "Ready") == 1
    assert _count_beside(read, "Ready") == 1
    assert _count_beside(everything, "Ready") == 2


def test_a_status_page_narrows_to_that_status(client: TestClient, context: AppContext):
    _add_entry(context, title="Narrated", status=EntryStatus.READY)
    _add_entry(context, title="Broken", status=EntryStatus.FAILED)

    body = client.get(ARTICLES_PAGE, params={"status": "failed"}).text

    assert "Broken" in body
    assert "Narrated" not in body
    assert _bar_title(body) == "Failed"


def test_status_and_the_read_filter_compose(client: TestClient, context: AppContext):
    _add_entry(context, title="Narrated and read", status=EntryStatus.READY, read=True)
    _add_entry(context, title="Narrated", status=EntryStatus.READY)
    _add_entry(context, title="Broken", status=EntryStatus.FAILED)

    body = client.get(ARTICLES_PAGE, params={"status": "ready", "filter": "read"}).text

    assert "Narrated and read" in body
    assert ">Narrated<" not in body
    assert "Broken" not in body


def test_an_unrecognised_status_falls_back_to_the_whole_library(
    client: TestClient, context: AppContext
):
    """A shared link should still show a library, not an error page."""
    _add_entry(context, title="An article")

    body = client.get(ARTICLES_PAGE, params={"status": "half-baked"}).text

    assert "An article" in body
    assert _bar_title(body) == "Library"


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


def _segments(body: str) -> list[tuple[str, str, bool]]:
    """The (href, label, selected) of each filter segment, in rendered order."""
    return [
        (href, label.strip(), 'aria-current="true"' in attributes)
        for href, attributes, label in re.findall(
            r'<a class="seg" href="([^"]+)"([^>]*)>([^<]+)</a>', body
        )
    ]


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_each_filter_is_one_tap_away_and_the_active_one_is_marked(
    client: TestClient, context: AppContext, path: str
):
    """Cycling made two of the three destinations cost two taps and a guess."""
    _add_entry(context, title="An article")

    segments = _segments(client.get(path, params={"filter": "read"}).text)

    assert [label for _, label, _ in segments] == ["Unread", "Read", "All"]
    assert [selected for _, _, selected in segments] == [False, True, False]
    assert [href.rsplit("filter=", 1)[1].split("&")[0] for href, _, _ in segments] == [
        "unread",
        "read",
        "all",
    ]


def test_filter_segments_keep_the_selection_they_were_tapped_from(
    client: TestClient, context: AppContext
):
    _add_entry(context, title="An article", origin="Daily News")

    body = client.get(
        ARTICLES_PAGE, params={"origin_id": "daily news", "status": "pending"}
    ).text

    for href, _, _ in _segments(body):
        assert "origin_id=daily+news" in href
        assert "status=pending" in href


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


def _meta_of(body: str, title: str) -> str:
    """The metadata line rendered under one article's title."""
    after = body.split(f'<span class="title">{title}</span>', 1)
    assert len(after) == 2, f"no row titled {title!r}"
    return after[1].split("</a>", 1)[0]


def test_dates_carry_the_year(client: TestClient, context: AppContext):
    """The backlog reaches back years; a bare month and day makes a 2016
    article look like this week's."""
    old = datetime(2016, 3, 7, 12, 0, tzinfo=timezone.utc)
    _add_entry(context, title="From the archive", published_at=old)
    _add_entry(context, title="From today")

    body = client.get(ARTICLES_PAGE).text

    assert "Mar 07, 2016" in _meta_of(body, "From the archive")
    assert utcnow().strftime("%b %d, %Y") in _meta_of(body, "From today")


def test_a_narrated_article_shows_how_long_it_runs(
    client: TestClient, context: AppContext
):
    _add_entry(
        context, title="Narrated", status=EntryStatus.READY, duration_seconds=1500
    )

    meta = _meta_of(client.get(ARTICLES_PAGE).text, "Narrated")

    assert "25 min" in meta


def test_a_very_short_narration_is_still_a_minute(
    client: TestClient, context: AppContext
):
    """Rounding to the nearest minute would report a 20-second clip as 0 min."""
    _add_entry(context, title="Brief", status=EntryStatus.READY, duration_seconds=20)

    assert "1 min" in _meta_of(client.get(ARTICLES_PAGE).text, "Brief")


def test_an_article_being_narrated_shows_its_progress(
    client: TestClient, context: AppContext
):
    _add_entry(
        context,
        title="Halfway",
        status=EntryStatus.PROCESSING,
        progress=(5, 10),
    )

    meta = _meta_of(client.get(ARTICLES_PAGE).text, "Halfway")

    assert "50%" in meta
    assert 'aria-valuenow="50"' in meta
    assert " min" not in meta


def test_an_article_claimed_but_not_yet_started_says_only_that(
    client: TestClient, context: AppContext
):
    """progress_percent is None until the first chunk lands and for single-chunk
    articles, and a bar reading 0% would be worse than saying it is running."""
    _add_entry(context, title="Just claimed", status=EntryStatus.PROCESSING)

    meta = _meta_of(client.get(ARTICLES_PAGE).text, "Just claimed")

    assert "narrating" in meta
    assert "%" not in meta
    assert " min" not in meta


@pytest.mark.parametrize("status", [EntryStatus.PENDING, EntryStatus.FAILED])
def test_an_article_with_no_audio_shows_no_length_at_all(
    client: TestClient, context: AppContext, status: EntryStatus
):
    _add_entry(context, title="Nothing yet", status=status)

    meta = _meta_of(client.get(ARTICLES_PAGE).text, "Nothing yet")

    assert " min" not in meta
    assert "%" not in meta
    assert "narrating" not in meta


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


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_search_opens_without_javascript(
    client: TestClient, context: AppContext, path: str
):
    """A native <details>, so the magnifying glass works with the script off."""
    _add_entry(context, title="An article")

    body = client.get(path).text

    assert '<details class="searchpop"' in body
    assert "<summary data-searchopen" in body
    assert '<form method="get"' in body


@pytest.mark.parametrize("path", [SOURCES_PAGE, ARTICLES_PAGE])
def test_opening_search_focuses_the_field_inside_the_tap(
    client: TestClient, context: AppContext, path: str
):
    """iOS raises the keyboard only for a focus() that runs synchronously in a
    real gesture handler. The details' own toggle event is dispatched later, so
    focusing there left the keyboard down."""
    _add_entry(context, title="An article")

    body = client.get(path).text
    script = body.split("<script>", 1)[1].split("</script>", 1)[0]

    assert "addEventListener('click'" in script
    assert "addEventListener('toggle'" not in script
    assert "event.preventDefault();" in script
    assert "pop.open = true;" in script
    assert "field.focus();" in script
    assert "field.select();" in script


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


def test_the_read_pip_stays_a_button_a_thumb_can_find(
    client: TestClient, context: AppContext
):
    """Swiping is the fast path for read, not the only one."""
    _add_entry(context, title="Saved and read", read=True, queued=True)

    read_button = _tag(
        client.get(ARTICLES_PAGE, params={"filter": "all"}).text, 'data-action="read"'
    )

    assert read_button.startswith("<button")
    assert 'aria-pressed="true"' in read_button
    assert "Mark as unread: Saved and read" in read_button


def test_the_star_is_an_indicator_that_no_tap_reaches(
    client: TestClient, context: AppContext
):
    """Starring is swipe-only by request, so the visible star must not invite
    a tap that would do nothing."""
    _add_entry(context, title="Saved", queued=True)

    body = client.get(ARTICLES_PAGE).text
    star = _tag(body, 'class="star"')

    assert star.startswith("<span")
    assert 'aria-hidden="true"' in star
    assert '<button class="star"' not in body


def test_starring_stays_reachable_off_screen_for_assistive_technology(
    client: TestClient, context: AppContext
):
    """Untappable is not the same as unreachable: the action survives as an
    off-screen button a keyboard or screen reader can still get to."""
    _add_entry(context, title="Saved", queued=True)

    star_button = _tag(client.get(ARTICLES_PAGE).text, 'data-action="star"')

    assert star_button.startswith("<button")
    assert "sr" in star_button
    assert 'aria-pressed="true"' in star_button
    assert "Remove from Listen Later: Saved" in star_button


def test_the_row_swipe_cedes_the_screen_edges_to_the_browser(
    client: TestClient, context: AppContext
):
    """Safari's back swipe starts at the edge and cannot be cancelled once it
    has begun, so the row gesture has to decline to start there."""
    _add_entry(context, title="An article")

    body = client.get(ARTICLES_PAGE).text

    assert "startedAt <= EDGE_GUARD" in body
    assert "startedAt >= width - EDGE_GUARD" in body
    assert "back gesture" in body  # said on screen, not only in the source


def test_a_swipe_keeps_the_direction_it_started_in(
    client: TestClient, context: AppContext
):
    """Dragging back should cancel the swipe, not arm the opposite action."""
    _add_entry(context, title="An article")

    body = client.get(ARTICLES_PAGE).text

    assert "gesture.action = dx > 0 ? 'read' : 'star';" in body
    assert "gesture.action === 'read' ? Math.max(0, dx) : Math.min(0, dx)" in body
    assert "run(it.row, it.action);" in body


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
