from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from num2words import num2words

_INTEGER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
_NUMBER = rf"{_INTEGER}(?:\.[0-9]+)?|\.[0-9]+"
_NUMBER_END = r"(?!\w|\.[A-Za-z0-9]|,\d)"

_CURRENCY_RANGE = re.compile(
    rf"(?<!\w)(?P<symbol>[$£€])[ \t]*(?P<start>{_NUMBER})"
    rf"(?P<start_magnitude>[ \t]*(?:[kKmMbBtT]|bn|million|billion|trillion))?"
    rf"[ \t]*[–—-][ \t]*"
    rf"(?P<end_symbol>[$£€])?[ \t]*(?P<end>{_NUMBER})"
    rf"(?P<end_magnitude>[ \t]*(?:[kKmMbBtT]|bn|million|billion|trillion))?"
    rf"{_NUMBER_END}"
    r"(?![ \t]*%)"
)
_CURRENCY = re.compile(
    rf"(?<!\w)(?:(?P<outer_sign>[+\-−])[ \t]*)?(?P<symbol>[$£€])[ \t]*"
    rf"(?P<inner_sign>[+\-−]?)(?P<amount>{_NUMBER})"
    rf"(?P<magnitude>[ \t]*(?:[kKmMbBtT]|bn|million|billion|trillion))?"
    rf"{_NUMBER_END}",
    re.IGNORECASE,
)
_RANGE = re.compile(
    rf"(?<![\w.])(?P<start>[+\-−]?(?:{_NUMBER}))[ \t]*[–—-][ \t]*"
    rf"(?P<end>[+\-−]?(?:{_NUMBER}))(?P<percent>[ \t]*%)?{_NUMBER_END}"
)
_ORDINAL = re.compile(
    rf"(?<![\w.,])(?P<number>{_INTEGER})(?P<suffix>st|nd|rd|th)\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(
    rf"(?<![\w.])(?P<number>[+\-−]?(?:{_NUMBER}))[ \t]*%{_NUMBER_END}"
)
_PLAIN_NUMBER = re.compile(
    rf"(?<![\w.])(?P<number>[+\-−]?(?:{_NUMBER})){_NUMBER_END}"
)
_DECADE = re.compile(r"(?<![\w.])(?P<year>(?:18|19|20)[0-9]0)s\b")
_YEAR_RANGE = re.compile(
    r"(?<![\w.\-])(?P<start>(?:18|19|20)[0-9]{2})-(?P<end>[0-9]{2}|[0-9]{4})"
    r"(?![0-9-])"
)
_UNAMBIGUOUS_US_DATE = re.compile(
    r"(?<![\w.])(?P<month>0?[1-9]|1[0-2])/(?P<day>1[3-9]|2[0-9]|3[01])/"
    r"(?P<year>[0-9]{2}|[0-9]{4})(?![0-9/])"
)
_SCIENTIFIC_NUMBER = rf"(?:{_NUMBER})(?:[eE][+\-−]?[0-9]+)?"
_SCIENTIFIC_POWER = re.compile(
    rf"(?<![#\w.\-])(?P<mantissa>[+\-−]?(?:{_NUMBER}))[ \t]*×[ \t]*"
    rf"10\^(?P<exponent>[+\-−]?[0-9]+)(?!\w)"
)
_SCIENTIFIC_E = re.compile(
    rf"(?<![#\w.\-])(?P<mantissa>[+\-−]?(?:{_NUMBER}))[eE]"
    rf"(?P<exponent>[+\-−]?[0-9]+)(?!\w)"
)
_POWER = re.compile(
    rf"(?<![#\w.\-])(?P<base>[+\-−]?(?:{_NUMBER}))\^"
    rf"(?P<exponent>[+\-−]?[0-9]+)(?!\w)"
)
_LONG_SCIENTIFIC = re.compile(
    rf"(?<![#\w.\-])(?:[+\-−]?(?:{_NUMBER})[ \t]*×[ \t]*10\^"
    rf"[+\-−]?[0-9]+|[+\-−]?(?:{_NUMBER})[eE][+\-−]?[0-9]+|"
    rf"[+\-−]?(?:{_NUMBER})\^[+\-−]?[0-9]+){_NUMBER_END}"
)
_MONTH_DATE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)[ \t]+(?P<day>[0-9]{1,2})"
    r"(?![0-9.]|st\b|nd\b|rd\b|th\b)",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    rf"(?<![\w/\-=])(?P<operator>[<>~])[ \t]*"
    rf"(?=[+\-−]?{_SCIENTIFIC_NUMBER}{_NUMBER_END})"
)
_ISSUE_NUMBER = re.compile(
    rf"\b(?P<label>issue|PR|ticket)[ \t]+#(?P<number>{_NUMBER}){_NUMBER_END}",
    re.IGNORECASE,
)
_UNIT_TOKEN = (
    r"mg/cm\^2|nmol/L|ng/mL|Mbit/s|Gbit/s|GB/s|TB/s|mmHg|Mbps|Gbps|"
    r"mAh|kcal|m/s|m²|kWh|mph|fps|GHz|MHz|kHz|MB|KB|GB|TB|GW|kW|MW|"
    r"mV|Wh|Hz|mm|cm|km|μg|mg|kg|mL|IU|hrs?|ms|μm|V|W|°C|°F|℃|℉"
)
_UNIT = re.compile(
    rf"(?<![\w.])(?P<number>[+\-−]?(?:{_NUMBER}))[ \t]*"
    rf"(?P<unit>{_UNIT_TOKEN})"
    r"(?![\w/]|[-.][A-Za-z0-9])"
)
_UNIT_RANGE = re.compile(
    rf"(?<![\w.])(?P<start>[+\-−]?(?:{_NUMBER}))[ \t]*[–—-][ \t]*"
    rf"(?P<end>[+\-−]?(?:{_NUMBER}))[ \t]*(?P<unit>{_UNIT_TOKEN})"
    r"(?![\w/]|[-.][A-Za-z0-9])"
)
_WORD_UNIT_RANGE = re.compile(
    rf"(?<![\w.])(?P<start>[+\-−]?(?:{_NUMBER}))[ \t]*-[ \t]*"
    rf"(?P<end>[+\-−]?(?:{_NUMBER}))[ \t]+"
    r"(?P<unit>years?|months?|weeks?|days?|miles?|inches|feet)\b",
    re.IGNORECASE,
)
_HYPHENATED_VERSION = re.compile(
    r"\b(?P<product>[A-Za-z][A-Za-z0-9-]*)-(?P<version>[0-9]+(?:\.[0-9]+)+)"
    r"(?P<suffix>-[A-Za-z][A-Za-z0-9]*)?\b"
)
_EXPLICIT_VERSION = re.compile(
    r"\b(?P<label>v|version|release)[ \t]*(?P<version>[0-9]+(?:\.[0-9]+){2,})\b",
    re.IGNORECASE,
)
_SLASH_ORDINAL = re.compile(
    r"(?<![\w.])(?P<numerator>[0-9]+)/(?P<denominator>[0-9]+)"
    r"(?:st|nd|rd|th)s?\b",
    re.IGNORECASE,
)
_CONTEXTUAL_FRACTION = re.compile(
    r"(?<![\w.])(?P<numerator>[0-9]+)/(?P<denominator>[0-9]+)"
    r"(?=[ \t]+(?:of|chance|support|people|population)\b)",
    re.IGNORECASE,
)
_DOTTED_INITIALISM = re.compile(
    r"(?<!\w)[A-Za-z]\.(?:[ \t]*[A-Za-z]\.)+(?![A-Za-z])"
)
_AI_INITIALISM = re.compile(r"\b(?:AGI|ASI|AIXI|AISI)\b")
_ROMAN = re.compile(r"\b[IVXLCDM]+\b")
_ROMAN_CUE = re.compile(
    r"(?:chapter|part|book|volume|section|act|scene|appendix|world war|"
    r"phase|stage|level|episode|super bowl|article|figure|table|amendment)"
    r"[ \t]+$",
    re.IGNORECASE,
)
_ROMAN_NAME_CUE = re.compile(
    r"(?:Benedict|Charles|Edward|Elizabeth|George|Henry|James|John|Leo|Louis|"
    r"Paul|Philip|Pius|Richard|Rocky|William)[ \t]+$",
    re.IGNORECASE,
)
_ABBREVIATION = re.compile(
    r"\b(?:e\.g\.|i\.e\.|etc\.|vs\.)(?!\w)", re.IGNORECASE
)
_TITLE = re.compile(r"\b(?P<title>Dr|Mr|Mrs|Prof)\.[ \t]+(?=[A-Z])")
_INVALID_GROUPING = re.compile(
    r"(?<!\w)[0-9]+(?:,[0-9]+)+(?:\.[0-9]+)?(?!\w)"
)
_LONG_NUMBER = re.compile(r"(?<![\w.])[0-9][0-9,]*(?:\.[0-9]+)?(?!\w)")
_MARKDOWN_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")
_MARKDOWN_BLOCKQUOTE = re.compile(r"(?m)^[ \t]*>[ \t]*")
_INLINE_CODE = re.compile(
    r"```.*?```|~~~.*?~~~|``[^`\n]*``|`[^`\n]*`", re.DOTALL
)
_HTML_TAG = re.compile(r"<[A-Za-z!/](?:\"[^\"]*\"|'[^']*'|[^'\">])*>")
_TERMINAL_HEART = re.compile(
    r"(?<!\w)<3"
    r"(?![ \t]+(?:years?|months?|weeks?|days?|hours?|minutes?|seconds?)\b)"
    r"(?![0-9])"
)
_ASCII_ARROW = re.compile(r"[ \t]*->[ \t]*")
_ACCESSIBILITY_ARTIFACT = re.compile(
    r"[ \t]*\((?:open|opens) in a new window\)", re.IGNORECASE
)
_FOOTNOTE_LINE_PREFIX = re.compile(r"(?m)^[ \t]*-[ \t]*\^[ \t]*")
_FOOTNOTE_BACKLINK = re.compile(r"[ \t]*↩(?:\ufe0e|\ufe0f)*(?=[ \t]*(?:\n|$))")
_GLUED_BRACKET_CITATION = re.compile(
    r"(\[[0-9]{1,3}(?:[ \t]*[-–,][ \t]*[0-9]{1,3})*\])(?=[A-Za-z])"
)
_TERMINAL_BRACKET_CITATION = re.compile(
    r"(?P<end>[.!?][\"'”’)}\]]*)"
    r"\[[0-9]{1,3}(?:[ \t]*[-–,][ \t]*[0-9]{1,3})*\]"
    r"(?P<next>[A-Z])"
)
_TERMINAL_SPACED_CITATION = re.compile(
    r"(?P<end>[.!?][\"'”’)}\]]*)"
    r"\[[0-9]{1,3}(?:[ \t]*[-–,][ \t]*[0-9]{1,3})*\]"
    r"(?=[ \t]*(?:\n|$)|[ \t\n]+(?:[\"“'(\[]?[A-Z]|\([a-z]\)))"
)
_TERMINAL_SUPERSCRIPT_CITATION = re.compile(
    r"(?P<end>[.!?][\"'”’)}\]]*)[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?=[ \t\n]|$)"
)

