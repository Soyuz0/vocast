from collections.abc import Callable, Sequence

from .audio import concat_with_silence
from .chunking import chunk_text
from .engines import AudioChunk, TTSEngine


class SynthesisCancelled(Exception):
    """Synthesis was asked to stop before it finished.

    No audio is returned, so nothing partial is ever written. Raised only
    between chunks, never mid-chunk.
    """


def synthesize_passages(
    passages: Sequence[tuple[str, str | None]],
    engine: TTSEngine,
    *,
    progress: bool = True,
    should_continue: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    gap_ms: int = 320,
) -> AudioChunk:
    """Synthesize (text, voice) passages and join them.

    Each passage is chunked separately so no chunk straddles a change of voice.
    The gap between passages is longer than the one between chunks of the same
    voice, because an abrupt switch reads as a glitch rather than as another
    speaker taking over.
    """
    prepared = [
        (chunk_text(text, engine.max_chars), voice)
        for text, voice in passages
        if text.strip()
    ]
    total = sum(len(chunks) for chunks, _ in prepared)
    if not total:
        raise ValueError("input text is empty")

    rendered: list[AudioChunk] = []
    done = 0
    for chunks, voice in prepared:
        for index, chunk in enumerate(chunks, 1):
            if should_continue is not None and not should_continue():
                raise SynthesisCancelled(f"stopped after {done} of {total} chunks")
            if progress:
                print(f"[{done + index}/{total}] synthesizing ({len(chunk)} chars)...")
            rendered.append(engine.synthesize(chunk, voice=voice))
            if on_progress is not None:
                on_progress(done + index, total)
        done += len(chunks)
    return concat_with_silence(rendered, gap_ms=gap_ms)


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
