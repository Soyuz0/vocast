from collections.abc import Callable

from .audio import concat_with_silence
from .chunking import chunk_text
from .engines import AudioChunk, TTSEngine


class SynthesisCancelled(Exception):
    """Synthesis was asked to stop before it finished.

    No audio is returned, so nothing partial is ever written. Raised only
    between chunks, never mid-chunk.
    """


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