_PROTECTED_PATTERNS = (
    re.compile(r"https?://[^\s<>()]+", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b[0-9]+(?:[-–—][0-9]+){2,}\b"),
    re.compile(r"\b[0-9]+[WV][–—-][0-9]+\b"),
    re.compile(r"(?<!\w)[0-9]+[–—-][0-9]+[A-Za-z]+[0-9]+\b"),
    re.compile(r"#[0-9A-Fa-f]{3,8}\b"),
    re.compile(r"\b0[xX][0-9A-Fa-f]+(?:\^[0-9]+)?\b"),
    re.compile(
        rf"(?<!\w)[+\-−]?[ \t]*[$£€][ \t]*[+\-−]?[ \t]*(?:{_NUMBER})"
        rf"[ \t]*[–—-][ \t]*[+\-−]?[ \t]*(?:[$£€])?[ \t]*[+\-−]?[ \t]*"
        rf"(?:{_NUMBER})[ \t]*%"
    ),
    re.compile(
        rf"(?<!\w)[$£€][ \t]*(?:{_NUMBER})(?:[eE][+\-−]?[0-9]+|"
        rf"\^[+\-−]?[0-9]+|[ \t]*×[ \t]*10\^[+\-−]?[0-9]+)"
    ),
    _HTML_TAG,
    _TERMINAL_HEART,
    re.compile(r"\b(?:v(?:ersion)?[ \t]*)?\d+(?:\.\d+){2,}\b", re.IGNORECASE),
    re.compile(
        r"\b\d{4}-\d{2}-\d{2}T\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
        r"(?:Z|[+\-]\d{2}:?\d{2})?\b"
    ),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?"
        r"(?:[ \t]*[ap]\.?m\.?|Z|[+\-]\d{2}:?\d{2})?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{1,2}:\d{2}(?:[ \t]*[ap]\.?m\.?)?\b", re.IGNORECASE),
    re.compile(r"\b\d{3}[- ]\d{3}-\d{4}\b"),
    re.compile(r"\(\d{3}\)[ \t]*\d{3}-\d{4}\b"),
    re.compile(r"\b\d{3}-\d{4}\b"),
    re.compile(r"\bISBN(?:-1[03])?:?[ \t]+[0-9X-]+\b", re.IGNORECASE),
    re.compile(
        rf"(?<![\w.$£€])[+\-−]?(?:{_NUMBER})[ \t]*-[ \t]*"
        rf"[+\-−]?(?:{_NUMBER}){_NUMBER_END}"
        rf"(?![ \t]*(?:%|{_UNIT_TOKEN})(?![\w/]))"
    ),
    re.compile(rf"(?<![\w.])(?:{_NUMBER})(?:/(?:{_NUMBER}))+{_NUMBER_END}"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*-[0-9][A-Za-z0-9-]*\b(?!\.[0-9])"),
    re.compile(rf"(?<!\w)[+\-−][ \t]*[$£€][ \t]*[+\-−](?:{_NUMBER})"),
)

_CURRENCY_NAMES = {
    "$": ("dollar", "dollars", "cent", "cents"),
    "£": ("pound", "pounds", "penny", "pence"),
    "€": ("euro", "euros", "cent", "cents"),
}
_MAGNITUDES = {
    "k": "thousand",
    "m": "million",
    "bn": "billion",
    "b": "billion",
    "t": "trillion",
    "million": "million",
    "billion": "billion",
    "trillion": "trillion",
}
_ABBREVIATIONS = {
    "e.g.": "for example,",
    "i.e.": "that is,",
    "etc.": "et cetera",
    "vs.": "versus",
}
_TITLES = {
    "Dr": "Doctor",
    "Mr": "Mister",
    "Mrs": "Missus",
    "Prof": "Professor",
}
_MONTH_DAYS = {
    "january": 31,
    "february": 29,
    "march": 31,
    "april": 30,
    "may": 31,
    "june": 30,
    "july": 31,
    "august": 31,
    "september": 30,
    "october": 31,
    "november": 30,
    "december": 31,
}
_DECADE_NAMES = {
    20: "twenties",
    30: "thirties",
    40: "forties",
    50: "fifties",
    60: "sixties",
    70: "seventies",
    80: "eighties",
    90: "nineties",
}
_FRACTION_NAMES = {
    2: ("half", "halves"),
    3: ("third", "thirds"),
    4: ("quarter", "quarters"),
    5: ("fifth", "fifths"),
    6: ("sixth", "sixths"),
    7: ("seventh", "sevenths"),
    8: ("eighth", "eighths"),
    9: ("ninth", "ninths"),
    10: ("tenth", "tenths"),
}
_UNIT_NAMES = {
    "mg/cm^2": (
        "milligram per square centimeter",
        "milligrams per square centimeter",
    ),
    "nmol/L": ("nanomole per liter", "nanomoles per liter"),
    "ng/mL": ("nanogram per milliliter", "nanograms per milliliter"),
    "GB/s": ("gigabyte per second", "gigabytes per second"),
    "TB/s": ("terabyte per second", "terabytes per second"),
    "Mbit/s": ("megabit per second", "megabits per second"),
    "Gbit/s": ("gigabit per second", "gigabits per second"),
    "mmHg": ("millimeter of mercury", "millimeters of mercury"),
    "Mbps": ("megabit per second", "megabits per second"),
    "Gbps": ("gigabit per second", "gigabits per second"),
    "mAh": ("milliamp hour", "milliamp hours"),
    "kcal": ("kilocalorie", "kilocalories"),
    "m/s": ("meter per second", "meters per second"),
    "m²": ("square meter", "square meters"),
    "kWh": ("kilowatt hour", "kilowatt hours"),
    "mph": ("mile per hour", "miles per hour"),
    "fps": ("frame per second", "frames per second"),
    "GHz": ("gigahertz", "gigahertz"),
    "MHz": ("megahertz", "megahertz"),
    "kHz": ("kilohertz", "kilohertz"),
    "MB": ("megabyte", "megabytes"),
    "KB": ("kilobyte", "kilobytes"),
    "GB": ("gigabyte", "gigabytes"),
    "TB": ("terabyte", "terabytes"),
    "GW": ("gigawatt", "gigawatts"),
    "kW": ("kilowatt", "kilowatts"),
    "MW": ("megawatt", "megawatts"),
    "mV": ("millivolt", "millivolts"),
    "Wh": ("watt hour", "watt hours"),
    "Hz": ("hertz", "hertz"),
    "mm": ("millimeter", "millimeters"),
    "cm": ("centimeter", "centimeters"),
    "km": ("kilometer", "kilometers"),
    "μg": ("microgram", "micrograms"),
    "mg": ("milligram", "milligrams"),
    "kg": ("kilogram", "kilograms"),
    "mL": ("milliliter", "milliliters"),
    "IU": ("international unit", "international units"),
    "hr": ("hour", "hours"),
    "hrs": ("hour", "hours"),
    "ms": ("millisecond", "milliseconds"),
    "μm": ("micrometer", "micrometers"),
    "V": ("volt", "volts"),
    "W": ("watt", "watts"),
    "°C": ("degree Celsius", "degrees Celsius"),
    "℃": ("degree Celsius", "degrees Celsius"),
    "°F": ("degree Fahrenheit", "degrees Fahrenheit"),
    "℉": ("degree Fahrenheit", "degrees Fahrenheit"),
}
_UNICODE_TRANSLATION = str.maketrans(
    {
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
        "\u200b": " ",
        "\u2060": "",
        "\ufeff": "",
        "\u00ad": "",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2212": "−",
        "\u3002": ".",
        "\uff01": "!",
        "\uff1f": "?",
        "\uff0c": ",",
        "\uff1b": ";",
        "\uff1a": ":",
        "\uff0e": ".",
        "\uff05": "%",
        "\uff06": "&",
        "\u00b5": "μ",
        "\u2028": "\n",
        "\u2029": "\n\n",
    }
)
_TERMINAL_PUNCTUATION = re.compile(r"[.!?…:;,–—-][\"'”’)}\]]*$")


def normalize_for_speech(text: str) -> str:
    """Turn unambiguous English notation into text both TTS engines read alike."""
    if not text:
        return text

    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"(?<=[0-9])[\u00a0\u2007\u202f](?=[0-9]{3}\b)", ",", text)
    text = text.translate(_UNICODE_TRANSLATION)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    protected: list[str] = []
    text = _stash_inline_code(text, protected)
    text = _ASCII_ARROW.sub(" right arrow ", text)
    text = _ACCESSIBILITY_ARTIFACT.sub("", text)
    text = _FOOTNOTE_LINE_PREFIX.sub("", text)
    text = _FOOTNOTE_BACKLINK.sub("", text)
    text = _MARKDOWN_HEADING.sub("", text)
    text = _MARKDOWN_BLOCKQUOTE.sub(_strip_markdown_blockquote, text)
    text = _TERMINAL_BRACKET_CITATION.sub(r"\g<end> \g<next>", text)
    text = _TERMINAL_SPACED_CITATION.sub(r"\g<end>", text)
    text = _TERMINAL_SUPERSCRIPT_CITATION.sub(r"\g<end>", text)
    text = _GLUED_BRACKET_CITATION.sub(r"\1 ", text)
    text = _punctuate_sections(text)
    text = _ISSUE_NUMBER.sub(_expand_issue_number, text)
    text = _EXPLICIT_VERSION.sub(_expand_explicit_version, text)
    text = _YEAR_RANGE.sub(_expand_year_range, text)
    text = _UNAMBIGUOUS_US_DATE.sub(_expand_us_date, text)
    text = _stash_oversized_fractions(text, protected)
    text = _SLASH_ORDINAL.sub(_expand_fraction, text)
    text = _CONTEXTUAL_FRACTION.sub(_expand_fraction, text)
    text = _WORD_UNIT_RANGE.sub(_expand_word_unit_range, text)

    for pattern in _PROTECTED_PATTERNS:
        text = _stash_matches(text, pattern, protected)
    text = _stash_long_scientific(text, protected)
    text = _stash_long_numbers(text, protected)
    text = _stash_invalid_grouping(text, protected)

    text = _ABBREVIATION.sub(lambda match: _expand_abbreviation(match, text), text)
    text = _TITLE.sub(lambda match: _TITLES[match.group("title")] + " ", text)
    text = _AI_INITIALISM.sub(_expand_ai_initialism, text)
    text = _ROMAN.sub(lambda match: _expand_roman(match, text), text)
    text = _DOTTED_INITIALISM.sub(lambda match: _expand_initialism(match, text), text)
    text = _HYPHENATED_VERSION.sub(_expand_hyphenated_version, text)
    text = _CURRENCY_RANGE.sub(_expand_currency_range, text)
    text = _CURRENCY.sub(_expand_currency, text)
    text = _COMPARISON.sub(_expand_comparison, text)
    text = _SCIENTIFIC_POWER.sub(_expand_scientific_power, text)
    text = _SCIENTIFIC_E.sub(_expand_scientific_e, text)
    text = _POWER.sub(_expand_power, text)
    text = _UNIT_RANGE.sub(_expand_unit_range, text)
    text = _RANGE.sub(_expand_range, text)
    text = _MONTH_DATE.sub(lambda match: _expand_month_date(match, text), text)
    text = _DECADE.sub(_expand_decade, text)
    text = _ORDINAL.sub(_expand_ordinal, text)
    text = _UNIT.sub(_expand_unit, text)
    text = _PERCENT.sub(
        lambda match: f"{_spoken_number(match.group('number'))} percent", text
    )
    text = _PLAIN_NUMBER.sub(lambda match: _spoken_number(match.group("number")), text)

    for index, original in enumerate(protected):
        text = text.replace(_placeholder(index), original)
    return text


