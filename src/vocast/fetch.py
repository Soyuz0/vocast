"""Fetch and extract article text from URLs using trafilatura."""

from __future__ import annotations

import gzip
import json
import re
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable

import trafilatura
from lxml import etree
from lxml import html as lxml_html

from .quotes import quotes_from_xml

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_FLOW_CONTAINERS = frozenset({"article", "body", "div", "main", "section"})
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "aside",
        "blockquote",
        "div",
        "dl",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "menu",
        "ol",
        "p",
        "pre",
        "table",
        "ul",
    }
)
_PARAGRAPH_BREAK = re.compile(r"(\n[ \t\r]*\n+)")
_MIN_ORPHANED_CHARS = 400


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
    original_html = (html_fetcher or _fetch_html)(url)
    repaired_html = repair_orphaned_text_blocks(original_html)

    # Balanced mode can discard low-confidence blocks inside an otherwise long
    # article. Recall keeps those blocks; the explicit exclusions still bound it.
    options = {
        "output_format": "json",
        "with_metadata": True,
        "include_comments": False,
        "include_tables": False,
        "prune_xpath": ["//pre"],
        "favor_recall": True,
    }
    result = trafilatura.extract(
        original_html,
        **options,
    )
    selected_html = original_html
    if repaired_html != original_html:
        repaired_result = trafilatura.extract(repaired_html, **options)
        if _json_text_length(repaired_result) > _json_text_length(result):
            result = repaired_result
            selected_html = repaired_html
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
            selected_html,
            output_format="xml",
            include_comments=False,
            include_tables=False,
            prune_xpath=["//pre"],
            favor_recall=True,
        )
    )
    return title, text, cover_image_url, quotes


def _json_text_length(result: str | None) -> int:
    if not result:
        return 0
    try:
        return len(json.loads(result).get("text") or "")
    except (json.JSONDecodeError, AttributeError):
        return 0


def repair_orphaned_text_blocks(document: str) -> str:
    """Put substantial bare article prose into paragraphs before extraction.

    Some publishing bridges strip paragraph tags while leaving headings,
    quotations, and links intact. The prose then lives in the container's text
    nodes and child tails. Browsers display that text, but extractors do not
    reliably consider it article content. Repair only flow containers with a
    substantial amount of such text; ordinary, well-formed pages are returned
    byte-for-byte unchanged.
    """
    try:
        root = lxml_html.document_fromstring(document)
    except (etree.ParserError, ValueError):
        return document

    candidates = [
        element
        for element in root.iter()
        if _local_name(element) in _FLOW_CONTAINERS
        and _orphaned_text_length(element) >= _MIN_ORPHANED_CHARS
    ]
    if not candidates:
        return document

    for container in reversed(candidates):
        _paragraphize_direct_text(container)
    return lxml_html.tostring(root, encoding="unicode")


def _orphaned_text_length(element: etree._Element) -> int:
    text = element.text or ""
    tails = "".join(child.tail or "" for child in element)
    return len(f"{text}{tails}".strip())


def _paragraphize_direct_text(container: etree._Element) -> None:
    leading = container.text or ""
    children = list(container)
    container.text = None
    for child in children:
        container.remove(child)

    paragraph: etree._Element | None = None

    def add_text(text: str) -> None:
        nonlocal paragraph
        for part in _PARAGRAPH_BREAK.split(text):
            if not part:
                continue
            if _PARAGRAPH_BREAK.fullmatch(part):
                if paragraph is not None:
                    container.append(paragraph)
                    paragraph = None
                continue
            if not part.strip() and paragraph is None:
                continue
            if paragraph is None:
                paragraph = etree.Element("p")
            if len(paragraph):
                last = paragraph[-1]
                last.tail = (last.tail or "") + part
            else:
                paragraph.text = (paragraph.text or "") + part

    add_text(leading)
    for child in children:
        tail = child.tail or ""
        child.tail = None
        if _local_name(child) in _BLOCK_ELEMENTS:
            if paragraph is not None:
                container.append(paragraph)
                paragraph = None
            container.append(child)
        else:
            if paragraph is None:
                paragraph = etree.Element("p")
            paragraph.append(child)
        add_text(tail)
    if paragraph is not None:
        container.append(paragraph)


def _local_name(element: etree._Element) -> str:
    if not isinstance(element.tag, str):
        return ""
    return etree.QName(element).localname.lower()
