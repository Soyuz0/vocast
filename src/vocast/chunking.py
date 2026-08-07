import re

_SENTENCE_END = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][\"'”’)}\]])|"
    r"(?<=[.!?][\"'”’)}\]][\"'”’)}\]])|(?<=…))"
    r"\s+(?=\S)"
)
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")
_NONTERMINAL_ABBREVIATION = re.compile(
    r"\b(?:No|Fig|Dr|Mr|Mrs|Ms|Prof|St)\.$", re.IGNORECASE
)
_REPORTING_CLAUSE = re.compile(
    r"(?:I|[A-Z][a-z]+)\s+(?:said|asked|replied|answered|added|wrote)\b"
)


def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    sentences: list[str] = []
    for part in _SENTENCE_END.split(text):
        part = part.strip()
        if not part:
            continue
        if sentences and (
            _NONTERMINAL_ABBREVIATION.search(sentences[-1])
            or _is_reporting_clause(sentences[-1], part)
            or not _starts_sentence(part)
        ):
            sentences[-1] += " " + part
        else:
            sentences.append(part)
    return sentences


def _starts_sentence(text: str) -> bool:
    if text[0] in "\"“'‘([":
        return True
    return text[0].isupper()


def _is_reporting_clause(previous: str, following: str) -> bool:
    return bool(re.search(r"[?!][\"'”’]$", previous) and _REPORTING_CLAUSE.match(following))


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Group sentences into chunks no larger than max_chars, preserving boundaries."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    sentences = split_sentences(text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for s in sentences:
        if len(s) > max_chars:
            if current:
                chunks.append(" ".join(current))
                current, current_len = [], 0
            chunks.extend(_split_long(s, max_chars))
            continue
        added = len(s) + (1 if current else 0)
        if current_len + added > max_chars:
            chunks.append(" ".join(current))
            current, current_len = [s], len(s)
        else:
            current.append(s)
            current_len += added

    if current:
        chunks.append(" ".join(current))
    return chunks


def _split_long(sentence: str, max_chars: int) -> list[str]:
    pieces = _CLAUSE_END.split(sentence)
    out: list[str] = []
    current: list[str] = []
    current_len = 0
    for p in pieces:
        if len(p) > max_chars:
            if current:
                out.append(" ".join(current))
                current, current_len = [], 0
            out.extend(_hard_wrap(p, max_chars))
            continue
        added = len(p) + (1 if current else 0)
        if current_len + added > max_chars:
            out.append(" ".join(current))
            current, current_len = [p], len(p)
        else:
            current.append(p)
            current_len += added
    if current:
        out.append(" ".join(current))
    return out


def _hard_wrap(text: str, max_chars: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for w in words:
        if len(w) > max_chars:
            if buf:
                out.append(" ".join(buf))
                buf, buf_len = [], 0
            out.extend(w[index : index + max_chars] for index in range(0, len(w), max_chars))
            continue
        added = len(w) + (1 if buf else 0)
        if buf_len + added > max_chars and buf:
            out.append(" ".join(buf))
            buf, buf_len = [w], len(w)
        else:
            buf.append(w)
            buf_len += added
    if buf:
        out.append(" ".join(buf))
    return out
