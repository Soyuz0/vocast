"""Converting samples for the encoder."""

import numpy as np
import pytest

from vocast.audio import _to_pcm16

# --- pcm conversion ---------------------------------------------------------


@pytest.mark.parametrize("size", [1, 999, 1 << 20, (1 << 20) + 1, 3_000_000])
def test_block_conversion_matches_converting_all_at_once(size: int):
    """The blockwise spelling exists to save memory, not to change the audio, so
    the bytes must be exactly what the whole-array arithmetic produced."""
    rng = np.random.default_rng(7)
    samples = (rng.standard_normal(size) * 1.4).astype(np.float32)

    whole = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    assert _to_pcm16(samples) == whole


def test_samples_beyond_the_range_are_clipped_not_wrapped():
    """Clipping in blocks must still clip: a sample above 1.0 that wrapped would
    be a loud click in the episode."""
    samples = np.array([2.0, -2.0, 0.0], dtype=np.float32)

    pcm = np.frombuffer(_to_pcm16(samples), dtype=np.int16)

    assert list(pcm) == [32767, -32767, 0]


def test_a_block_boundary_does_not_disturb_the_samples_around_it():
    block = 1 << 20
    rng = np.random.default_rng(3)
    samples = (rng.standard_normal(block * 2 + 5) * 0.9).astype(np.float32)

    pcm = np.frombuffer(_to_pcm16(samples), dtype=np.int16)

    assert len(pcm) == samples.size
    assert pcm[block - 1] == np.int16(np.clip(samples[block - 1], -1, 1) * 32767)
    assert pcm[block] == np.int16(np.clip(samples[block], -1, 1) * 32767)
