"""Finding quoted passages inside an extracted article.

The extractor's plain text is what gets narrated, and it is deliberately left
untouched: a block quote arrives as ordinary paragraphs, indistinguishable from
the author's own words. Its structured output does mark quotes, so that is used
only to locate them, never to rebuild the text. Anything that cannot be located
is simply left unquoted, which is what the narration did before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Passage:
    """A run of article text and who should read it."""

    text: str
    quoted: bool


def quotes_from_xml(xml: str | None) -> list[str]:
    """The text of every quote element, in document order."""
    if not xml:
        return []
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    found = []
    for element in root.iter("quote"):
        text = " ".join("".join(element.itertext()).split())
        if text:
            found.append(text)
    return found


def split_quoted(text: str, quotes: list[str]) -> list[Passage]:
    """Split text into narrated and quoted passages.

    Matching is done on whitespace-normalised copies because the two extractor
    outputs disagree about line breaks, while the words themselves agree. The
    offsets returned are into the original text, so nothing is reformatted.
    """
    if not text.strip():
        return []
    if not quotes:
        return [Passage(text, quoted=False)]

    flat, offsets = _flatten(text)
    passages: list[Passage] = []
    cursor = 0  # position in the original text
    search_from = 0  # position in the flattened text

    for quote in quotes:
        needle = " ".join(quote.split())
        if not needle:
            continue
        at = flat.find(needle, search_from)
        if at == -1:
            continue
        start = offsets[at]
        end = offsets[at + len(needle) - 1] + 1
        if start > cursor:
            _append(passages, text[cursor:start], quoted=False)
        _append(passages, text[start:end], quoted=True)
        cursor = end
        search_from = at + len(needle)

    if cursor < len(text):
        _append(passages, text[cursor:], quoted=False)
    return passages


def _flatten(text: str) -> tuple[str, list[int]]:
    """Whitespace-normalised text, plus each character's original offset."""
    out: list[str] = []
    offsets: list[int] = []
    previous_was_space = True
    for index, char in enumerate(text):
        if char.isspace():
            if previous_was_space:
                continue
            out.append(" ")
            offsets.append(index)
            previous_was_space = True
        else:
            out.append(char)
            offsets.append(index)
            previous_was_space = False
    if out and out[-1] == " ":
        out.pop()
        offsets.pop()
    return "".join(out), offsets


def _append(passages: list[Passage], text: str, *, quoted: bool) -> None:
    stripped = text.strip()
    if stripped:
        passages.append(Passage(stripped, quoted=quoted))