def _punctuate_sections(text: str) -> str:
    lines = text.split("\n")
    for index, line in enumerate(lines[:-1]):
        stripped = line.rstrip()
        if not stripped or _TERMINAL_PUNCTUATION.search(stripped):
            continue

        next_line = lines[index + 1]
        paragraph_break = not next_line.strip()
        heading_break = bool(next_line.strip()) and _looks_like_heading(stripped)
        if paragraph_break or heading_break:
            lines[index] = stripped + "."
    return "\n".join(lines)


def _looks_like_heading(line: str) -> bool:
    words = line.split()
    if not 0 < len(words) <= 10 or len(line) > 80:
        return False
    cased_words = [word for word in words if any(char.isalpha() for char in word)]
    return bool(cased_words) and all(
        word.isupper() or next(char for char in word if char.isalpha()).isupper()
        for word in cased_words
    )


def _strip_markdown_blockquote(match: re.Match[str]) -> str:
    following = match.string[match.end() :]
    if re.match(r"[+\-−]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+\-−]?[0-9]+)?", following):
        if not re.match(r"[0-9]+[ \t]*\)", following):
            return match.group()
    return ""


def _stash_matches(text: str, pattern: re.Pattern[str], values: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        values.append(match.group())
        return _placeholder(len(values) - 1)

    return pattern.sub(replace, text)


def _stash_inline_code(text: str, values: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        values.append(match.group())
        return _placeholder(len(values) - 1)

    return _INLINE_CODE.sub(replace, text)


def _stash_invalid_grouping(text: str, values: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group()
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", value):
            return value
        values.append(value)
        return _placeholder(len(values) - 1)

    return _INVALID_GROUPING.sub(replace, text)


def _stash_long_numbers(text: str, values: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group()
        if sum(char.isdigit() for char in value) <= 30:
            return value
        values.append(value)
        return _placeholder(len(values) - 1)

    return _LONG_NUMBER.sub(replace, text)


def _stash_long_scientific(text: str, values: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if all(len(run) <= 30 for run in re.findall(r"[0-9]+", match.group())):
            return match.group()
        values.append(match.group())
        return _placeholder(len(values) - 1)

    return _LONG_SCIENTIFIC.sub(replace, text)


def _stash_oversized_fractions(text: str, values: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if len(match.group("numerator")) <= 30 and len(match.group("denominator")) <= 30:
            return match.group()
        values.append(match.group())
        return _placeholder(len(values) - 1)

    return _SLASH_ORDINAL.sub(replace, text)


def _placeholder(index: int) -> str:
    return f"\ue000{_letters(index)}\ue001"


def _letters(index: int) -> str:
    result = ""
    while True:
        index, remainder = divmod(index, 26)
        result = chr(ord("A") + remainder) + result
        if not index:
            return result
        index -= 1


def _expand_abbreviation(match: re.Match[str], text: str) -> str:
    replacement = _ABBREVIATIONS[match.group().lower()]
    if match.group()[0].isupper():
        replacement = replacement[0].upper() + replacement[1:]
    if replacement.endswith(",") and re.match(
        r"[,;:!?]", text[match.end() :]
    ):
        replacement = replacement[:-1]
    if _is_sentence_final(
        match, text, uppercase_starts_sentence=match.group().lower() == "etc."
    ):
        replacement = replacement.rstrip(",") + "."
    return replacement


def _expand_initialism(match: re.Match[str], text: str) -> str:
    letters = "".join(char for char in match.group() if char.isalpha()).upper()
    if set(letters) <= set("IVXLCDM"):
        letters = " ".join(letters)
    if _is_sentence_final(match, text):
        return letters + "."
    return letters


def _expand_ai_initialism(match: re.Match[str]) -> str:
    return {
        "AGI": "Aye Gee Eye",
        "ASI": "Aye Ess Eye",
        "AIXI": "Aye Eye Ex Eye",
        "AISI": "Aye Eye Ess Eye",
    }[match.group()]


def _is_sentence_final(
    match: re.Match[str], text: str, *, uppercase_starts_sentence: bool = False
) -> bool:
    remainder = text[match.end() :]
    if not remainder.strip():
        return True
    if re.match(r"^[ \t]*\n", remainder):
        return True
    closing = re.match(r"^[ \t]*[\"'”’)}\]]+", remainder)
    if closing:
        after_closing = remainder[closing.end() :]
        return bool(
            not after_closing.strip()
            or re.match(r"^[ \t]*\n", after_closing)
            or re.match(r"^[ \t]+[A-Z]", after_closing)
        )
    return uppercase_starts_sentence and bool(re.match(r"^[ \t]+[A-Z]", remainder))


def _expand_roman(match: re.Match[str], text: str) -> str:
    numeral = match.group()
    value = _roman_value(numeral)
    if value is None:
        return numeral

    before = text[max(0, match.start() - 24) : match.start()]
    after = text[match.end() : match.end() + 16]
    if before.endswith(".") or re.match(r"^\.[A-Za-z]", after):
        return numeral
    section_context = bool(_ROMAN_CUE.search(before))
    name_context = bool(_ROMAN_NAME_CUE.search(before))
    standalone = not text[: match.start()].strip(" \t\n") and not text[
        match.end() :
    ].strip(" \t\n.!?")
    if numeral == "I" and standalone:
        return numeral
    if numeral == "I" and name_context:
        royal_title = bool(
            re.search(r"\b(?:King|Queen|Pope)[ \t]+[A-Z][a-z]+[ \t]+$", before)
        )
        possessive = bool(re.match(r"^[’']s\b", after))
        terminal = not after.strip(" \t\n.!?")
        if not (royal_title or possessive or terminal):
            return numeral
    if name_context:
        ordinal = _clean_number_words(num2words(value, lang="en", to="ordinal"))
        return f"the {ordinal}"
    if not (section_context or standalone):
        return numeral
    return _cardinal(value)


def _roman_value(numeral: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for char in reversed(numeral):
        value = values[char]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total if _to_roman(total) == numeral else None


def _to_roman(value: int) -> str:
    numerals = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    parts: list[str] = []
    for number, numeral in numerals:
        count, value = divmod(value, number)
        parts.append(numeral * count)
    return "".join(parts)


def _expand_currency_range(match: re.Match[str]) -> str:
    symbol = match.group("symbol")
    start_magnitude = match.group("start_magnitude")
    end_magnitude = match.group("end_magnitude")
    if end_magnitude and not start_magnitude:
        names = _CURRENCY_NAMES[symbol]
        magnitude = _MAGNITUDES[end_magnitude.strip().lower()]
        return (
            f"{_spoken_number(match.group('start'))} to "
            f"{_spoken_number(match.group('end'))} {magnitude} {names[1]}"
        )
    start = _currency_words(symbol, match.group("start"), start_magnitude)
    end = _currency_words(
        match.group("end_symbol") or symbol, match.group("end"), end_magnitude
    )
    return f"{start} to {end}"


def _expand_currency(match: re.Match[str]) -> str:
    sign = match.group("outer_sign") or match.group("inner_sign")
    if match.group("outer_sign") and match.group("inner_sign"):
        return match.group()
    result = _currency_words(
        match.group("symbol"), match.group("amount"), match.group("magnitude")
    )

    if sign in {"-", "−"}:
        return "minus " + result
    if sign == "+":
        return "plus " + result
    return result


def _currency_words(symbol: str, raw_amount: str, magnitude: str | None = None) -> str:
    amount = raw_amount.replace(",", "")
    names = _CURRENCY_NAMES[symbol]
    if magnitude:
        spoken = _spoken_number(amount)
        unit = _MAGNITUDES[magnitude.strip().lower()]
        return f"{spoken} {unit} {names[1]}"

    whole, dot, fraction = amount.partition(".")
    major = int(whole or "0")
    if not dot:
        return f"{_cardinal(major)} {names[0] if major == 1 else names[1]}"
    if len(fraction) > 2:
        unit = names[0] if major == 1 and not any(digit != "0" for digit in fraction) else names[1]
        return f"{_spoken_number(amount)} {unit}"

    minor = int(fraction.ljust(2, "0"))
    parts: list[str] = []
    if major:
        parts.append(f"{_cardinal(major)} {names[0] if major == 1 else names[1]}")
    if minor:
        minor_words = f"{_cardinal(minor)} {names[2] if minor == 1 else names[3]}"
        parts.append(minor_words)
    return " and ".join(parts) if parts else f"zero {names[1]}"


def _expand_range(match: re.Match[str]) -> str:
    start = _spoken_number(match.group("start"))
    end = _spoken_number(match.group("end"))
    result = f"{start} to {end}"
    if match.group("percent"):
        result += " percent"
    return result


def _expand_unit_range(match: re.Match[str]) -> str:
    singular, plural = _UNIT_NAMES[match.group("unit")]
    unit = singular if _is_one(match.group("end")) else plural
    return (
        f"{_spoken_number(match.group('start'))} to "
        f"{_spoken_number(match.group('end'))} {unit}"
    )


def _expand_word_unit_range(match: re.Match[str]) -> str:
    return (
        f"{_spoken_number(match.group('start'))} to "
        f"{_spoken_number(match.group('end'))} {match.group('unit')}"
    )


def _expand_issue_number(match: re.Match[str]) -> str:
    if sum(char.isdigit() for char in match.group("number")) > 30:
        return match.group()
    return f"{match.group('label')} number {_spoken_number(match.group('number'))}"


def _expand_hyphenated_version(match: re.Match[str]) -> str:
    version = " point ".join(
        _spoken_version_component(component)
        for component in match.group("version").split(".")
    )
    suffix = match.group("suffix")
    return f"{match.group('product')} {version}{' ' + suffix[1:] if suffix else ''}"


def _expand_explicit_version(match: re.Match[str]) -> str:
    if any(len(component) > 30 for component in match.group("version").split(".")):
        return match.group()
    version = " point ".join(
        _spoken_version_component(component)
        for component in match.group("version").split(".")
    )
    label = "version" if match.group("label").lower() == "v" else match.group("label")
    return f"{label} {version}"


def _expand_fraction(match: re.Match[str]) -> str:
    if len(match.group("numerator")) > 30 or len(match.group("denominator")) > 30:
        return match.group()
    numerator = int(match.group("numerator"))
    denominator = int(match.group("denominator"))
    names = _FRACTION_NAMES.get(denominator)
    if names is None:
        return match.group()
    denominator_words = names[0] if numerator == 1 else names[1]
    return f"{_cardinal(numerator)} {denominator_words}"


def _spoken_version_component(component: str) -> str:
    if len(component) > 1 and component.startswith("0"):
        return " ".join(_cardinal(int(digit)) for digit in component)
    return _cardinal(int(component))


def _expand_month_date(match: re.Match[str], text: str) -> str:
    day = int(match.group("day"))
    if not 1 <= day <= _MONTH_DAYS[match.group("month").lower()]:
        return match.group()
    if match.group("month").lower() == "may":
        before = text[max(0, match.start() - 12) : match.start()]
        after = text[match.end() :]
        date_context = bool(
            re.search(r"\b(?:on|by|since|until|from|dated)[ \t]+$", before, re.IGNORECASE)
            or re.match(r"^[ \t]*(?:,|[0-9]{4}\b)", after)
        )
        if match.group("month") != "May" or (
            not date_context and re.match(r"^[ \t]+[a-z]", after)
        ):
            return match.group()
    ordinal = _clean_number_words(num2words(day, lang="en", to="ordinal"))
    return f"{match.group('month')} {ordinal}"


def _expand_decade(match: re.Match[str]) -> str:
    year = int(match.group("year"))
    century, decade = divmod(year, 100)
    if decade == 0:
        return "two thousands" if year == 2000 else f"{_cardinal(century)} hundreds"
    if decade == 10:
        return f"{_cardinal(century)} tens"
    return f"{_cardinal(century)} {_DECADE_NAMES[decade]}"


def _expand_year_range(match: re.Match[str]) -> str:
    start = int(match.group("start"))
    raw_end = match.group("end")
    end = int(raw_end) if len(raw_end) == 4 else start // 100 * 100 + int(raw_end)
    return f"{_year_words(start)} to {_year_words(end)}"


def _expand_us_date(match: re.Match[str]) -> str:
    month = int(match.group("month"))
    day = int(match.group("day"))
    year = int(match.group("year"))
    if day > _MONTH_DAYS[list(_MONTH_DAYS)[month - 1]]:
        return match.group()
    if year < 100:
        year += 2000
    month_name = list(_MONTH_DAYS)[month - 1].title()
    ordinal = _clean_number_words(num2words(day, lang="en", to="ordinal"))
    return f"{month_name} {ordinal}, {_year_words(year)}"


def _year_words(year: int) -> str:
    return _clean_number_words(num2words(year, lang="en", to="year"))


def _expand_comparison(match: re.Match[str]) -> str:
    return (
        {"<": "less than", ">": "greater than", "~": "approximately"}[
            match.group("operator")
        ]
        + " "
    )


def _expand_scientific_power(match: re.Match[str]) -> str:
    return (
        f"{_spoken_number(match.group('mantissa'))} times ten to the power of "
        f"{_spoken_number(match.group('exponent'))}"
    )


def _expand_scientific_e(match: re.Match[str]) -> str:
    return (
        f"{_spoken_number(match.group('mantissa'))} times ten to the power of "
        f"{_spoken_number(match.group('exponent'))}"
    )


def _expand_power(match: re.Match[str]) -> str:
    return (
        f"{_spoken_number(match.group('base'))} to the power of "
        f"{_spoken_number(match.group('exponent'))}"
    )


def _expand_unit(match: re.Match[str]) -> str:
    number = match.group("number")
    singular, plural = _UNIT_NAMES[match.group("unit")]
    return f"{_spoken_number(number)} {singular if _is_one(number) else plural}"


def _is_one(number: str) -> bool:
    normalized = number.replace(",", "").replace("−", "-").lstrip("+")
    return abs(Decimal(normalized)) == 1


def _expand_ordinal(match: re.Match[str]) -> str:
    digits = match.group("number").replace(",", "")
    if len(digits) > 30:
        return match.group()
    value = int(digits)
    suffix = match.group("suffix").lower()
    expected = (
        "th"
        if 10 <= value % 100 <= 20
        else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    )
    if suffix != expected:
        return match.group()
    return _clean_number_words(num2words(value, lang="en", to="ordinal"))


def _spoken_number(value: str) -> str:
    sign = ""
    if value[:1] in {"+", "-", "−"}:
        sign = "plus " if value[0] == "+" else "minus "
        value = value[1:]
    value = value.replace(",", "")
    whole, dot, fraction = value.partition(".")
    if dot:
        integer_words = _cardinal(int(whole or "0"))
        decimal_words = " ".join(_cardinal(int(digit)) for digit in fraction)
        return f"{sign}{integer_words} point {decimal_words}"
    if len(value) > 1 and value.startswith("0"):
        return sign + " ".join(_cardinal(int(digit)) for digit in value)
    return sign + _cardinal(int(value))


def _cardinal(value: int) -> str:
    if 1100 <= value <= 1900 and value % 100 == 0:
        return f"{_cardinal(value // 100)} hundred"
    return _clean_number_words(num2words(value, lang="en"))


def _clean_number_words(words: str) -> str:
    return words.replace(",", "").replace(" and ", " ")
