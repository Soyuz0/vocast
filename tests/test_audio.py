"""Converting samples for the encoder."""

import numpy as np
import pytest

from vocast.audio import _is_embeddable_image, _to_pcm16, write_audio
from vocast.engines.engine import AudioChunk

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


# --- cover art --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "magic", "embeddable"),
    [
        ("cover.jpg", b"\xff\xd8\xff\xe0", True),
        ("cover.png", b"\x89PNG\r\n\x1a\n", True),
        ("cover.ico", b"\x00\x00\x01\x00", False),
        ("cover.gif", b"GIF89a", False),
    ],
)
def test_only_formats_the_encoder_accepts_are_offered_to_it(
    tmp_path, name, magic, embeddable
):
    """An ICO fails the export only after the encoder has written its temporary
    files, so the episode loses its artwork and the files are left behind."""
    path = tmp_path / name
    path.write_bytes(magic + b"padding")

    assert _is_embeddable_image(path) is embeddable


def test_a_missing_or_empty_cover_is_not_offered(tmp_path):
    assert _is_embeddable_image(tmp_path / "absent.png") is False
    (tmp_path / "empty.png").write_bytes(b"")
    assert _is_embeddable_image(tmp_path / "empty.png") is False


def test_an_unembeddable_cover_still_produces_the_episode(tmp_path):
    """Losing the artwork is acceptable; losing the episode is not."""
    ico = tmp_path / "cover.ico"
    ico.write_bytes(b"\x00\x00\x01\x00" + b"0" * 64)
    out = tmp_path / "episode.mp3"

    write_audio(
        AudioChunk(np.zeros(2400, dtype=np.float32), 24000), out, cover_path=ico
    )

    assert out.exists() and out.stat().st_size > 0
