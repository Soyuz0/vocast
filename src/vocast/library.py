"""Library — synthesized articles stored as <id>/{audio.mp3, meta.json}."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.resources import as_file, files
from pathlib import Path

import imageio_ffmpeg

from .audio import write_audio
from .engines import AudioChunk

LIBRARY_PATH = Path.home() / ".vocast" / "library"


def set_library_path(path: Path | str) -> Path:
    """Point the library at a different directory and return it.

    Entries resolve their paths against the module-level LIBRARY_PATH at call
    time, so the long-running service can relocate storage once at startup
    (from config) instead of every caller threading a path around.
    """
    global LIBRARY_PATH
    LIBRARY_PATH = Path(path).expanduser()
    return LIBRARY_PATH


def library_path() -> Path:
    return LIBRARY_PATH


_COVER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# File signatures, to confirm a downloaded cover really is the image its
# content-type claims (and not, say, an HTML error page served as image/*).
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ICO_MAGIC = b"\x00\x00\x01\x00"


@dataclass
class LibraryEntry:
    id: str
    title: str
    source: str | None
    synthesized_at: str
    duration_seconds: float
    voice: str
    engine: str
    cover_url: str | None = None

    def dir(self) -> Path:
        return LIBRARY_PATH / self.id

    def audio_path(self) -> Path:
        return self.dir() / "audio.mp3"

    def meta_path(self) -> Path:
        return self.dir() / "meta.json"

    def article_path(self) -> Path:
        """The narrated text, stored so feeds can use it as show notes.

        Kept out of meta.json deliberately: rendering a feed reads every
        entry's metadata, and inlining article bodies there would make that
        scan orders of magnitude more expensive.
        """
        return self.dir() / "article.txt"

    def article_text(self) -> str | None:
        path = self.article_path()
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    @property
    def short_id(self) -> str:
        """Trailing hex token of the id — a stable, human-friendly handle."""
        return self.id.rsplit("_", 1)[-1]


def _make_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "untitled"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = secrets.token_hex(3)
    return f"{ts}_{slug}_{short}"


def _download_cover(url: str, dest_dir: Path) -> Path | None:
    """Download a JPEG/PNG cover into dest_dir; return its path, or None.

    Returns None on any failure (bad URL, non-image content, timeout) so a
    missing or unusable cover never blocks adding the article.
    """
    ext_by_type = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        # Feed icons are very often ICO. ffmpeg will not embed one as ID3
        # artwork, so these are converted below rather than stored as-is: an
        # ICO handed to the encoder fails the export, which cost every such
        # episode its artwork and leaked the encoder's temporary files.
        "image/vnd.microsoft.icon": ".ico",
        "image/x-icon": ".ico",
    }
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _COVER_USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ext = ext_by_type.get(resp.headers.get_content_type())
            if ext is None:
                return None
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not data.startswith((_JPEG_MAGIC, _PNG_MAGIC, _ICO_MAGIC)):
        return None
    if ext == ".ico":
        return _ico_as_png(data, dest_dir)
    dest = dest_dir / f"cover{ext}"
    dest.write_bytes(data)
    return dest


def _ico_as_png(data: bytes, dest_dir: Path) -> Path | None:
    """Rewrite an ICO as a PNG the encoder will accept, or give up on artwork.

    Returning None falls back to the bundled cover, which is a better outcome
    than handing the encoder something it will refuse: that failure is only
    discovered mid-export, after its temporary files have been written.
    """
    source = dest_dir / "cover.ico"
    dest = dest_dir / "cover.png"
    source.write_bytes(data)
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(source), str(dest)],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        source.unlink(missing_ok=True)
        return None
    source.unlink(missing_ok=True)
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return None
    return dest


@contextmanager
def _resolve_cover(downloaded: Path | None):
    """Yield a cover image path: the downloaded one, else the bundled default.

    Yields None only if the bundled default is somehow missing, so callers
    still produce audio without art rather than failing.
    """
    if downloaded is not None:
        yield downloaded
        return
    default = files("vocast").joinpath("assets/default_cover.jpg")
    if not default.is_file():
        yield None
        return
    with as_file(default) as path:
        yield path


def add_entry(
    *,
    title: str,
    chunk: AudioChunk,
    voice: str,
    engine: str,
    source: str | None = None,
    cover_url: str | None = None,
    mp3_bitrate: str = "96k",
    article_text: str | None = None,
) -> LibraryEntry:
    entry_id = _make_id(title)
    entry_dir = LIBRARY_PATH / entry_id
    entry_dir.mkdir(parents=True, exist_ok=False)

    downloaded_cover = _download_cover(cover_url, entry_dir) if cover_url else None
    audio_path = entry_dir / "audio.mp3"
    with _resolve_cover(downloaded_cover) as cover_path:
        write_audio(chunk, audio_path, mp3_bitrate=mp3_bitrate, cover_path=cover_path)

    duration = float(len(chunk.samples)) / chunk.sample_rate
    entry = LibraryEntry(
        id=entry_id,
        title=title,
        source=source,
        synthesized_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=duration,
        voice=voice,
        engine=engine,
        cover_url=cover_url,
    )
    (entry_dir / "meta.json").write_text(json.dumps(asdict(entry), indent=2))
    if article_text:
        (entry_dir / "article.txt").write_text(article_text, encoding="utf-8")
    return entry


def replace_entry(
    entry_id: str,
    *,
    title: str,
    chunk: AudioChunk,
    voice: str,
    engine: str,
    source: str | None = None,
    cover_url: str | None = None,
    mp3_bitrate: str = "96k",
    article_text: str | None = None,
) -> LibraryEntry:
    """Rewrite an existing entry's audio, keeping its id.

    The id is the podcast GUID, so re-narrating an article in place leaves
    subscribers' records intact. Minting a new id instead makes clients report
    the old episode as withdrawn by the publisher.

    New audio is written alongside the old and swapped in only once complete, so
    a failure part-way leaves the previous episode serving.
    """
    if not is_valid_entry_id(entry_id):
        raise ValueError(f"unsafe entry id: {entry_id!r}")
    entry_dir = LIBRARY_PATH / entry_id
    if not entry_dir.is_dir():
        raise FileNotFoundError(f"no library entry at {entry_dir}")

    staged = entry_dir / f".audio.new-{os.getpid()}.mp3"
    downloaded_cover = _download_cover(cover_url, entry_dir) if cover_url else None
    try:
        with _resolve_cover(downloaded_cover) as cover_path:
            write_audio(chunk, staged, mp3_bitrate=mp3_bitrate, cover_path=cover_path)
        os.replace(staged, entry_dir / "audio.mp3")
    finally:
        staged.unlink(missing_ok=True)

    duration = float(len(chunk.samples)) / chunk.sample_rate
    entry = LibraryEntry(
        id=entry_id,
        title=title,
        source=source,
        synthesized_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=duration,
        voice=voice,
        engine=engine,
        cover_url=cover_url,
    )
    (entry_dir / "meta.json").write_text(json.dumps(asdict(entry), indent=2))
    if article_text:
        (entry_dir / "article.txt").write_text(article_text, encoding="utf-8")
    return entry


def list_entries(limit: int | None = None) -> list[LibraryEntry]:
    """Newest first. `limit` stops early instead of reading the whole library.

    Entry ids begin with a UTC timestamp, so reverse directory order is already
    newest-first and the limit can be applied before any metadata is read --
    which matters when the library holds thousands of episodes on a network
    share.
    """
    if not LIBRARY_PATH.exists():
        return []
    entries: list[LibraryEntry] = []
    for child in sorted(LIBRARY_PATH.iterdir(), reverse=True):
        if limit is not None and len(entries) >= limit:
            break
        meta = child / "meta.json"
        if meta.exists():
            entries.append(LibraryEntry(**json.loads(meta.read_text())))
    return entries


_SAFE_ENTRY_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_entry_id(entry_id: str) -> bool:
    """Whether an id can only ever name a directory inside the library.

    Entry ids arrive from URL paths, so they are untrusted. A separator or a
    parent reference would let `LIBRARY_PATH / entry_id` escape the library --
    an absolute id escapes outright, since pathlib discards the left operand.
    """
    return bool(_SAFE_ENTRY_ID.match(entry_id)) and ".." not in entry_id


def get_entry(entry_id: str) -> LibraryEntry | None:
    if not is_valid_entry_id(entry_id):
        return None
    meta = LIBRARY_PATH / entry_id / "meta.json"
    if not meta.exists():
        return None
    return LibraryEntry(**json.loads(meta.read_text()))


def match_entries(entries: list[LibraryEntry], query: str) -> list[LibraryEntry]:
    """Resolve a query against entries.

    Priority: an exact id / short_id match (case-insensitive) wins outright;
    otherwise a case-insensitive substring match on the title.
    """
    q = query.strip()
    lowered = q.lower()
    exact = [e for e in entries if e.id == q or e.short_id.lower() == lowered]
    if exact:
        return exact
    return [e for e in entries if lowered in e.title.lower()]


def resolve(query: str) -> list[LibraryEntry]:
    """Scan the library and resolve a query to zero, one, or many entries."""
    return match_entries(list_entries(), query)


def delete_entry(entry: LibraryEntry) -> None:
    shutil.rmtree(entry.dir())
