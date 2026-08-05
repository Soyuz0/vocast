from collections.abc import Callable, Sequence
from pathlib import Path

from .audio import concat_with_silence
from .chunking import chunk_text
from .engines import AudioChunk, TTSEngine
from .staging import (
    ChunkSink,
    MemoryChunks,
    StagedChunks,
    engine_fingerprint,
    narration_fingerprint,
)


class SynthesisCancelled(Exception):
    """Synthesis was asked to stop before it finished.

    No audio is returned, so nothing partial is ever published. Raised only
    between chunks, never mid-chunk. Chunks already staged on disk are kept: a
    cancellation is a pause or a shutdown, which is exactly when the work done
    so far has to survive.
    """


def synthesize_passages(
    passages: Sequence[tuple[str, str | None]],
    engine: TTSEngine,
    *,
    progress: bool = True,
    should_continue: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    gap_ms: int = 320,
    staging_dir: Path | None = None,
) -> AudioChunk:
    """Synthesize (text, voice) passages and join them.

    Each passage is chunked separately so no chunk straddles a change of voice.
    One gap length is used between every pair of chunks, whether or not the voice
    changes there: gap_ms is the longer gap a change of voice wants, since an
    abrupt switch reads as a glitch rather than as another speaker taking over,
    and it is applied throughout.

    With staging_dir set, each chunk is checkpointed there as it is produced and
    a later call with the same passages and engine carries on from what it finds,
    instead of narrating hours of audio again. Without it every chunk is held in
    memory until the end, which is fine for a short article and for one-shot use
    from the CLI.
    """
    plan = _chunk_plan(passages, engine)
    total = len(plan)
    if not total:
        raise ValueError("input text is empty")

    sink = _open_sink(plan, engine, gap_ms=gap_ms, staging_dir=staging_dir)
    done = sink.completed
    if done and on_progress is not None:
        # Report the resumed position before synthesizing anything, so a
        # progress bar picks up where the previous run left off rather than
        # appearing to start the article over.
        on_progress(done, total)

    for index in range(done, total):
        text, voice = plan[index]
        if should_continue is not None and not should_continue():
            raise SynthesisCancelled(f"stopped after {index} of {total} chunks")
        if progress:
            print(f"[{index + 1}/{total}] synthesizing ({len(text)} chars)...")
        sink.store(index, engine.synthesize(text, voice=voice))
        if on_progress is not None:
            on_progress(index + 1, total)
    return sink.assemble(gap_ms=gap_ms)


def _chunk_plan(
    passages: Sequence[tuple[str, str | None]], engine: TTSEngine
) -> list[tuple[str, str | None]]:
    """Every chunk to be synthesized, in order, with the voice to read it."""
    return [
        (chunk, voice)
        for text, voice in passages
        if text.strip()
        for chunk in chunk_text(text, engine.max_chars)
    ]


def _open_sink(
    plan: Sequence[tuple[str, str | None]],
    engine: TTSEngine,
    *,
    gap_ms: int,
    staging_dir: Path | None,
) -> ChunkSink:
    if staging_dir is None:
        return MemoryChunks()
    return StagedChunks.open(
        staging_dir,
        fingerprint=narration_fingerprint(
            plan, engine_id=engine_fingerprint(engine), gap_ms=gap_ms
        ),
    )


def synthesize_article(
    text: str,
    engine: TTSEngine,
    voice: str | None = None,
    progress: bool = True,
    should_continue: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> AudioChunk:
    """Synthesize text to one audio chunk.

    should_continue is consulted between chunks; returning False raises
    SynthesisCancelled. This is what makes a long article interruptible: an
    unbounded article can take hours, and without a check between chunks there
    would be no way to stop it short of killing the process.

    on_progress is called with (completed, total) chunks. Chunk counts are the
    only measure of progress available: the total is known before any audio is
    produced, and chunks are near-uniform in length, so the ratio tracks elapsed
    audio closely.
    """
    chunks = chunk_text(text, engine.max_chars)
    if not chunks:
        raise ValueError("input text is empty")

    audio_chunks = []
    for i, chunk in enumerate(chunks, 1):
        if should_continue is not None and not should_continue():
            raise SynthesisCancelled(f"stopped after {i - 1} of {len(chunks)} chunks")
        if progress:
            print(f"[{i}/{len(chunks)}] synthesizing ({len(chunk)} chars)...")
        audio_chunks.append(engine.synthesize(chunk, voice=voice))
        if on_progress is not None:
            on_progress(i, len(chunks))

    return concat_with_silence(audio_chunks)
