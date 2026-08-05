"""Startup checks for the episode library directory.

A long-running service must not begin narrating if it cannot store the result.
The expensive part is synthesis, so failing before the first article beats
discovering the problem 17 minutes later, 10,000 times over.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import STORAGE_MARKER, StorageConfig
from .logs import get_logger, kv

log = get_logger("storage")


class StorageUnavailableError(RuntimeError):
    """The library directory is missing, unwritable, or not the expected one."""


def verify_storage(config: StorageConfig) -> None:
    """Check the library directory is present, writable, and really mounted.

    Raises StorageUnavailableError with a fix-it message. Under a container
    restart policy that turns a not-yet-ready network mount into a retry loop
    that heals itself once the mount appears.
    """
    path = config.library_path

    if config.require_marker:
        _require_marker(path)

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageUnavailableError(
            f"cannot create the episode library at {path}: {exc}"
        ) from exc

    _require_writable(path, "episode library")
    log.info("storage ready %s", kv(library_path=path))


def verify_staging(path: Path) -> None:
    """Check the synthesis staging directory is present and writable.

    Same reasoning as the library: partially narrated articles are checkpointed
    here so a restart resumes instead of starting over, and a staging area that
    cannot be written to would only be discovered chunks into an article. Better
    to refuse to start, which under a restart policy heals itself once the path
    is usable.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageUnavailableError(
            f"cannot create the synthesis staging directory at {path}: {exc}"
        ) from exc

    _require_writable(path, "synthesis staging directory")
    log.info("staging ready %s", kv(staging_path=path))


def _require_marker(path: Path) -> None:
    marker = path / STORAGE_MARKER
    if marker.exists():
        return
    raise StorageUnavailableError(
        f"{marker} is missing, so {path} is probably not mounted yet. "
        "Episodes would be written to an empty mountpoint and never served. "
        f"If this directory really is the right one, create the marker with: "
        f"touch {marker}  (or set storage.require_marker: false)"
    )


def _require_writable(path: Path, what: str) -> None:
    probe = path / f".vocast-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
    except OSError as exc:
        raise StorageUnavailableError(
            f"the {what} at {path} is not writable: {exc}. "
            "Check the mount, and that the process user owns it."
        ) from exc
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
