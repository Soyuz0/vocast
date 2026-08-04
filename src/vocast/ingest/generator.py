"""The seam between ingestion and vocast's article -> audio pipeline.

Everything downstream of this module deals in `GeneratedEpisode` values and
knows nothing about trafilatura, chunking, Kokoro, or mp3 encoding. Everything
upstream reuses the existing pipeline rather than reimplementing it, so the
manual `vocast add` path and the automated path always produce identical audio.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypedDict

import trafilatura

from .. import library
from ..engines import AudioChunk, TTSEngine, get_engine
from ..fetch import fetch_article_parts
from ..pipeline import SynthesisCancelled, synthesize_passages
from ..quotes import quotes_from_xml, split_quoted
from .logs import get_logger, kv
from .nethttp import BlockedURLError, FetchError, FetchPolicy, fetch

log = get_logger("generator")

#: Below this many characters the "article" is almost always a paywall notice,
#: a consent interstitial, or a navigation-only shell. Narrating it produces a
#: useless episode, so generation fails loudly instead.
MIN_ARTICLE_CHARS = 400


def narration_parts(title: str, byline: str | None, body: str) -> tuple[str, str]:
    """The spoken heading, and the body as it will actually be narrated.

    Extractors frequently repeat the headline as the article's first line, so a
    leading line matching the title is dropped rather than narrated twice. That
    makes the narrated body differ from the extracted one, which is why callers
    that need to work on the body must take it from here rather than reusing what
    they passed in.

    Sentence-terminating punctuation is added so the chunker treats the intro as
    its own sentences instead of running it into the first paragraph.
    """
    lines = body.lstrip().splitlines()
    if lines and _is_same_heading(lines[0], title):
        body = "\n".join(lines[1:]).lstrip()

    intro = [_as_sentence(title)]
    if byline:
        intro.append(_as_sentence(f"by {byline}"))
    return "\n\n".join(intro), body.strip()


#: Sites that render in the browser serve a notice like this instead of the
#: article. The text clears the length minimum easily -- it comes with the nav
#: and the title -- so length alone does not catch it.
_JAVASCRIPT_NOTICE = re.compile(
    r"requires javascript|enable javascript|javascript is (?:disabled|required)",
    re.IGNORECASE,
)


def rejects_without_javascript(text: str) -> bool:
    """Whether this is a site's no-JavaScript notice rather than an article.

    The phrase alone proves nothing: an article may well discuss JavaScript. It
    counts only when it opens the text and the text is far too short to be the
    article it is claiming to be, which is what a rendering shell looks like.
    """
    return bool(_JAVASCRIPT_NOTICE.search(text[:300])) and len(text) < 2000


#: How much more text the feed must hold before it is believed over the page. A
#: page is normally the better source, so this is not a close-run comparison: it
#: is for when the page plainly did not yield the article at all.
FEED_BODY_ADVANTAGE = 2.0


def build_narration(title: str, byline: str | None, body: str) -> str:
    """Compose what is actually read aloud: title, byline, then the article."""
    heading, spoken_body = narration_parts(title, byline, body)
    return f"{heading}\n\n{spoken_body}"


def _as_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped if stripped[-1] in ".!?:;," else f"{stripped}."


def _normalize_heading(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _is_same_heading(line: str, title: str) -> bool:
    normalized = _normalize_heading(line)
    return bool(normalized) and normalized == _normalize_heading(title)


class _EpisodeWrite(TypedDict):
    """Arguments shared by creating and replacing a library entry."""

    title: str
    chunk: AudioChunk
    voice: str
    engine: str
    source: str | None
    cover_url: str | None
    mp3_bitrate: str
    article_text: str | None


@dataclass(frozen=True)
class GeneratedEpisode:
    episode_id: str
    title: str
    audio_path: str
    duration_seconds: float | None
    #: Size on disk, captured here so the feed never has to stat the file.
    audio_bytes: int | None = None
    content_hash: str | None = None


class GenerationError(Exception):
    """Base class for a failure to turn a URL into an episode."""


class TransientGenerationError(GenerationError):
    """Worth retrying: a timeout, a network blip, a server-side 5xx."""


class PermanentGenerationError(GenerationError):
    """Retrying cannot help: a 404, a blocked URL, or unusable content."""


class GenerationCancelled(GenerationError):
    """Generation was stopped deliberately, e.g. by pausing narration.

    Not a failure: the article goes straight back on the queue with its retry
    count untouched.
    """


class EpisodeGenerator(Protocol):
    def generate_from_url(
        self,
        url: str,
        *,
        title: str | None = None,
        byline: str | None = None,
        cover_url: str | None = None,
        replace_episode_id: str | None = None,
        content_html: str | None = None,
        prefer_content_html: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> GeneratedEpisode: ...


class VocastEpisodeGenerator:
    """Drives the existing vocast pipeline for one article.

    The TTS engine is constructed lazily and then reused, because loading the
    Kokoro weights costs seconds and would otherwise be paid per episode.
    """

    def __init__(
        self,
        *,
        engine_name: str = "kokoro",
        voice: str | None = None,
        policy: FetchPolicy | None = None,
        engine: TTSEngine | None = None,
        min_chars: int = MIN_ARTICLE_CHARS,
        mp3_bitrate: str = "96k",
        should_continue: Callable[[], bool] | None = None,
        quote_voice: str | None = None,
    ) -> None:
        self._engine_name = engine_name
        self._voice = voice
        self._policy = policy or FetchPolicy()
        self._engine = engine
        self._min_chars = min_chars
        self._mp3_bitrate = mp3_bitrate
        self._should_continue = should_continue
        self._quote_voice = quote_voice

    def generate_from_url(
        self,
        url: str,
        *,
        title: str | None = None,
        byline: str | None = None,
        cover_url: str | None = None,
        replace_episode_id: str | None = None,
        content_html: str | None = None,
        prefer_content_html: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> GeneratedEpisode:
        if content_html and prefer_content_html:
            # The post's own text, supplied because its link points elsewhere.
            extracted_title, text, article_cover, quotes = self._from_html(
                content_html, url
            )
        else:
            extracted_title, text, article_cover, quotes = self._extract_or_fall_back(
                url, content_html
            )
        # A supplied cover is the publication's own artwork, which keeps every
        # episode from a source visually consistent; the article's own image is
        # only a fallback.
        artwork = cover_url or article_cover
        engine = self._resolve_engine()
        voice = self._voice or engine.default_voice

        spoken_title = title or extracted_title or "untitled"
        heading, spoken_body = narration_parts(spoken_title, byline, text)
        narration = f"{heading}\n\n{spoken_body}"
        passages = self._passages(heading, spoken_body, quotes, voice)

        try:
            chunk = synthesize_passages(
                passages,
                engine,
                progress=False,
                should_continue=self._should_continue,
                on_progress=on_progress,
            )
        except SynthesisCancelled as exc:
            raise GenerationCancelled(f"narration of {url} stopped: {exc}") from exc
        except ValueError as exc:
            # Raised for empty input, which the length check above should have
            # already caught; treat it as unusable content rather than retrying.
            raise PermanentGenerationError(f"synthesis rejected {url}: {exc}") from exc
        except Exception as exc:
            raise TransientGenerationError(
                f"synthesis failed for {url}: {type(exc).__name__}: {exc}"
            ) from exc

        # A TypedDict rather than a plain dict: the same arguments go to two
        # functions, and an untyped mapping would silently stop them being
        # checked against either signature.
        shared: _EpisodeWrite = {
            "title": spoken_title,
            "chunk": chunk,
            "voice": voice,
            "engine": self._engine_name,
            "source": url,
            "cover_url": artwork,
            "mp3_bitrate": self._mp3_bitrate,
            "article_text": narration,
        }
        # Re-narrating in place keeps the podcast GUID, so subscribers do not
        # see the old episode withdrawn and a new one appear.
        if replace_episode_id:
            try:
                entry = library.replace_entry(replace_episode_id, **shared)
            except (FileNotFoundError, ValueError) as exc:
                log.warning(
                    "cannot replace episode in place, creating a new one %s",
                    kv(episode_id=replace_episode_id, error=exc),
                )
                entry = library.add_entry(**shared)
        else:
            entry = library.add_entry(**shared)
        log.info(
            "episode generated %s",
            kv(
                episode_id=entry.id,
                url=url,
                title=entry.title,
                seconds=round(entry.duration_seconds, 1),
            ),
        )
        return GeneratedEpisode(
            episode_id=entry.id,
            title=entry.title,
            audio_path=str(entry.audio_path()),
            duration_seconds=entry.duration_seconds,
            audio_bytes=_size_of(entry.audio_path()),
            content_hash=_hash_text(narration),
        )

    # -- internals ---------------------------------------------------------

    def _passages(
        self, heading: str, body: str, quotes: list[str], voice: str
    ) -> list[tuple[str, str]]:
        """Assign a voice to each run of the narration.

        Takes the heading and body separately, already as narration_parts
        produced them. An earlier version recovered the body by searching for it
        inside the finished narration, which silently narrated the whole article
        twice whenever the heading dedup had altered it: the search failed, and
        the failure looked like "no heading" rather than "not found".

        Only the body is examined for quotes. The heading names the article and
        its publication and stays with the narrator even when the title is itself
        a quotation, which on a link blog it often is.
        """
        whole = f"{heading}\n\n{body}"
        if not self._quote_voice or not quotes:
            return [(whole, voice)]
        passages = [(heading, voice)] if heading.strip() else []
        passages.extend(
            (passage.text, self._quote_voice if passage.quoted else voice)
            for passage in split_quoted(body, quotes)
        )
        return passages or [(whole, voice)]

    def _extract_or_fall_back(
        self, url: str, content_html: str | None
    ) -> tuple[str | None, str, str | None, list[str]]:
        """Fetch the article, or narrate the feed's copy if fetching fails.

        A fetch fails for reasons that say nothing about whether the article is
        worth hearing: the page moved, a newsletter bridge expired its permalink,
        a paywall went up. When the feed already handed us a substantial body,
        losing the article to any of those is a choice, not a necessity.

        The fetch is still tried first, because a feed body is often an excerpt
        while the page is the whole piece. The fallback only runs when there is
        nothing better to be had.
        """
        try:
            fetched = self._extract(url)
        except (TransientGenerationError, PermanentGenerationError) as exc:
            # Deliberately not GenerationError: cancellation is also one of those,
            # and a paused worker must stop rather than quietly take a shortcut.
            if not content_html:
                raise
            log.info(
                "fetch failed, narrating the body the feed carried %s",
                kv(url=url, error=exc),
            )
            return self._from_html(content_html, url)

        if not content_html:
            return fetched
        # The page was readable, but a page that renders in the browser can
        # yield its chrome and nothing else while still looking like a success.
        # When the feed is holding several times more text, the page did not
        # give us the article.
        from_feed = self._from_html(content_html, url)
        if len(from_feed[1]) > len(fetched[1]) * FEED_BODY_ADVANTAGE:
            log.info(
                "page yielded far less than the feed, narrating the feed's copy %s",
                kv(url=url, page_chars=len(fetched[1]), feed_chars=len(from_feed[1])),
            )
            return from_feed
        return fetched

    def _extract(self, url: str) -> tuple[str | None, str, str | None, list[str]]:
        try:
            extracted_title, text, cover_url, quotes = fetch_article_parts(
                url, html_fetcher=self._fetch_html
            )
        except BlockedURLError as exc:
            raise PermanentGenerationError(str(exc)) from exc
        except FetchError as exc:
            raise _classify_fetch_error(exc) from exc
        except ValueError as exc:
            # trafilatura found nothing worth reading.
            raise PermanentGenerationError(f"could not extract {url}: {exc}") from exc

        cleaned = text.strip()
        if rejects_without_javascript(cleaned):
            raise PermanentGenerationError(
                f"{url} served a page asking for JavaScript rather than the "
                f"article; only {len(cleaned)} characters were readable"
            )
        if len(cleaned) < self._min_chars:
            raise PermanentGenerationError(
                f"extracted only {len(cleaned)} characters from {url}, below the "
                f"{self._min_chars} character minimum; the page is probably a "
                "paywall, consent screen, or navigation stub rather than an article"
            )
        return extracted_title, cleaned, cover_url, quotes

    def _from_html(
        self, content_html: str, url: str
    ) -> tuple[str | None, str, str | None, list[str]]:
        """Extract narratable text from a feed entry's own body.

        Run through the same extractor as a web page so the cleanup rules match:
        code blocks stripped, boilerplate dropped, paragraphs preserved.
        """
        document = f"<html><body>{content_html}</body></html>"
        try:
            extracted = trafilatura.extract(
                document,
                include_comments=False,
                include_tables=False,
                prune_xpath=["//pre"],
            )
            # A second pass, because the plain output drops the quote elements
            # and rebuilding the text from the structured one would change it.
            quotes = quotes_from_xml(
                trafilatura.extract(
                    document,
                    output_format="xml",
                    include_comments=False,
                    include_tables=False,
                    prune_xpath=["//pre"],
                )
            )
        except Exception as exc:
            raise PermanentGenerationError(
                f"could not read the post body for {url}: {type(exc).__name__}: {exc}"
            ) from exc

        cleaned = (extracted or "").strip()
        if len(cleaned) < self._min_chars:
            raise PermanentGenerationError(
                f"the post body for {url} yielded only {len(cleaned)} characters, "
                f"below the {self._min_chars} character minimum"
            )
        return None, cleaned, None, quotes

    def _fetch_html(self, url: str) -> str:
        return fetch(url, policy=self._policy).text()

    def _resolve_engine(self) -> TTSEngine:
        if self._engine is None:
            log.info("loading TTS engine %s", kv(engine=self._engine_name))
            try:
                self._engine = get_engine(self._engine_name)
            except Exception as exc:
                raise TransientGenerationError(
                    f"could not load TTS engine {self._engine_name!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return self._engine


# HTTP statuses that are worth another attempt: rate limits, request timeouts,
# and anything the origin server blames on itself.
# 403 is here because bot-protection edges return it under load rather than 429.
# Twelve openai.com articles were discarded this way, and the same fetcher gets
# 200 for those URLs on a later attempt, so the rejection was never about the
# request being unacceptable. Retries are bounded, so a page that really does
# refuse everyone still ends up failed, just after a few attempts instead of one.
_RETRYABLE_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504, 507, 509})


def _classify_fetch_error(exc: FetchError) -> GenerationError:
    """Decide whether a fetch failure deserves a retry.

    Only a definite client-side rejection (a 4xx we cannot fix by waiting) is
    permanent. Anything else, including an unrecognized message, is treated as
    transient so a temporary outage does not permanently discard an article.
    """
    message = str(exc)
    status = _status_from_message(message)
    if status is None:
        return TransientGenerationError(message)
    if status in _RETRYABLE_STATUSES:
        return TransientGenerationError(message)
    if 400 <= status < 500:
        return PermanentGenerationError(message)
    return TransientGenerationError(message)


def _status_from_message(message: str) -> int | None:
    marker = "HTTP "
    if not message.startswith(marker):
        return None
    candidate = message[len(marker) :].split(" ", 1)[0]
    return int(candidate) if candidate.isdigit() else None


def _size_of(path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
