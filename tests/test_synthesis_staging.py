"""Checkpointing synthesis to disk: identical audio, and a resume that is safe.

The engine is a stub throughout. What is under test is our bookkeeping -- what
gets written, what is reused, what is thrown away -- not numpy's ability to
round-trip an array.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from vocast.audio import concat_with_silence
from vocast.engines.engine import AudioChunk
from vocast.pipeline import SynthesisCancelled, synthesize_passages
from vocast.staging import (
    MANIFEST_NAME,
    StagedChunks,
    StagingUnavailableError,
    narration_fingerprint,
    sweep_stale,
)

GAP_MS = 320


class Killed(Exception):
    """Stands in for the process dying mid-article."""


class ToneEngine:
    """A chunk of audio per call, distinct per (text, voice) and never silent.

    Distinct because splicing the wrong chunk in has to be detectable, and never
    silent because a bug that produced zeros would otherwise pass. die_after
    stops it the way a kill would, part-way through an article.
    """

    max_chars = 40
    default_voice = "narrator"
    sample_rate = 24000

    def __init__(
        self, *, samples_per_chunk: int = 11, die_after: int | None = None
    ) -> None:
        self.samples_per_chunk = samples_per_chunk
        self.synthesized: list[tuple[str, str | None]] = []
        self._die_after = die_after

    def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
        if self._die_after is not None and len(self.synthesized) >= self._die_after:
            raise Killed(f"killed after {self._die_after} chunks")
        self.synthesized.append((text, voice))
        offset = sum(ord(ch) for ch in text) + len(voice or "")
        samples = np.linspace(
            0.001 * (offset % 997 + 1), 0.5, self.samples_per_chunk, dtype=np.float32
        )
        return AudioChunk(samples, self.sample_rate)


def _passages(sentences: int = 9, voice: str | None = "narrator"):
    text = " ".join(f"Sentence number {n} carries on." for n in range(sentences))
    return [(text, voice)]


def _narrate(passages, engine, *, staging_dir: Path | None = None, **kwargs):
    return synthesize_passages(
        passages,
        engine,
        progress=False,
        gap_ms=GAP_MS,
        staging_dir=staging_dir,
        **kwargs,
    )


# --- the audio must not change ---------------------------------------------


def test_the_join_reproduces_the_concatenation_it_replaced():
    """Pinned against the original implementation, which built the audio with
    np.concatenate over the chunks and a float32 gap between each."""
    rng = np.random.default_rng(20260805)
    chunks = [
        AudioChunk(rng.standard_normal(1000 + n).astype(np.float32), 24000)
        for n in range(5)
    ]
    gap = np.zeros(int(24000 / 1000 * GAP_MS), dtype=np.float32)
    parts: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        if index:
            parts.append(gap)
        parts.append(chunk.samples)
    expected = np.concatenate(parts)

    joined = concat_with_silence(chunks, gap_ms=GAP_MS)

    assert joined.samples.dtype == expected.dtype
    assert joined.samples.tobytes() == expected.tobytes()


def test_staged_audio_is_identical_to_holding_every_chunk_in_memory(tmp_path: Path):
    passages = _passages()

    in_memory = _narrate(passages, ToneEngine())
    staged = _narrate(passages, ToneEngine(), staging_dir=tmp_path / "stage")

    assert staged.sample_rate == in_memory.sample_rate
    assert staged.samples.dtype == in_memory.samples.dtype
    assert staged.samples.tobytes() == in_memory.samples.tobytes()


def test_staged_audio_is_identical_across_a_restart(tmp_path: Path):
    """The join must not care where the chunks were produced or when."""
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"

    uninterrupted = _narrate(passages, ToneEngine())
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=3), staging_dir=staging_dir)
    resumed = _narrate(passages, ToneEngine(), staging_dir=staging_dir)

    assert resumed.samples.tobytes() == uninterrupted.samples.tobytes()


def test_the_gap_between_chunks_is_still_uniform(tmp_path: Path):
    """320 ms everywhere, including between chunks of the same voice."""
    engine = ToneEngine(samples_per_chunk=5)
    passages = [("First sentence here.", "narrator"), ("Second one there.", "quoter")]

    joined = _narrate(passages, engine, staging_dir=tmp_path / "stage")

    gap = int(engine.sample_rate / 1000 * GAP_MS)
    expected = concat_with_silence(
        [engine.synthesize(text, voice=voice) for text, voice in passages],
        gap_ms=GAP_MS,
    )
    assert len(joined.samples) == 2 * 5 + gap
    assert joined.samples.tobytes() == expected.samples.tobytes()


def test_each_chunk_reaches_disk_before_the_next_is_narrated(tmp_path: Path):
    """Why staging lowers peak memory: only the chunk in hand need be in memory."""
    staging_dir = tmp_path / "stage"
    on_disk_when_called: list[int] = []

    class WatchingEngine(ToneEngine):
        def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
            on_disk_when_called.append(_staged_chunk_files(staging_dir))
            return super().synthesize(text, voice)

    _narrate(_passages(), WatchingEngine(), staging_dir=staging_dir)

    assert on_disk_when_called == list(range(len(on_disk_when_called)))


# --- resuming ---------------------------------------------------------------


def test_a_resumed_run_only_synthesizes_what_is_missing(tmp_path: Path):
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"
    dying = ToneEngine(die_after=4)
    with pytest.raises(Killed):
        _narrate(passages, dying, staging_dir=staging_dir)

    resuming = ToneEngine()
    _narrate(passages, resuming, staging_dir=staging_dir)

    assert len(dying.synthesized) == 4
    assert resuming.synthesized[0] not in dying.synthesized
    assert len(dying.synthesized) + len(resuming.synthesized) == _chunk_count(
        passages, ToneEngine()
    )


def test_progress_resumes_from_the_chunks_on_disk(tmp_path: Path):
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=4), staging_dir=staging_dir)

    reported: list[tuple[int, int]] = []
    _narrate(
        passages,
        ToneEngine(),
        staging_dir=staging_dir,
        on_progress=lambda done, total: reported.append((done, total)),
    )

    total = reported[-1][1]
    assert reported[0] == (4, total), "a resumed run must not report starting over"
    assert [done for done, _ in reported] == list(range(4, total + 1))


def test_cancellation_keeps_the_staged_chunks(tmp_path: Path):
    """A pause or a shutdown is exactly when the work has to survive."""
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"
    engine = ToneEngine()
    synthesized = 0

    def keep_going() -> bool:
        nonlocal synthesized
        synthesized = len(engine.synthesized)
        return synthesized < 5

    with pytest.raises(SynthesisCancelled):
        _narrate(passages, engine, staging_dir=staging_dir, should_continue=keep_going)

    assert _staged_chunk_files(staging_dir) == 5
    resumed = _narrate(passages, ToneEngine(), staging_dir=staging_dir)
    assert (
        resumed.samples.tobytes() == _narrate(passages, ToneEngine()).samples.tobytes()
    )


def test_a_chunk_left_half_written_is_not_spliced_into_the_audio(tmp_path: Path):
    """What a kill mid-write leaves behind: the file exists but is truncated."""
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=4), staging_dir=staging_dir)
    truncated = max(staging_dir.glob("chunk-*.npy"))
    truncated.write_bytes(truncated.read_bytes()[:-8])

    engine = ToneEngine()
    resumed = _narrate(passages, engine, staging_dir=staging_dir)

    assert (
        resumed.samples.tobytes() == _narrate(passages, ToneEngine()).samples.tobytes()
    )
    assert not truncated.exists() or truncated.stat().st_size > 0


def test_a_chunk_with_the_wrong_dtype_is_not_reused(tmp_path: Path):
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=4), staging_dir=staging_dir)
    np.save(staging_dir / "chunk-00000.npy", np.ones(11, dtype=np.float64))

    engine = ToneEngine()
    resumed = _narrate(passages, engine, staging_dir=staging_dir)

    expected = _narrate(passages, ToneEngine())
    assert resumed.samples.tobytes() == expected.samples.tobytes()
    assert resumed.samples.dtype == expected.samples.dtype


def test_a_manifest_with_the_wrong_sample_rate_is_not_reused(tmp_path: Path):
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=4), staging_dir=staging_dir)
    manifest_path = staging_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sample_rate"] = 48000
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    engine = ToneEngine()
    resumed = _narrate(passages, engine, staging_dir=staging_dir)

    assert resumed.sample_rate == ToneEngine.sample_rate
    assert resumed.samples.tobytes() == _narrate(
        passages, ToneEngine()
    ).samples.tobytes()


def test_chunks_from_the_original_staging_manifest_remain_resumable(tmp_path: Path):
    passages = _passages(sentences=12)
    staging_dir = tmp_path / "stage"
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=4), staging_dir=staging_dir)
    manifest_path = staging_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("dtype")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    engine = ToneEngine()
    _narrate(passages, engine, staging_dir=staging_dir)

    assert len(engine.synthesized) == _chunk_count(passages, engine) - 4


def test_chunks_beyond_the_narration_are_not_spliced_in(tmp_path: Path):
    passages = _passages()
    staging_dir = tmp_path / "stage"
    expected = _narrate(passages, ToneEngine(), staging_dir=staging_dir)
    chunks = sorted(staging_dir.glob("chunk-*.npy"))
    shutil.copyfile(chunks[-1], staging_dir / f"chunk-{len(chunks):05d}.npy")

    resumed = _narrate(passages, ToneEngine(), staging_dir=staging_dir)

    assert resumed.samples.tobytes() == expected.samples.tobytes()


# --- refusing to reuse someone else's chunks -------------------------------


def test_a_changed_article_discards_the_staged_chunks(tmp_path: Path):
    _assert_starts_over(tmp_path, _passages(sentences=9), _passages(sentences=13))


def test_a_changed_voice_discards_the_staged_chunks(tmp_path: Path):
    _assert_starts_over(tmp_path, _passages(), _passages(voice="someone_else"))


def test_dropping_the_voice_discards_the_staged_chunks(tmp_path: Path):
    """Falling back to the engine's default is still a change of narrator."""
    _assert_starts_over(tmp_path, _passages(), [(_passages()[0][0], None)])


