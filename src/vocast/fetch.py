"""Fetch and extract article text from URLs using trafilatura."""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable

import trafilatura

from .quotes import quotes_from_xml

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _fetch_html(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                raw = zlib.decompress(raw)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code} {e.reason} from {url}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        raise ValueError(f"network error fetching {url}: {reason}") from e
    except TimeoutError:
        raise ValueError(f"timed out fetching {url}") from None


def fetch_article(
    url: str,
    *,
    html_fetcher: Callable[[str], str] | None = None,
) -> tuple[str | None, str, str | None]:
    """Fetch a URL and return (title, body_text, cover_image_url).

    cover_image_url is the article's og:image when the page advertises one,
    else None. Raises ValueError if the URL can't be fetched or has no
    extractable content.

    html_fetcher overrides how the page is retrieved. The default is the plain
    urllib path used by `vocast add`; the long-running service passes a fetcher
    that enforces size caps and refuses private addresses.
    """
    title, text, cover_image_url, _ = fetch_article_parts(
        url, html_fetcher=html_fetcher
    )
    return title, text, cover_image_url


def fetch_article_parts(
    url: str,
    *,
    html_fetcher: Callable[[str], str] | None = None,
) -> tuple[str | None, str, str | None, list[str]]:
    """As fetch_article, plus the text of any block quotes the page contains.

    The quotes come from a second pass over the same HTML in the extractor's
    structured format, which marks them. The narrated text is still the plain
    output, unchanged, so quotes can only ever affect which voice reads a
    passage, never what is read.
    """
    html = (html_fetcher or _fetch_html)(url)

    result = trafilatura.extract(
        html,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=False,
        prune_xpath=["//pre"],
    )
    if result is None:
        raise ValueError(f"could not extract content from {url}")

    data = json.loads(result)
    title = data.get("title")
    text = (data.get("text") or "").strip()
    cover_image_url = data.get("image") or None

    if not text:
        raise ValueError(f"extracted empty content from {url}")

    # A second pass: the plain output drops the quote elements, and rebuilding
    # the narration from the structured one would change the text.
    quotes = quotes_from_xml(
        trafilatura.extract(
            html,
            output_format="xml",
            include_comments=False,
            include_tables=False,
            prune_xpath=["//pre"],
        )
    )
    return title, text, cover_image_url, quotes
