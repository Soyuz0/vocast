"""Synthesis checkpointed to disk, one chunk at a time.

A long article is a hundred-plus chunks and hours of narration, and the process
narrating it does not always live that long. Holding every chunk in memory until
the end makes each such death total -- 99% done and 0% done are the same
outcome -- and costs a gigabyte of resident memory for a three-hour article,
which is itself a reason to be killed.

So each chunk is written out as soon as it exists, and the finished audio is
assembled by streaming those files back. Raw samples via `numpy.save`, not an
encoded format: the join has to reproduce exactly what holding everything in
memory would have produced, and any lossy or resampling round trip would change
the audio.

Resuming is only ever allowed when the staged chunks provably belong to the same
narration, which a fingerprint over the chunk texts, voices and engine decides.
Splicing two different articles together is much worse than synthesizing again,
so anything unrecognized is deleted rather than reused.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio import concat_with_silence, join_with_silence
from .engines import AudioChunk

log = logging.getLogger("vocast.staging")

MANIFEST_NAME = "manifest.json"

_CHUNK_PREFIX = "chunk-"
_CHUNK_SUFFIX = ".npy"
_UNFINISHED_SUFFIX = ".part"

#: Bumped whenever the layout on disk or the inputs to the fingerprint change,
#: so chunks written by an older version are discarded rather than misread.
_FORMAT_VERSION = "1"

#: How long a staging directory may go untouched before it is treated as
#: abandoned. Generous on purpose: an article can take a whole working day of
#: CPU across several restarts, and every resumed chunk refreshes the mtime, so
#: only a narration that is making no progress at all ages out.
DEFAULT_MAX_STAGING_AGE = timedelta(hours=72)


class StagingUnavailableError(RuntimeError):
    """The staging directory cannot be used, so synthesis must not start.

    Raised instead of quietly carrying on in memory: staging is what makes a
    long article survive a restart, and losing it silently would leave the
    service narrating for hours with nothing to show for an interruption.
    """


def engine_fingerprint(engine: object) -> str:
    """How an engine identifies itself within a narration fingerprint.

    The class is the engine's identity -- `kokoro` and `kokoro-onnx` are
    different classes -- and the sample rate and chunk limit are included
    because a change in either changes the samples or how the text is split.
    """
    cls = type(engine)
    rate = getattr(engine, "sample_rate", None)
    limit = getattr(engine, "max_chars", None)
    return f"{cls.__module__}.{cls.__qualname__}|{rate}|{limit}"


def narration_fingerprint(
    chunks: Sequence[tuple[str, str | None]], *, engine_id: str, gap_ms: int
) -> str:
    """Identify a narration by everything that decides how it will sound.

    The chunk texts subsume both the article and how it was split, and each
    chunk's voice covers the narrator and the quote voice, so a change to any of
    them yields a different fingerprint and the staged audio is thrown away.
    Lengths are hashed alongside the text so that no two different plans can
    serialize to the same bytes.
    """
    digest = sha256()
    digest.update(
        f"v{_FORMAT_VERSION}\0{engine_id}\0gap={gap_ms}\0chunks={len(chunks)}\0".encode()
    )
    for text, voice in chunks:
        body = text.encode("utf-8")
        digest.update(f"{voice or ''}\0{len(body)}\0".encode())
        digest.update(body)
        digest.update(b"\0")
    return digest.hexdigest()


class ChunkSink(Protocol):
    """Where finished chunks go while an article is being narrated."""

    @property
    def completed(self) -> int:
        """How many chunks, counted from the first, are already done."""

    def store(self, index: int, chunk: AudioChunk) -> None: ...

    def assemble(self, *, gap_ms: int) -> AudioChunk: ...


class MemoryChunks:
    """Keeps every chunk in memory until the end. No resume, peak is the article."""

    def __init__(self) -> None:
        self._chunks: list[AudioChunk] = []

    @property
    def completed(self) -> int:
        return len(self._chunks)

    def store(self, index: int, chunk: AudioChunk) -> None:
        self._chunks.append(chunk)

    def assemble(self, *, gap_ms: int) -> AudioChunk:
        return concat_with_silence(self._chunks, gap_ms=gap_ms)


class StagedChunks:
    """Chunks written to a directory as they are produced.

    The files themselves are the record of progress: `completed` is however many
    consecutive chunks are readable from the first, so nothing needs to be kept
    consistent between a counter and the disk. Each write goes to a temporary
    name and is renamed into place, so a process killed mid-write leaves a
    partial file that is never mistaken for a chunk.
    """

    def __init__(self, directory: Path, *, fingerprint: str) -> None:
        self._directory = Path(directory)
        self._fingerprint = fingerprint
        self._completed = 0
        self._sample_rate: int | None = None

    @classmethod
    def open(cls, directory: Path | str, *, fingerprint: str) -> StagedChunks:
        """Adopt chunks left by an earlier run, or start the directory afresh."""
        staged = cls(Path(directory), fingerprint=fingerprint)
        staged._prepare()
        return staged

    @property
    def completed(self) -> int:
        return self._completed

    def store(self, index: int, chunk: AudioChunk) -> None:
        if index != self._completed:
            raise ValueError(
                f"staged chunks must be written in order: expected index "
                f"{self._completed}, got {index}"
            )
        if self._sample_rate is None:
            self._sample_rate = chunk.sample_rate
            self._write_manifest()
        elif chunk.sample_rate != self._sample_rate:
            # The same message concat_with_silence would have raised, because
            # this is the same defect: audio that cannot be joined coherently.
            raise ValueError("sample rates differ across chunks")

        path = self._chunk_path(index)
        unfinished = path.with_name(path.name + _UNFINISHED_SUFFIX)
        try:
            with open(unfinished, "wb") as handle:
                np.save(handle, chunk.samples, allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(unfinished, path)
        except OSError as exc:
            raise StagingUnavailableError(
                f"cannot write the staged chunk {path}: {exc}"
            ) from exc
        self._completed = index + 1

    def assemble(self, *, gap_ms: int) -> AudioChunk:
        """Join the staged chunks into the finished audio.

        The chunks are handed over one at a time rather than as a list, so the
        only complete copy of the article that ever exists in memory is the
        result itself.
        """
        if not self._completed or self._sample_rate is None:
            raise ValueError("no audio chunks to concatenate")
        parts = _StagedParts(
            [self._chunk_path(index) for index in range(self._completed)]
        )
        return join_with_silence(parts, sample_rate=self._sample_rate, gap_ms=gap_ms)

    # -- internals ---------------------------------------------------------

    def _prepare(self) -> None:
        self._make_directory()
        manifest = self._read_manifest()
        if manifest is None or manifest.get("fingerprint") != self._fingerprint:
            if manifest is not None or self._chunk_path(0).exists():
                log.warning(
                    "discarding staged chunks in %s: they were produced from a "
                    "different narration",
                    self._directory,
                )
            self._reset()
            return

        rate = manifest.get("sample_rate")
        self._sample_rate = int(rate) if isinstance(rate, int) else None
        self._completed = self._readable_prefix()
        if self._completed and self._sample_rate is None:
            # The manifest records the rate before the first chunk is written, so
            # chunks without one mean a manifest we did not write. Nothing can be
            # concluded about the audio either, so none of it is reused.
            log.warning(
                "discarding staged chunks in %s: the manifest does not say what "
                "sample rate they were produced at",
                self._directory,
            )
            self._reset()
            return
        self._drop_from(self._completed)
        if self._completed:
            log.info(
                "resuming synthesis from %d staged chunk(s) in %s",
                self._completed,
                self._directory,
            )

    def _make_directory(self) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StagingUnavailableError(
                f"cannot create the synthesis staging directory "
                f"{self._directory}: {exc}"
            ) from exc

    def _reset(self) -> None:
        discard_staged_chunks(self._directory)
        self._completed = 0
        self._sample_rate = None
        self._make_directory()
        self._write_manifest()

    def _readable_prefix(self) -> int:
        """How many chunks are present and readable, counting from the first.

        Counting stops at the first gap, so the chunks in hand are always a
        prefix of the narration: a truncated or missing file makes everything
        after it worthless anyway, since audio must be joined in order.
        """
        count = 0
        while _holds_samples(self._chunk_path(count)):
            count += 1
        return count

    def _drop_from(self, index: int) -> None:
        """Remove unfinished writes and anything beyond the usable prefix."""
        for path in self._directory.iterdir():
            if path.name.endswith(_UNFINISHED_SUFFIX):
                _remove(path)
                continue
            position = _chunk_index(path)
            if position is not None and position >= index:
                _remove(path)

    def _chunk_path(self, index: int) -> Path:
        return self._directory / f"{_CHUNK_PREFIX}{index:05d}{_CHUNK_SUFFIX}"

    @property
    def _manifest_path(self) -> Path:
        return self._directory / MANIFEST_NAME

    def _read_manifest(self) -> dict[str, object] | None:
        try:
            loaded = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def _write_manifest(self) -> None:
        payload = {
            "fingerprint": self._fingerprint,
            "sample_rate": self._sample_rate,
            "version": _FORMAT_VERSION,
        }
        unfinished = self._manifest_path.with_name(MANIFEST_NAME + _UNFINISHED_SUFFIX)
        try:
            unfinished.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(unfinished, self._manifest_path)
        except OSError as exc:
            raise StagingUnavailableError(
                f"cannot write {self._manifest_path}: {exc}. Synthesis would "
                "have nowhere to checkpoint, so it is not started."
            ) from exc


class _StagedParts(Sequence[np.ndarray]):
    """The staged chunks as a sequence, each mapped only while it is being used.

    Deliberately not a list of arrays. Memory-mapping every chunk at once and
    then reading them all leaves every page resident, which costs as much as
    having held the chunks in memory in the first place. Mapping one chunk per
    lookup and letting it go keeps the peak at one chunk plus the result.
    """

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = list(paths)

    def __len__(self) -> int:
        return len(self._paths)

    def __getitem__(self, index: int) -> np.ndarray:  # type: ignore[override]
        return np.load(self._paths[index], mmap_mode="r", allow_pickle=False)


def discard_staged_chunks(directory: Path | str) -> None:
    """Delete a staging directory, tolerating its absence."""
    path = Path(directory)
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("could not remove the staging directory %s: %s", path, exc)


def sweep_stale(
    root: Path | str,
    *,
    max_age: timedelta = DEFAULT_MAX_STAGING_AGE,
    now: datetime | None = None,
) -> list[Path]:
    """Delete staging directories that have gone untouched for max_age.

    A killed process leaves its chunks behind deliberately -- that is the whole
    point -- but nothing else ever removes them, because the run that would have
    cleaned up is the one that died. Without a sweep they accumulate a gigabyte
    at a time. Staleness is judged on the most recently written chunk, so a
    narration still making progress is never swept however long it has run.
    """
    directory = Path(root)
    if not directory.is_dir():
        return []
    cutoff = (now or datetime.now(timezone.utc)) - max_age
    swept: list[Path] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        touched = _last_touched(child)
        if touched is None or touched >= cutoff:
            continue
        freed = _directory_size(child)
        discard_staged_chunks(child)
        if child.exists():
            continue
        swept.append(child)
        log.warning(
            "removed abandoned synthesis staging %s (%d KiB, untouched since %s)",
            child,
            freed // 1024,
            touched.isoformat(timespec="seconds"),
        )
    return swept


def _holds_samples(path: Path) -> bool:
    """Whether path is a complete `.npy` file of one-dimensional samples.

    Memory-mapping reads the header and checks the file is long enough for the
    array it claims, which is exactly the truncation a killed process leaves
    behind, and costs no read of the samples themselves.
    """
    try:
        samples = np.load(path, mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError, EOFError):
        return False
    return bool(getattr(samples, "ndim", 0) == 1)


def _chunk_index(path: Path) -> int | None:
    if not path.name.startswith(_CHUNK_PREFIX) or path.suffix != _CHUNK_SUFFIX:
        return None
    digits = path.name[len(_CHUNK_PREFIX) : -len(_CHUNK_SUFFIX)]
    return int(digits) if digits.isdigit() else None


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        log.warning("could not remove the staged file %s: %s", path, exc)


def _last_touched(directory: Path) -> datetime | None:
    newest: float | None = None
    try:
        candidates = [directory, *directory.iterdir()]
    except OSError:
        return None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=timezone.utc)


def _directory_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total