def test_a_changed_quote_voice_discards_the_staged_chunks(tmp_path: Path):
    """Only the quoted passage moves, and that is enough to invalidate all of it."""
    text = _passages()[0][0]
    _assert_starts_over(
        tmp_path,
        [(text, "narrator"), ("A quoted line.", "narrator")],
        [(text, "narrator"), ("A quoted line.", "quoter")],
    )


def test_a_changed_chunk_limit_discards_the_staged_chunks(tmp_path: Path):
    """Different chunking means different chunk boundaries, so different audio."""
    staging_dir = tmp_path / "stage"
    passages = _passages()
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=3), staging_dir=staging_dir)

    rechunking = ToneEngine()
    rechunking.max_chars = 90
    joined = _narrate(passages, rechunking, staging_dir=staging_dir)

    assert len(rechunking.synthesized) == _chunk_count(passages, rechunking)
    expected = ToneEngine()
    expected.max_chars = 90
    assert joined.samples.tobytes() == _narrate(passages, expected).samples.tobytes()


def test_chunks_from_a_different_engine_are_discarded(tmp_path: Path):
    class OtherEngine(ToneEngine):
        """A different engine class: kokoro and kokoro-onnx are two of these."""

    staging_dir = tmp_path / "stage"
    passages = _passages()
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=3), staging_dir=staging_dir)

    other = OtherEngine()
    _narrate(passages, other, staging_dir=staging_dir)

    assert len(other.synthesized) == _chunk_count(passages, other)


