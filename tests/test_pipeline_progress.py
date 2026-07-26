import numpy as np

from vocast.engines.engine import AudioChunk, TTSEngine
from vocast.pipeline import synthesize_article


class CountingEngine(TTSEngine):
    """Returns a fixed snippet of silence per call, with a tiny chunk limit
    so short test text still splits into several chunks."""

    max_chars = 40
    default_voice = "test"
    sample_rate = 24000

    def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
        return AudioChunk(samples=np.zeros(8, dtype=np.float32), sample_rate=24000)


def test_progress_is_reported_after_every_chunk():
    reported: list[tuple[int, int]] = []
    text = " ".join(f"Sentence number {n} carries on." for n in range(12))

    synthesize_article(
        text,
        CountingEngine(),
        progress=False,
        on_progress=lambda done, total: reported.append((done, total)),
    )

    total = reported[-1][1]
    assert total > 1
    assert reported == [(n, total) for n in range(1, total + 1)]


def test_synthesis_works_without_a_progress_callback():
    """on_progress is optional: the CLI has nowhere to report it."""
    result = synthesize_article(
        "A short line of narration.", CountingEngine(), progress=False
    )

    assert result.sample_rate == 24000
