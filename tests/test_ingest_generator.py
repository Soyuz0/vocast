"""The ingestion -> pipeline seam.

Verifies what we hand to vocast's pipeline and how we classify its failures.
The pipeline itself (trafilatura, Kokoro, ffmpeg) is mocked at the boundary.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vocast import library
from vocast.engines import AudioChunk
from vocast.ingest import generator as generator_module
from vocast.ingest.generator import (
    MIN_ARTICLE_CHARS,
    GenerationCancelled,
    PermanentGenerationError,
    TransientGenerationError,
    VocastEpisodeGenerator,
)
from vocast.ingest.nethttp import BlockedURLError, FetchError

ARTICLE_TEXT = "Sentence one. " * 100


class FakeEngine:
    sample_rate = 24000
    max_chars = 1800
    default_voice = "af_heart"

    def __init__(self, *, break_after: int | None = None) -> None:
        self.synthesized: list[str] = []
        #: Stop mid-article after this many chunks, the way a dying engine or a
        #: killed process does.
        self._break_after = break_after

    def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
        if self._break_after is not None and len(self.synthesized) >= self._break_after:
            raise RuntimeError("engine died")
        self.synthesized.append(text)
        return AudioChunk(np.zeros(2400, dtype=np.float32), self.sample_rate)


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(library, "LIBRARY_PATH", tmp_path / "library")
    return tmp_path / "library"


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


def _stub_extraction(
    monkeypatch: pytest.MonkeyPatch,
    *,
    title: str | None = "Extracted Title",
    text: str = ARTICLE_TEXT,
    cover: str | None = None,
):
    def fake_fetch_article(url, *, html_fetcher=None):
        return title, text, cover, []

    monkeypatch.setattr(generator_module, "fetch_article_parts", fake_fetch_article)


def _stub_extraction_raising(monkeypatch: pytest.MonkeyPatch, exc: Exception):
    def fake_fetch_article(url, *, html_fetcher=None):
        raise exc

    monkeypatch.setattr(generator_module, "fetch_article_parts", fake_fetch_article)


# --- happy path ------------------------------------------------------------


def test_generates_an_episode_in_the_library(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine)

    episode = gen.generate_from_url("https://example.com/a")

    assert episode.episode_id
    assert Path(episode.audio_path).exists()
    assert episode.duration_seconds == pytest.approx(0.1)
    assert (lib / episode.episode_id / "meta.json").exists()


def test_supplied_title_overrides_the_extracted_one(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine)

    episode = gen.generate_from_url("https://example.com/a", title="Feed Title")

    assert episode.title == "Feed Title"


def test_extracted_title_is_used_when_the_feed_gave_none(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, title="Extracted Title")
    gen = VocastEpisodeGenerator(engine=engine)

    assert gen.generate_from_url("https://example.com/a").title == "Extracted Title"


def test_article_text_reaches_the_tts_engine(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, text="Hello there. " * 60)
    VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")

    assert engine.synthesized
    assert "Hello there." in engine.synthesized[0]


def test_configured_voice_is_recorded_on_the_episode(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine, voice="af_bella")

    episode = gen.generate_from_url("https://example.com/a")

    stored = library.get_entry(episode.episode_id)
    assert stored.voice == "af_bella"


def test_source_url_is_recorded_on_the_episode(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine)

    episode = gen.generate_from_url("https://example.com/original")

    assert (
        library.get_entry(episode.episode_id).source == "https://example.com/original"
    )


def test_content_hash_is_stable_for_identical_text(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine)

    first = gen.generate_from_url("https://example.com/a")
    second = gen.generate_from_url("https://example.com/a")

    assert first.content_hash == second.content_hash
    assert first.episode_id != second.episode_id


# --- thin content ----------------------------------------------------------


def test_too_little_text_fails_instead_of_making_an_empty_episode(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, text="Subscribe to read more.")
    gen = VocastEpisodeGenerator(engine=engine)

    with pytest.raises(PermanentGenerationError, match="below the"):
        gen.generate_from_url("https://example.com/paywalled")

    assert engine.synthesized == []


def test_text_at_the_minimum_length_is_accepted(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, text="x" * MIN_ARTICLE_CHARS)
    gen = VocastEpisodeGenerator(engine=engine)

    assert gen.generate_from_url("https://example.com/a").episode_id


def test_nothing_is_written_to_the_library_when_extraction_is_too_thin(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, text="too short")
    gen = VocastEpisodeGenerator(engine=engine)

    with pytest.raises(PermanentGenerationError):
        gen.generate_from_url("https://example.com/a")

    assert library.list_entries() == []


# --- failure classification ------------------------------------------------


def test_blocked_url_is_permanent(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction_raising(monkeypatch, BlockedURLError("private address"))
    with pytest.raises(PermanentGenerationError):
        VocastEpisodeGenerator(engine=engine).generate_from_url("http://10.0.0.1/")


def test_extraction_failure_is_permanent(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction_raising(monkeypatch, ValueError("could not extract content"))
    with pytest.raises(PermanentGenerationError, match="could not extract"):
        VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")


@pytest.mark.parametrize("status", [404, 401, 410, 451])
def test_client_error_statuses_are_permanent(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, status: int
):
    _stub_extraction_raising(
        monkeypatch, FetchError(f"HTTP {status} Nope from https://example.com/a")
    )
    with pytest.raises(PermanentGenerationError):
        VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504, 408])
def test_server_and_rate_limit_statuses_are_transient(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, status: int
):
    _stub_extraction_raising(
        monkeypatch, FetchError(f"HTTP {status} Nope from https://example.com/a")
    )
    with pytest.raises(TransientGenerationError):
        VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")


def test_forbidden_is_retried_rather_than_discarded(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """Bot-protection edges answer 403 under load instead of 429, and serve the
    same URL happily later, so treating it as final discards a fetchable article."""
    _stub_extraction_raising(
        monkeypatch, FetchError("HTTP 403 Forbidden from https://example.com/a")
    )
    with pytest.raises(TransientGenerationError):
        VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")


def test_network_error_is_transient(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction_raising(
        monkeypatch, FetchError("network error fetching https://example.com/a: refused")
    )
    with pytest.raises(TransientGenerationError):
        VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")


def test_tts_failure_is_transient(lib: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_extraction(monkeypatch)

    class BrokenEngine(FakeEngine):
        def synthesize(self, text, voice=None):
            raise RuntimeError("CUDA out of memory")

    gen = VocastEpisodeGenerator(engine=BrokenEngine())
    with pytest.raises(TransientGenerationError, match="synthesis failed"):
        gen.generate_from_url("https://example.com/a")


def test_engine_load_failure_is_transient(lib: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_extraction(monkeypatch)
    monkeypatch.setattr(
        generator_module,
        "get_engine",
        lambda name: (_ for _ in ()).throw(OSError("no model weights")),
    )
    gen = VocastEpisodeGenerator(engine_name="kokoro")

    with pytest.raises(TransientGenerationError, match="could not load TTS engine"):
        gen.generate_from_url("https://example.com/a")


# --- engine reuse ----------------------------------------------------------


def test_engine_is_loaded_once_and_reused(lib: Path, monkeypatch: pytest.MonkeyPatch):
    """Loading Kokoro costs seconds, so it must not happen per episode."""
    _stub_extraction(monkeypatch)
    loads: list[str] = []

    def fake_get_engine(name: str):
        loads.append(name)
        return FakeEngine()

    monkeypatch.setattr(generator_module, "get_engine", fake_get_engine)
    gen = VocastEpisodeGenerator(engine_name="kokoro")

    gen.generate_from_url("https://example.com/a")
    gen.generate_from_url("https://example.com/b")

    assert loads == ["kokoro"]


def test_guarded_fetcher_is_used_for_article_html(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """Article HTML must go through the policy-enforcing fetch layer."""
    captured: dict[str, object] = {}

    def fake_fetch_article(url, *, html_fetcher=None):
        captured["fetcher"] = html_fetcher
        return "T", ARTICLE_TEXT, None, []

    monkeypatch.setattr(generator_module, "fetch_article_parts", fake_fetch_article)
    seen: list[str] = []
    monkeypatch.setattr(
        generator_module,
        "fetch",
        lambda url, policy=None: _FakeResponse(seen, url),
    )

    VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")

    assert captured["fetcher"] is not None
    captured["fetcher"]("https://example.com/a")
    assert seen == ["https://example.com/a"]


class _FakeResponse:
    def __init__(self, seen: list[str], url: str) -> None:
        seen.append(url)

    def text(self) -> str:
        return "<html></html>"


# --- article text ----------------------------------------------------------


def test_narrated_text_is_stored_beside_the_audio(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """Feeds use it as show notes, so it has to outlive generation."""
    _stub_extraction(monkeypatch, text=ARTICLE_TEXT)
    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://example.com/a"
    )

    stored = library.get_entry(episode.episode_id)
    # What is stored is what was narrated, intro included.
    assert stored.article_text().startswith("Extracted Title.")
    assert "Sentence one." in stored.article_text()


def test_article_text_is_absent_for_older_episodes(lib: Path):
    """Episodes generated before this existed must still render."""
    from vocast.engines import AudioChunk

    entry = library.add_entry(
        title="No Text",
        chunk=AudioChunk(np.zeros(2400, dtype=np.float32), 24000),
        voice="af_heart",
        engine="kokoro",
    )
    assert library.get_entry(entry.id).article_text() is None


# --- narration intro -------------------------------------------------------


def test_narration_starts_with_title_then_byline_then_article():
    from vocast.ingest.generator import build_narration

    spoken = build_narration("The Bitter Lesson", "LessWrong", "Body starts here.")
    assert spoken == "The Bitter Lesson.\n\nby LessWrong.\n\nBody starts here."


def test_duplicate_headline_is_not_read_twice():
    """Extractors often repeat the headline as the first line of the body."""
    from vocast.ingest.generator import build_narration

    spoken = build_narration(
        "Solve for the equilibrium", "MR", "Solve for the equilibrium\nThen the body."
    )
    assert spoken.count("Solve for the equilibrium") == 1
    assert spoken == "Solve for the equilibrium.\n\nby MR.\n\nThen the body."


def test_headline_match_ignores_punctuation_and_case():
    from vocast.ingest.generator import build_narration

    spoken = build_narration("Duane Arnold", "LW", "duane arnold!\nBody.")
    assert spoken == "Duane Arnold.\n\nby LW.\n\nBody."


def test_byline_is_omitted_when_the_publication_is_unknown():
    from vocast.ingest.generator import build_narration

    assert build_narration("A Title", None, "Body.") == "A Title.\n\nBody."


def test_existing_title_punctuation_is_not_doubled():
    from vocast.ingest.generator import build_narration

    assert build_narration("Really?", "Pub", "Body.").startswith("Really?\n\nby Pub.")


def test_generator_narrates_the_intro_and_records_it(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, title="Extracted", text=ARTICLE_TEXT)
    gen = VocastEpisodeGenerator(engine=engine)

    episode = gen.generate_from_url(
        "https://example.com/a", title="Feed Title", byline="Marginal Revolution"
    )

    assert engine.synthesized[0].startswith("Feed Title.")
    assert "by Marginal Revolution." in engine.synthesized[0]
    stored = library.get_entry(episode.episode_id).article_text()
    assert stored.startswith("Feed Title.\n\nby Marginal Revolution.")


# --- cancellation ----------------------------------------------------------


def test_synthesis_stops_between_chunks_when_asked(
    lib: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unbounded article can take hours; pausing must not wait it out."""
    from vocast.ingest.generator import GenerationCancelled

    _stub_extraction(monkeypatch, text="Sentence one. " * 400)
    engine = FakeEngine()
    calls = {"n": 0}

    def keep_going() -> bool:
        calls["n"] += 1
        return calls["n"] <= 1  # allow one chunk, then cancel

    gen = VocastEpisodeGenerator(engine=engine, should_continue=keep_going)
    with pytest.raises(GenerationCancelled):
        gen.generate_from_url("https://example.com/a")

    assert library.list_entries() == [], "no partial episode may be written"