def test_a_staging_directory_without_its_manifest_is_not_trusted(tmp_path: Path):
    staging_dir = tmp_path / "stage"
    passages = _passages()
    with pytest.raises(Killed):
        _narrate(passages, ToneEngine(die_after=3), staging_dir=staging_dir)
    (staging_dir / MANIFEST_NAME).unlink()

    engine = ToneEngine()
    _narrate(passages, engine, staging_dir=staging_dir)

    assert len(engine.synthesized) == _chunk_count(passages, engine)


def test_the_fingerprint_covers_text_voice_engine_and_gap():
    plan = [("Some words.", "narrator")]
    baseline = narration_fingerprint(plan, engine_id="engine-a", gap_ms=320)

    assert narration_fingerprint(plan, engine_id="engine-a", gap_ms=320) == baseline
    assert narration_fingerprint(plan, engine_id="engine-b", gap_ms=320) != baseline
    assert narration_fingerprint(plan, engine_id="engine-a", gap_ms=120) != baseline
    assert (
        narration_fingerprint(
            [("Some words.", "quoter")], engine_id="engine-a", gap_ms=320
        )
        != baseline
    )
    assert (
        narration_fingerprint(
            [("Some", "words.narrator")], engine_id="engine-a", gap_ms=320
        )
        != baseline
    ), "a plan must not be able to serialize to the same bytes as another"


# --- failing loudly --------------------------------------------------------


