import logging
import warnings
from collections.abc import Sequence
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import soundfile as sf

from .engines import AudioChunk

# pydub probes PATH for ffmpeg/avconv when it is imported and warns if neither is
# found. We supply ffmpeg via imageio-ffmpeg (set as the converter below), so on
# machines without a system ffmpeg (e.g. Windows) that probe is a false alarm —
# silence just that one warning during the import.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="Couldn't find ffmpeg or avconv", category=RuntimeWarning
    )
    from pydub import AudioSegment

# Use the ffmpeg binary bundled with imageio-ffmpeg so users don't need a
# system ffmpeg install. pydub shells out to this for MP3 encoding.
AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

log = logging.getLogger("vocast.audio")


def concat_with_silence(chunks: list[AudioChunk], gap_ms: int = 120) -> AudioChunk:
    if not chunks:
        raise ValueError("no audio chunks to concatenate")
    sr = chunks[0].sample_rate
    if any(c.sample_rate != sr for c in chunks):
        raise ValueError("sample rates differ across chunks")
    return join_with_silence([c.samples for c in chunks], sample_rate=sr, gap_ms=gap_ms)


def join_with_silence(
    parts: Sequence[np.ndarray], *, sample_rate: int, gap_ms: int
) -> AudioChunk:
    """Join runs of samples end to end, gap_ms of silence between each.

    Written into one pre-allocated array rather than assembled with
    np.concatenate, which holds the parts and the joined copy at the same time:
    for a three-hour article that second copy is another gigabyte. It also lets
    the parts be memory maps of staged chunk files (see vocast.staging), where
    materializing them all at once would defeat the point of staging them.

    The silence is the zeros the array is created with, and the promotion rule
    matches concatenating a float32 gap with the parts, so the result is
    bit-for-bit what np.concatenate produced.

    The sequence is walked twice, once for the layout and once to fill it, and
    each part is used and released before the next is asked for. A lazy sequence
    of files therefore never has more than one part in memory at a time.
    """
    if not parts:
        raise ValueError("no audio chunks to concatenate")
    gap_samples = int(sample_rate / 1000 * gap_ms)
    gaps = len(parts) - 1
    lengths: list[int] = []
    dtypes: list[np.dtype] = []
    for part in parts:
        lengths.append(len(part))
        dtypes.append(part.dtype)
    if gaps:
        dtypes.append(np.dtype(np.float32))
    joined = np.zeros(sum(lengths) + gaps * gap_samples, dtype=np.result_type(*dtypes))
    at = 0
    for index, length in enumerate(lengths):
        if index:
            at += gap_samples
        joined[at : at + length] = parts[index]
        at += length
    return AudioChunk(joined, sample_rate)


def write_audio(
    chunk: AudioChunk,
    path: Path,
    mp3_bitrate: str = "96k",
    cover_path: Path | None = None,
) -> None:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".wav":
        sf.write(path, chunk.samples, chunk.sample_rate)
        return
    if suffix == ".mp3":
        pcm = np.clip(chunk.samples, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        seg = AudioSegment(
            pcm.tobytes(),
            frame_rate=chunk.sample_rate,
            sample_width=2,
            channels=1,
        )
        # Embed cover art (ID3 APIC) when given a usable image. If embedding
        # fails for any reason, still produce the mp3 without art: missing
        # artwork is a far better outcome than no episode. The failure is
        # logged rather than swallowed, because silently dropping art from
        # every episode would otherwise look like the feature never existed.
        if cover_path is not None and Path(cover_path).exists():
            try:
                seg.export(
                    path, format="mp3", bitrate=mp3_bitrate, cover=str(cover_path)
                )
                return
            except Exception as exc:  # noqa: BLE001 - any failure falls back
                log.warning(
                    "could not embed cover art in %s, writing without it: %s: %s",
                    path,
                    type(exc).__name__,
                    exc,
                )
        seg.export(path, format="mp3", bitrate=mp3_bitrate)
        return
    raise ValueError(f"unsupported output format: {suffix!r} (use .mp3 or .wav)")
