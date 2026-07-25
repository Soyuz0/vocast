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

    def __init__(self) -> None:
        self.synthesized: list[str] = []

    def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
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
        return title, text, cover

    monkeypatch.setattr(generator_module, "fetch_article", fake_fetch_article)


def _stub_extraction_raising(monkeypatch: pytest.MonkeyPatch, exc: Exception):
    def fake_fetch_article(url, *, html_fetcher=None):
        raise exc

    monkeypatch.setattr(generator_module, "fetch_article", fake_fetch_article)


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


@pytest.mark.parametrize("status", [404, 401, 403, 410, 451])
def test_client_error_statuses_are_permanent(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, status: int
):
    _stub_extraction_raising(
        monkeypatch, FetchError(f"HTTP {status} Nope from https://example.com/a")
    )
    with pytest.raises(PermanentGenerationError):
        VocastEpisodeGenerator(engine=engine).generate_from_url("https://example.com/a")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
def test_server_and_rate_limit_statuses_are_transient(
    lib: Path, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, status: int
):
    _stub_extraction_raising(
        monkeypatch, FetchError(f"HTTP {status} Nope from https://example.com/a")
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
        return "T", ARTICLE_TEXT, None

    monkeypatch.setattr(generator_module, "fetch_article", fake_fetch_article)
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