def test_an_unwritable_staging_area_stops_synthesis(tmp_path: Path):
    """Narrating for hours with nowhere to checkpoint is worse than not starting."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("in the way", encoding="utf-8")
    engine = ToneEngine()

    with pytest.raises(StagingUnavailableError):
        _narrate(_passages(), engine, staging_dir=blocked / "stage")

    assert engine.synthesized == []


def test_disk_full_while_writing_a_chunk_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("vocast.staging.np.save", disk_full)

    with pytest.raises(StagingUnavailableError, match="No space left on device"):
        _narrate(_passages(), ToneEngine(), staging_dir=tmp_path / "stage")
    assert not list((tmp_path / "stage").glob("chunk-*.npy"))


def test_two_writers_cannot_open_the_same_staging_directory(tmp_path: Path):
    staging_dir = tmp_path / "stage"
    first = StagedChunks.open(staging_dir, fingerprint="abc")

    with pytest.raises(StagingUnavailableError, match="already in use"):
        StagedChunks.open(staging_dir, fingerprint="abc")
    assert first.completed == 0


def test_chunks_must_be_stored_in_order(tmp_path: Path):
    staged = StagedChunks.open(tmp_path / "stage", fingerprint="abc")

    with pytest.raises(ValueError, match="in order"):
        staged.store(1, AudioChunk(np.zeros(4, dtype=np.float32), 24000))


def test_a_chunk_at_another_sample_rate_is_refused(tmp_path: Path):
    staged = StagedChunks.open(tmp_path / "stage", fingerprint="abc")
    staged.store(0, AudioChunk(np.zeros(4, dtype=np.float32), 24000))

    with pytest.raises(ValueError, match="sample rates differ"):
        staged.store(1, AudioChunk(np.zeros(4, dtype=np.float32), 48000))


def test_assembling_nothing_is_an_error(tmp_path: Path):
    staged = StagedChunks.open(tmp_path / "stage", fingerprint="abc")

    with pytest.raises(ValueError, match="no audio chunks"):
        staged.assemble(gap_ms=GAP_MS)


# --- sweeping abandoned staging -------------------------------------------


def test_stale_staging_is_swept_and_recent_staging_is_left_alone(tmp_path: Path):
    root = tmp_path / "staging"
    fresh = _stage_one(root / "entry-1", age=timedelta(hours=1))
    stale = _stage_one(root / "entry-2", age=timedelta(hours=100))

    swept = sweep_stale(root, max_age=timedelta(hours=72))

    assert swept == [stale]
    assert not stale.exists()
    assert fresh.exists()


def test_sweeping_judges_age_on_the_newest_chunk(tmp_path: Path):
    """A narration still making progress must never be swept out from under it."""
    root = tmp_path / "staging"
    directory = _stage_one(root / "entry-1", age=timedelta(days=9))
    recent_chunk = directory / "chunk-00001.npy"
    np.save(recent_chunk, np.zeros(4, dtype=np.float32))

    assert sweep_stale(root, max_age=timedelta(hours=72)) == []
    assert recent_chunk.exists()


def test_sweeping_does_not_delete_staging_in_active_use(tmp_path: Path):
    root = tmp_path / "staging"
    directory = root / "entry-1"
    staged = StagedChunks.open(directory, fingerprint="abc")
    staged.store(0, AudioChunk(np.zeros(8, dtype=np.float32), 24000))
    long_ago = time.time() - timedelta(days=9).total_seconds()
    for path in [*directory.iterdir(), directory]:
        os.utime(path, (long_ago, long_ago))

    assert sweep_stale(root, max_age=timedelta(hours=72)) == []
    assert directory.exists()


def test_sweeping_a_missing_root_is_not_an_error(tmp_path: Path):
    assert sweep_stale(tmp_path / "never-created", max_age=timedelta(hours=1)) == []


# --- helpers ---------------------------------------------------------------


def _assert_starts_over(tmp_path: Path, staged_from, then) -> None:
    """Chunks staged from one narration must never reach another's audio."""
    staging_dir = tmp_path / "stage"
    with pytest.raises(Killed):
        _narrate(staged_from, ToneEngine(die_after=3), staging_dir=staging_dir)

    engine = ToneEngine()
    joined = _narrate(then, engine, staging_dir=staging_dir)

    assert len(engine.synthesized) == _chunk_count(then, engine)
    assert joined.samples.tobytes() == _narrate(then, ToneEngine()).samples.tobytes()


def _chunk_count(passages, engine) -> int:
    from vocast.chunking import chunk_text
    from vocast.text_normalization import normalize_for_speech

    return sum(
        len(chunk_text(normalize_for_speech(text), engine.max_chars))
        for text, _ in passages
    )


def _staged_chunk_files(directory: Path) -> int:
    return len(list(directory.glob("chunk-*.npy"))) if directory.exists() else 0


def _stage_one(directory: Path, *, age: timedelta) -> Path:
    import os
    import time

    staged = StagedChunks.open(directory, fingerprint="abc")
    staged.store(0, AudioChunk(np.zeros(8, dtype=np.float32), 24000))
    when = time.time() - age.total_seconds()
    for path in [*directory.iterdir(), directory]:
        os.utime(path, (when, when))
    return directory