def test_uncancelled_generation_is_unaffected(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine, should_continue=lambda: True)
    assert gen.generate_from_url("https://example.com/a").episode_id


# --- cover art -------------------------------------------------------------


def test_publication_artwork_wins_over_the_articles_own_image(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """Keeps every episode from one source looking consistent."""
    _stub_extraction(monkeypatch, cover="https://example.com/article-image.jpg")
    gen = VocastEpisodeGenerator(engine=engine)

    episode = gen.generate_from_url(
        "https://example.com/a", cover_url="http://freshrss/f.php?h=logo"
    )

    assert library.get_entry(episode.episode_id).cover_url == (
        "http://freshrss/f.php?h=logo"
    )


def test_article_image_is_used_when_the_publication_has_none(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, cover="https://example.com/article-image.jpg")
    gen = VocastEpisodeGenerator(engine=engine)

    episode = gen.generate_from_url("https://example.com/a", cover_url=None)

    assert library.get_entry(episode.episode_id).cover_url == (
        "https://example.com/article-image.jpg"
    )


# --- in-place regeneration -------------------------------------------------


def test_regeneration_keeps_the_episode_id(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """The id is the podcast GUID; changing it makes clients report the old
    episode as withdrawn by the publisher."""
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine)
    first = gen.generate_from_url("https://example.com/a")

    second = gen.generate_from_url(
        "https://example.com/a", replace_episode_id=first.episode_id
    )

    assert second.episode_id == first.episode_id
    assert len(library.list_entries()) == 1


def test_regeneration_rewrites_the_audio(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine)
    first = gen.generate_from_url("https://example.com/a", title="Old")

    gen.generate_from_url(
        "https://example.com/a", title="New", replace_episode_id=first.episode_id
    )

    stored = library.get_entry(first.episode_id)
    assert stored.title == "New"
    assert Path(stored.audio_path()).exists()


def test_failed_regeneration_leaves_the_previous_episode_intact(
    lib: Path, monkeypatch: pytest.MonkeyPatch
):
    """Otherwise a transient failure would blank a published episode."""
    _stub_extraction(monkeypatch)
    good = VocastEpisodeGenerator(engine=FakeEngine())
    first = good.generate_from_url("https://example.com/a", title="Original")
    original_bytes = Path(first.audio_path).read_bytes()

    class BrokenEngine(FakeEngine):
        def synthesize(self, text, voice=None):
            raise RuntimeError("engine died")

    broken = VocastEpisodeGenerator(engine=BrokenEngine())
    with pytest.raises(TransientGenerationError):
        broken.generate_from_url(
            "https://example.com/a", replace_episode_id=first.episode_id
        )

    stored = library.get_entry(first.episode_id)
    assert stored.title == "Original"
    assert Path(stored.audio_path()).read_bytes() == original_bytes


def test_replacing_a_missing_episode_falls_back_to_creating_one(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch)
    gen = VocastEpisodeGenerator(engine=engine)

    episode = gen.generate_from_url(
        "https://example.com/a", replace_episode_id="20200101T000000Z_gone_abc123"
    )

    assert episode.episode_id != "20200101T000000Z_gone_abc123"
    assert library.get_entry(episode.episode_id) is not None


def test_replacement_refuses_an_unsafe_id(lib: Path, engine: FakeEngine):
    with pytest.raises(ValueError, match="unsafe entry id"):
        library.replace_entry(
            "../../escape",
            title="T",
            chunk=AudioChunk(np.zeros(240, dtype=np.float32), 24000),
            voice="v",
            engine="e",
        )


# --- narrating a post body -------------------------------------------------


def test_post_body_is_narrated_without_fetching_the_link(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """The whole point: a link post must not fetch what it links to."""

    def explode(url, **kwargs):
        raise AssertionError("must not fetch the outbound link")

    monkeypatch.setattr(generator_module, "fetch_article_parts", explode)
    body = "<p>" + ("The author's own commentary here. " * 20) + "</p>"

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://elsewhere.example/x",
        title="A Link Post",
        content_html=body,
        prefer_content_html=True,
    )

    assert episode.episode_id
    assert "own commentary" in library.get_entry(episode.episode_id).article_text()


def test_post_body_keeps_the_title_and_byline_intro(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    body = "<p>" + ("Commentary text. " * 40) + "</p>"
    VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://elsewhere.example/x",
        title="A Link Post",
        byline="Daring Fireball",
        content_html=body,
    )
    assert engine.synthesized[0].startswith("A Link Post.")
    assert "by Daring Fireball." in engine.synthesized[0]


def test_too_thin_a_post_body_fails_clearly(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    with pytest.raises(PermanentGenerationError, match="below the"):
        VocastEpisodeGenerator(engine=engine).generate_from_url(
            "https://elsewhere.example/x", content_html="<p>Too short.</p>"
        )


# --- quote voice -----------------------------------------------------------


QUOTED_HTML = (
    "<p>Someone wrote, at their blog:</p>"
    "<blockquote><p>" + ("The quoted argument runs on for a while. " * 8) + "</p>"
    "</blockquote>"
    "<p>" + ("And here is the commentary that follows it. " * 8) + "</p>"
)


class VoiceRecordingEngine(FakeEngine):
    """Records which voice each chunk was synthesized with, and what was said."""

    def __init__(self) -> None:
        super().__init__()
        self.voices: list[str | None] = []
        self.spoken: list[str] = []

    def synthesize(self, text, voice=None):
        self.voices.append(voice)
        self.spoken.append(text)
        return super().synthesize(text, voice=voice)


def test_a_block_quote_is_read_in_the_quote_voice(lib: Path):
    engine = VoiceRecordingEngine()

    VocastEpisodeGenerator(
        engine=engine, voice="af_heart", quote_voice="am_michael"
    ).generate_from_url("https://example.com/a", content_html=QUOTED_HTML)

    assert "am_michael" in engine.voices
    assert "af_heart" in engine.voices


def test_the_heading_stays_in_the_narrator_voice(lib: Path):
    """A link blog often quotes in its own headline; the title and publication
    are the narrator speaking, whatever the article does."""
    engine = VoiceRecordingEngine()

    VocastEpisodeGenerator(
        engine=engine, voice="af_heart", quote_voice="am_michael"
    ).generate_from_url(
        "https://example.com/a", title="'A Quoted Headline'", content_html=QUOTED_HTML
    )

    assert engine.voices[0] == "af_heart"


def test_without_a_quote_voice_everything_uses_one_voice(lib: Path):
    engine = VoiceRecordingEngine()

    VocastEpisodeGenerator(engine=engine, voice="af_heart").generate_from_url(
        "https://example.com/a", content_html=QUOTED_HTML
    )

    assert set(engine.voices) == {"af_heart"}


def test_the_narrated_text_is_unchanged_by_quote_splitting(lib: Path):
    """Quotes may only affect who reads a passage, never what is read."""
    plain = VocastEpisodeGenerator(
        engine=FakeEngine(), voice="af_heart"
    ).generate_from_url("https://example.com/a", content_html=QUOTED_HTML)
    split = VocastEpisodeGenerator(
        engine=FakeEngine(), voice="af_heart", quote_voice="am_michael"
    ).generate_from_url("https://example.com/b", content_html=QUOTED_HTML)

    assert _article_text(split) == _article_text(plain)


def _article_text(episode) -> str:
    return (Path(episode.audio_path).parent / "article.txt").read_text(encoding="utf-8")


DUPLICATE_HEADLINE_HTML = (
    "<p>Liberal Worlds: James Bryce and the Democratic Intellect</p>"
    "<p>" + ("Bryce wrote at length on the subject. " * 6) + "</p>"
    "<blockquote><p>" + ("The democratic intellect is a curious thing. " * 6) + "</p>"
    "</blockquote>"
    "<p>" + ("That is the argument in brief. " * 6) + "</p>"
)


def test_an_article_repeating_its_headline_is_not_narrated_twice(lib: Path):
    """Regression: the body was recovered by searching for it inside the finished
    narration, but the heading dedup had already altered it, so the search failed
    and the whole article was appended a second time -- once in the narrator's
    voice, once with the quote voice."""
    engine = VoiceRecordingEngine()

    VocastEpisodeGenerator(
        engine=engine, voice="af_heart", quote_voice="am_michael"
    ).generate_from_url(
        "https://example.com/a",
        title="Liberal Worlds: James Bryce and the Democratic Intellect",
        content_html=DUPLICATE_HEADLINE_HTML,
    )

    spoken = " ".join(engine.spoken)
    assert spoken.count("That is the argument in brief.") == 6, "body read once"
    assert spoken.count("The democratic intellect is a curious thing.") == 6


def test_the_headline_is_still_dropped_when_the_body_repeats_it(lib: Path):
    """The dedup itself must keep working: the title is spoken once, from the
    heading, not again from the body."""
    engine = VoiceRecordingEngine()

    VocastEpisodeGenerator(
        engine=engine, voice="af_heart", quote_voice="am_michael"
    ).generate_from_url(
        "https://example.com/a",
        title="Liberal Worlds: James Bryce and the Democratic Intellect",
        content_html=DUPLICATE_HEADLINE_HTML,
    )

    spoken = " ".join(engine.spoken)
    assert spoken.count("Liberal Worlds: James Bryce") == 1


def test_quote_voicing_survives_the_headline_dedup(lib: Path):
    """The earlier failure mode silently fell back to one voice for these
    articles; the quote must still be voiced separately."""
    engine = VoiceRecordingEngine()

    VocastEpisodeGenerator(
        engine=engine, voice="af_heart", quote_voice="am_michael"
    ).generate_from_url(
        "https://example.com/a",
        title="Liberal Worlds: James Bryce and the Democratic Intellect",
        content_html=DUPLICATE_HEADLINE_HTML,
    )

    assert "am_michael" in engine.voices


# --- falling back to the feed's copy ----------------------------------------


FEED_BODY = "<p>" + ("The newsletter's own text, in full. " * 20) + "</p>"


def test_a_failed_fetch_narrates_the_body_the_feed_carried(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """A bridge expiring its permalink says nothing about whether the article is
    worth hearing, and the feed already handed us the text."""
    _stub_extraction_raising(monkeypatch, FetchError("HTTP 404 Not Found from x"))

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://kill-the-newsletter.com/entries/gone.html", content_html=FEED_BODY
    )

    assert (
        "newsletter's own text" in library.get_entry(episode.episode_id).article_text()
    )


def test_a_paywalled_page_falls_back_to_the_feed_copy(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """The same applies to a permanent failure: a stub page is not a reason to
    discard a full article we already hold."""
    _stub_extraction(monkeypatch, text="Subscribe to continue reading.")

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://example.com/paywalled", content_html=FEED_BODY
    )

    assert (
        "newsletter's own text" in library.get_entry(episode.episode_id).article_text()
    )


def test_the_page_is_still_preferred_when_it_can_be_fetched(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """A feed body is often an excerpt while the page is the whole piece, so the
    fallback must not become the default."""
    _stub_extraction(monkeypatch, text=ARTICLE_TEXT)

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://example.com/a", content_html=FEED_BODY
    )

    text = library.get_entry(episode.episode_id).article_text()
    assert "newsletter's own text" not in text


def test_a_failed_fetch_with_no_feed_copy_still_fails(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """Nothing to fall back on, so the failure stands and is reported as before."""
    _stub_extraction_raising(monkeypatch, FetchError("HTTP 404 Not Found from x"))

    with pytest.raises(PermanentGenerationError):
        VocastEpisodeGenerator(engine=engine).generate_from_url(
            "https://example.com/gone"
        )


def test_cancellation_is_not_mistaken_for_a_failed_fetch(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """A paused worker must stop, not quietly narrate the feed copy instead."""
    _stub_extraction_raising(monkeypatch, GenerationCancelled("worker paused"))

    with pytest.raises(GenerationCancelled):
        VocastEpisodeGenerator(engine=engine).generate_from_url(
            "https://example.com/a", content_html=FEED_BODY
        )


# --- pages that render in the browser ---------------------------------------


JS_SHELL = (
    "x\nThis website requires javascript to properly function. Consider "
    "activating javascript to get access to all site functionality.\n"
    "LESSWRONG\nLW\nLogin\nA Post Title \u2014 LessWrong\nAI\nFrontpage\n98\n"
)


def test_a_javascript_notice_is_not_an_article(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """Regression: the notice comes with the nav and title, so it clears the
    length minimum and was narrated as though it were the post."""
    _stub_extraction(monkeypatch, text=JS_SHELL)

    with pytest.raises(PermanentGenerationError, match="JavaScript"):
        VocastEpisodeGenerator(engine=engine).generate_from_url(
            "https://www.lesswrong.com/posts/x/y"
        )


def test_a_javascript_notice_falls_back_to_the_feed_copy(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    _stub_extraction(monkeypatch, text=JS_SHELL)

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://www.lesswrong.com/posts/x/y", content_html=FEED_BODY
    )

    assert "newsletter's own text" in library.get_entry(
        episode.episode_id
    ).article_text()


def test_an_article_discussing_javascript_is_still_narrated(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """The phrase alone proves nothing; it has to open a suspiciously short text."""
    about_js = "Sites that require javascript are the subject here. " * 60
    _stub_extraction(monkeypatch, text=about_js)

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://example.com/about-js"
    )

    assert "subject here" in library.get_entry(episode.episode_id).article_text()


def test_the_feed_wins_when_the_page_yields_far_less(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """A page can return its chrome and nothing else while still looking like a
    success, which no failure path would catch."""
    _stub_extraction(monkeypatch, text="Cookie notice. Accept all. Manage choices.")

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://example.com/a", content_html=FEED_BODY
    )

    assert "newsletter's own text" in library.get_entry(
        episode.episode_id
    ).article_text()


def test_a_full_page_is_not_displaced_by_a_similar_feed_body(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
):
    """The page is normally the better source, so the comparison must not be a
    close-run thing."""
    _stub_extraction(monkeypatch, text=ARTICLE_TEXT)

    episode = VocastEpisodeGenerator(engine=engine).generate_from_url(
        "https://example.com/a", content_html="<p>" + ARTICLE_TEXT + "</p>"
    )

    assert "Sentence one." in library.get_entry(episode.episode_id).article_text()


# --- checkpointing partial narration ---------------------------------------


LONG_ARTICLE = "Sentence one. " * 400


def test_finished_episode_leaves_no_staged_chunks_behind(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _stub_extraction(monkeypatch)
    staging = tmp_path / "staging"
    gen = VocastEpisodeGenerator(engine=engine, staging_root=staging)

    gen.generate_from_url("https://example.com/a", resume_key="entry-7")

    assert not (staging / "entry-7").exists()


def test_a_cancelled_narration_keeps_its_chunks_for_the_next_attempt(
    lib: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Cancellation is a pause or a shutdown, and the entry is requeued."""
    _stub_extraction(monkeypatch, text=LONG_ARTICLE)
    staging = tmp_path / "staging"
    allowed = {"chunks": 2}

    def keep_going() -> bool:
        allowed["chunks"] -= 1
        return allowed["chunks"] > 0

    stopped = VocastEpisodeGenerator(
        engine=FakeEngine(), staging_root=staging, should_continue=keep_going
    )
    with pytest.raises(GenerationCancelled):
        stopped.generate_from_url("https://example.com/a", resume_key="entry-7")
    staged = sorted((staging / "entry-7").glob("chunk-*.npy"))

    resuming = FakeEngine()
    VocastEpisodeGenerator(engine=resuming, staging_root=staging).generate_from_url(
        "https://example.com/a", resume_key="entry-7"
    )

    assert len(staged) == 1, "the chunk narrated before the pause must survive it"
    assert len(resuming.synthesized) == _chunk_count(LONG_ARTICLE) - 1


def test_a_broken_narration_resumes_where_it_stopped(
    lib: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _stub_extraction(monkeypatch, text=LONG_ARTICLE)
    staging = tmp_path / "staging"
    with pytest.raises(TransientGenerationError):
        VocastEpisodeGenerator(
            engine=FakeEngine(break_after=2), staging_root=staging
        ).generate_from_url("https://example.com/a", resume_key="entry-7")

    resuming = FakeEngine()
    episode = VocastEpisodeGenerator(
        engine=resuming, staging_root=staging
    ).generate_from_url("https://example.com/a", resume_key="entry-7")

    assert len(resuming.synthesized) == _chunk_count(LONG_ARTICLE) - 2
    uninterrupted = VocastEpisodeGenerator(engine=FakeEngine()).generate_from_url(
        "https://example.com/a"
    )
    assert episode.duration_seconds == uninterrupted.duration_seconds, (
        "an article narrated across two runs must be as long as one narrated in one"
    )


def test_a_permanent_failure_discards_chunks_from_an_earlier_attempt(
    lib: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    staging = tmp_path / "staging"
    _stub_extraction(monkeypatch, text=LONG_ARTICLE)
    with pytest.raises(TransientGenerationError):
        VocastEpisodeGenerator(
            engine=FakeEngine(break_after=1), staging_root=staging
        ).generate_from_url("https://example.com/a", resume_key="entry-7")
    assert (staging / "entry-7").exists()

    _stub_extraction_raising(monkeypatch, BlockedURLError("blocked forever"))
    with pytest.raises(PermanentGenerationError):
        VocastEpisodeGenerator(engine=FakeEngine(), staging_root=staging).generate_from_url(
            "https://example.com/a", resume_key="entry-7"
        )

    assert not (staging / "entry-7").exists()


def test_nothing_is_staged_without_a_resume_key(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A one-off has nothing to resume into, so it stages nothing."""
    _stub_extraction(monkeypatch)
    staging = tmp_path / "staging"

    VocastEpisodeGenerator(engine=engine, staging_root=staging).generate_from_url(
        "https://example.com/a"
    )

    assert not staging.exists()


def test_a_resume_key_cannot_escape_the_staging_root(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The staging directory is deleted wholesale once the episode exists, so
    where a key points has to be ours to decide, not the caller's."""
    _stub_extraction(monkeypatch)
    staging = tmp_path / "staging"
    outside = tmp_path / "keep-me"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not delete", encoding="utf-8")

    episode = VocastEpisodeGenerator(
        engine=engine, staging_root=staging
    ).generate_from_url("https://example.com/a", resume_key="../keep-me")

    assert episode.episode_id
    assert (outside / "precious.txt").exists()
    assert list(staging.iterdir()) == [], "the staged chunks were inside the root"


def _chunk_count(article_text: str) -> int:
    """How many chunks the narration of this article splits into."""
    from vocast.chunking import chunk_text

    narration = generator_module.build_narration(
        "Extracted Title", None, article_text.strip()
    )
    return len(chunk_text(narration, FakeEngine.max_chars))
