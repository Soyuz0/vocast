import numpy as np
import pytest

from vocast.chunking import chunk_text
from vocast.engines.engine import AudioChunk
from vocast.pipeline import synthesize_article, synthesize_passages
from vocast.text_normalization import normalize_for_speech


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("There are 100000 listeners.", "There are one hundred thousand listeners."),
        ("The batch has 1,200 items.", "The batch has twelve hundred items."),
        ("The batch has 1\u202f200 items.", "The batch has twelve hundred items."),
        ("Version 5.6 is ready.", "Version five point six is ready."),
        ("Accuracy reached 99.5%.", "Accuracy reached ninety-nine point five percent."),
        ("She finished 21st.", "She finished twenty-first."),
        ("It was the 1,000th run.", "It was the one thousandth run."),
        ("The range was 5–10%.", "The range was five to ten percent."),
        ("Growth was -5–10%.", "Growth was minus five to ten percent."),
        ("Agent 007 arrived.", "Agent zero zero seven arrived."),
        ("The change was −12.", "The change was minus twelve."),
        ("Culture changed in the 1960s.", "Culture changed in the nineteen sixties."),
        ("Media changed in the 2000s.", "Media changed in the two thousands."),
        ("The 2014-15 season changed.", "The twenty fourteen to twenty fifteen season changed."),
        ("Reading List 07/25/26.", "Reading List July twenty-fifth, twenty twenty-six."),
        ("On July 21, 2026, it changed.", "On July twenty-first, two thousand twenty-six, it changed."),
        ("Score was 3–1.", "Score was three to one."),
        ("May 5 people enter?", "May five people enter?"),
    ],
)
def test_numbers_are_written_as_they_should_be_spoken(source: str, spoken: str):
    assert normalize_for_speech(source) == spoken


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("It cost $1.", "It cost one dollar."),
        ("It cost $12.50.", "It cost twelve dollars and fifty cents."),
        ("It cost $1,200.50.", "It cost twelve hundred dollars and fifty cents."),
        ("It cost $1.000.", "It cost one point zero zero zero dollar."),
        ("It cost $0.01.", "It cost one cent."),
        ("Revenue reached $100000.", "Revenue reached one hundred thousand dollars."),
        ("Revenue reached $1.2bn.", "Revenue reached one point two billion dollars."),
        ("Revenue reached $600B.", "Revenue reached six hundred billion dollars."),
        ("The valuation was $2T.", "The valuation was two trillion dollars."),
        ("The fee was £2.01.", "The fee was two pounds and one penny."),
        ("The fee was €1.50.", "The fee was one euro and fifty cents."),
        ("The estimate was $5–$10.", "The estimate was five dollars to ten dollars."),
    ],
)
def test_currency_follows_english_amount_then_unit_order(source: str, spoken: str):
    assert normalize_for_speech(source) == spoken


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("It is 20°C outside.", "It is twenty degrees Celsius outside."),
        ("Speed was 60 mph.", "Speed was sixty miles per hour."),
        ("Take 400 IU daily.", "Take four hundred international units daily."),
        ("The file is 4GB.", "The file is four gigabytes."),
        ("The rate is 10 Hz.", "The rate is ten hertz."),
        ("Use 1 mg in 2 mL.", "Use one milligram in two milliliters."),
        ("It moved at 5 m/s.", "It moved at five meters per second."),
        ("The area is 1 m².", "The area is one square meter."),
        (
            "Bandwidth was 153GB/s.",
            "Bandwidth was one hundred fifty-three gigabytes per second.",
        ),
        ("Use 2mg/cm^2.", "Use two milligrams per square centimeter."),
        ("Use 5–10 mg.", "Use five to ten milligrams."),
        ("Use 5-10 mg.", "Use five to ten milligrams."),
        ("The file is 19 MB.", "The file is nineteen megabytes."),
        ("The link runs at 14 Mbps.", "The link runs at fourteen megabits per second."),
        ("The road is 24 km.", "The road is twenty-four kilometers."),
        ("The draw is 26 W.", "The draw is twenty-six watts."),
        ("The width is 5 µm.", "The width is five micrometers."),
    ],
)
def test_unambiguous_measurements_include_their_units(source: str, spoken: str):
    assert normalize_for_speech(source) == spoken


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("Use <5 samples.", "Use less than five samples."),
        ("Use > 10 samples.", "Use greater than ten samples."),
        ("Use ~25 nmol/L.", "Use approximately twenty-five nanomoles per liter."),
        ("Use ~5–10%.", "Use approximately five to ten percent."),
        ("Issue #41 is fixed.", "Issue number forty-one is fixed."),
        ("PR #287 landed.", "PR number two hundred eighty-seven landed."),
        ("Goodbye ＆ more.", "Goodbye & more."),
        ("Model GPT‑5.6 stayed intact.", "Model GPT five point six stayed intact."),
        ("The rate was 1.2e-5.", "The rate was one point two times ten to the power of minus five."),
        ("There are 10^24 cases.", "There are ten to the power of twenty-four cases."),
        (
            "The constant is 6.02×10^23.",
            "The constant is six point zero two times ten to the power of twenty-three.",
        ),
        ("Use p < 1e-4.", "Use p less than one times ten to the power of minus four."),
        ("AGI and AIXI differ.", "Aye Gee Eye and Aye Eye Ex Eye differ."),
        ("Values move 0 -> 1.", "Values move zero right arrow one."),
        ("Two thirds means 2/3rds.", "Two thirds means two thirds."),
        ("About 2/3 of people agreed.", "About two thirds of people agreed."),
    ],
)
def test_symbols_with_unambiguous_prose_meanings_are_spoken(source: str, spoken: str):
    assert normalize_for_speech(source) == spoken


def test_comparison_symbols_do_not_corrupt_arrows_or_markdown_quotes():
    assert normalize_for_speech("Values move 0 -> 1.") == (
        "Values move zero right arrow one."
    )
    assert normalize_for_speech(">1) Atlas attempts.") == "one) Atlas attempts."
    assert normalize_for_speech("Open <kbd>3</kbd>.") == "Open <kbd>three</kbd>."
    assert normalize_for_speech("Write >2.log now.") == "Write >2.log now."
    assert normalize_for_speech(">65% responded.") == (
        "greater than sixty-five percent responded."
    )
    assert normalize_for_speech(">5–10% responded.") == (
        "greater than five to ten percent responded."
    )
    assert normalize_for_speech("> 65% responded.") == (
        "greater than sixty-five percent responded."
    )
    assert normalize_for_speech("> -5% responded.") == (
        "greater than minus five percent responded."
    )
    assert normalize_for_speech("> .5% responded.") == (
        "greater than zero point five percent responded."
    )
    assert normalize_for_speech("> 1e-4 failed.") == (
        "greater than one times ten to the power of minus four failed."
    )
    assert normalize_for_speech("> 0.5 failed.") == (
        "greater than zero point five failed."
    )
    assert normalize_for_speech("> -.5% failed.") == (
        "greater than minus zero point five percent failed."
    )
    assert normalize_for_speech("> 1e−4 failed.") == (
        "greater than one times ten to the power of minus four failed."
    )
    assert normalize_for_speech("Thanks for everything. <3") == (
        "Thanks for everything. <3"
    )
    assert normalize_for_speech("Thanks <3. Next.") == "Thanks <3. Next."
    assert normalize_for_speech("Hearts <3 <3.") == "Hearts <3 <3."
    assert normalize_for_speech("I <3 Python.") == "I <3 Python."
    assert normalize_for_speech("Use <3 years of data.") == (
        "Use less than three years of data."
    )


def test_scientific_notation_does_not_corrupt_code_or_identifiers():
    assert normalize_for_speech("color: #1e4;") == "color: #1e4;"
    assert normalize_for_speech("mask = 0x10^2;") == "mask = 0x10^2;"
    assert normalize_for_speech("model-1.2e5") == "model-1.2e5"
    assert normalize_for_speech("Budget $1e3.") == "Budget $1e3."
    assert normalize_for_speech("Budget $10^3.") == "Budget $10^3."


def test_oversized_scientific_notation_is_preserved():
    exponent = "9" * 400

    assert normalize_for_speech(f"Value 1e{exponent}.") == f"Value 1e{exponent}."
    assert normalize_for_speech(f"Value 10^{exponent}.") == f"Value 10^{exponent}."


def test_oversized_versions_and_fractions_are_preserved():
    digits = "9" * 400

    assert normalize_for_speech(f"Version {digits}.1.2") == f"Version {digits}.1.2"
    assert normalize_for_speech(f"Use 1/{digits}ths.") == f"Use 1/{digits}ths."


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("Chapter III explains it.", "Chapter three explains it."),
        ("World War II ended.", "World War two ended."),
        ("Henry VIII ruled.", "Henry the eighth ruled."),
        ("Elizabeth I’s motto.", "Elizabeth the first’s motto."),
        ("King James I.", "King James the first."),
        ("See Appendix V.", "See Appendix five."),
        ("World War I began.", "World War one began."),
        ("Section IV therapy is covered.", "Section four therapy is covered."),
        ("Super Bowl LVIII drew viewers.", "Super Bowl fifty-eight drew viewers."),
    ],
)
def test_roman_numerals_are_numbers_without_the_word_roman(source: str, spoken: str):
    assert normalize_for_speech(source) == spoken


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("U.S. policy changed.", "US policy changed."),
        ("The U.K. and E.U. agreed.", "The UK and EU agreed."),
        ("J. R. R. Tolkien wrote it.", "JRR Tolkien wrote it."),
        ("Use fruit, e.g. apples.", "Use fruit, for example, apples."),
        ("Use fruit, e.g., apples.", "Use fruit, for example, apples."),
        ("It is red, i.e. warm.", "It is red, that is, warm."),
        ("Dr. Smith spoke vs. Jones.", "Doctor Smith spoke versus Jones."),
        ("Use I.V. access for this.", "Use I V access for this."),
        ("Use apples, etc. Next sentence.", "Use apples, et cetera. Next sentence."),
        ("He said “U.S.” Then left.", "He said “US.” Then left."),
        ("The “U.S.” policy changed.", "The “US” policy changed."),
        ("Use “e.g.” examples.", "Use “for example,” examples."),
    ],
)
def test_dots_inside_abbreviations_do_not_become_pauses(source: str, spoken: str):
    assert normalize_for_speech(source) == spoken


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("Overview\n\nThe details follow.", "Overview.\n\nThe details follow."),
        ("Market Outlook\nPrices rose.", "Market Outlook.\nPrices rose."),
        ("Already stopped.\n\nNext section.", "Already stopped.\n\nNext section."),
        ("sentence wraps\nonto another line", "sentence wraps\nonto another line"),
    ],
)
def test_section_lines_without_punctuation_gain_a_stop(source: str, spoken: str):
    assert normalize_for_speech(source) == spoken


@pytest.mark.parametrize(
    "source",
    [
        "The timestamp 0:52:25 stayed intact.",
        "Use 192.168.1.1 at 12:30.",
        "Visit https://example.com/v1.2?q=1,200.",
        "Email test5.6@example.com.",
        "Keep malformed 12,34 unchanged.",
        "Keep malformed 12,34.56 unchanged.",
        "An IV drip is not chapter four.",
        "Start IV now and continue IV overnight.",
        "I agree that MIX is a word.",
        "Do not merge A.\n\nB. continued.",
        "Use e.g.com and a.b.py literally.",
        "Keep SHA-256 and ISO-8601 exact.",
        "ISBN 978-1-4028-9462-6 identifies it.",
        "Call 555-1234 now.",
        "Keep the invalid signs +$-1 unchanged.",
        "Keep 5-10 and 1/2 unchanged.",
    ],
)
def test_ambiguous_or_structured_notation_is_preserved(source: str):
    assert normalize_for_speech(source) == source


def test_pronoun_i_is_never_treated_as_a_roman_numeral():
    assert normalize_for_speech("I think this is ready.") == "I think this is ready."
    assert normalize_for_speech("John I think this is ready.") == (
        "John I think this is ready."
    )
    assert normalize_for_speech("John I’m ready.") == "John I’m ready."
    assert normalize_for_speech("John I, however, disagree.") == (
        "John I, however, disagree."
    )
    assert normalize_for_speech("King John I ruled.") == (
        "King John the first ruled."
    )


def test_explicit_multipart_versions_are_spoken_component_by_component():
    assert normalize_for_speech("Release v1.2.3 today.") == (
        "Release version one point two point three today."
    )
    assert normalize_for_speech("version 0.12.0") == (
        "version zero point twelve point zero"
    )


def test_normalization_is_idempotent():
    sources = [
        "Chapter III\n\nThe U.S. price was $1,200.50, up 5.6%.",
        "Use apples, etc.\n\nNext section.",
        "He said “U.S.” Then left.",
        "Keep 12,34.56 and v1.2.3 unchanged.",
        "Use I.V. access.",
        "Use `5 mg` exactly.",
    ]

    for source in sources:
        normalized = normalize_for_speech(source)
        assert normalize_for_speech(normalized) == normalized


def test_currency_normalization_preserves_surrounding_whitespace():
    assert normalize_for_speech("Cost: $1.") == "Cost: one dollar."
    assert normalize_for_speech("Cost\n\n$1") == "Cost.\n\none dollar"


def test_citation_cleanup_prevents_glued_or_spoken_terminal_markers():
    assert normalize_for_speech("Done.[4]Eventually it worked.") == (
        "Done. Eventually it worked."
    )
    assert normalize_for_speech("The protein[3]and receptor bind.") == (
        "The protein[three] and receptor bind."
    )
    assert normalize_for_speech("That was established.”¹ Next result.") == (
        "That was established.” Next result."
    )
    assert normalize_for_speech("The area is Δ⁴ units.") == "The area is Δ⁴ units."
    assert normalize_for_speech("Done.[4] Next result.") == "Done. Next result."
    assert normalize_for_speech("Done.”[4] Next result.") == "Done.” Next result."
    assert normalize_for_speech("Done.[4] “Next result.”") == "Done. “Next result.”"
    assert normalize_for_speech("Done.[4] (d) Next item.") == "Done. (d) Next item."


def test_web_formatting_artifacts_are_not_narrated():
    source = "## Core Objective\nUse the `bash` tool."

    assert normalize_for_speech(source) == "Core Objective.\nUse the `bash` tool."


def test_code_contents_and_markup_attributes_are_not_normalized():
    assert normalize_for_speech("Use `meta-llama/llama-3.3-70b-instruct`.") == (
        "Use `meta-llama/llama-3.3-70b-instruct`."
    )
    assert normalize_for_speech('Open <input value="5"> now.') == (
        'Open <input value="5"> now.'
    )
    assert normalize_for_speech('Open <input title="x > 3" value="5">.') == (
        'Open <input title="x > 3" value="5">.'
    )
    assert normalize_for_speech("Use ``code 5`` now.") == "Use ``code 5`` now."
    fenced = "```\nx = 5\n```"
    assert normalize_for_speech(fenced) == fenced
    tilde_fenced = "~~~\nx = 5\n~~~"
    assert normalize_for_speech(tilde_fenced) == tilde_fenced


def test_extraction_only_accessibility_and_footnote_artifacts_are_removed():
    source = "Details(opens in a new window). ↩︎︎\n- ^Footnote prose."

    assert normalize_for_speech(source) == "Details.\nFootnote prose."


def test_footer_like_prose_is_never_deleted_by_speech_normalization():
    source = "The post office appeared first on Main."

    assert normalize_for_speech(source) == source


def test_month_names_before_decimals_are_not_treated_as_dates():
    assert normalize_for_speech("The May 5.6 earthquake was severe.") == (
        "The May five point six earthquake was severe."
    )


def test_abbreviations_before_structured_numbers_do_not_split_sentences():
    assert chunk_text("No. 12:30 slot.", 100) == ["No. 12:30 slot."]


def test_section_punctuation_does_not_create_double_marks():
    assert normalize_for_speech("Therefore,\nNext line.") == "Therefore,\nNext line."
    assert normalize_for_speech("But—\nNext line.") == "But—\nNext line."


@pytest.mark.parametrize(
    "first",
    [
        "He said “Done.”",
        "That is explained.)",
        "First thought ended…",
    ],
)
def test_chunking_recognizes_sentence_punctuation_after_closers(first: str):
    assert chunk_text(f"{first} Next begins.", len(first)) == [first, "Next begins."]


def test_oversized_numeric_identifiers_are_left_unchanged():
    identifier = "9" * 400

    assert normalize_for_speech(f"Record {identifier} remains exact.") == (
        f"Record {identifier} remains exact."
    )
    assert normalize_for_speech(f"It was the {identifier}th run.") == (
        f"It was the {identifier}th run."
    )
    assert normalize_for_speech(f"Issue #{identifier} remains exact.") == (
        f"Issue #{identifier} remains exact."
    )


def test_structured_numeric_identifiers_are_preserved_as_whole_tokens():
    assert normalize_for_speech("SSN 123-45-6789.") == "SSN 123-45-6789."
    assert normalize_for_speech("Keep 1–2mg2 exact.") == "Keep 1–2mg2 exact."
    assert normalize_for_speech("SSN 123–45–6789.") == "SSN 123–45–6789."
    assert normalize_for_speech("SSN 123‐45‐6789.") == "SSN 123-45-6789."
    assert normalize_for_speech("SSN 123‒45‒6789.") == "SSN 123-45-6789."
    assert normalize_for_speech("CVE-2026-4890 remains open.") == (
        "CVE-2026-4890 remains open."
    )


def test_partial_unit_matches_do_not_concatenate_identifiers():
    assert normalize_for_speech("Keep 1mg2 exact.") == "Keep 1mg2 exact."
    assert normalize_for_speech("Use SAE 5W-30 oil.") == "Use SAE 5W-30 oil."
    assert normalize_for_speech("Model 5V-2 stays.") == "Model 5V-2 stays."
    assert normalize_for_speech("Use SAE 5W–30 oil.") == "Use SAE 5W–30 oil."


def test_currency_ranges_do_not_consume_percentages():
    assert normalize_for_speech("Loss was $5–10%.") == "Loss was $5–10%."
    assert normalize_for_speech("Loss was $5–$10%.") == "Loss was $5–$10%."
    assert normalize_for_speech("Loss was $5–€10%.") == "Loss was $5–€10%."
    assert normalize_for_speech("Loss -$5–-$10%.") == "Loss -$5–-$10%."
    assert normalize_for_speech("Loss $-5–$-10%.") == "Loss $-5–$-10%."
    assert normalize_for_speech("Loss $+5–$+10%.") == "Loss $+5–$+10%."


def test_currency_ranges_keep_compact_magnitudes_and_relationships():
    assert normalize_for_speech("Budget $200k-$300k.") == (
        "Budget two hundred thousand dollars to three hundred thousand dollars."
    )
    assert normalize_for_speech("Budget $30-50m.") == (
        "Budget thirty to fifty million dollars."
    )
    assert normalize_for_speech("Budget $3.1-$4.3 trillion.") == (
        "Budget three point one to four point three trillion dollars."
    )
    assert normalize_for_speech("Budget $6 million- $1 million.") == (
        "Budget six million dollars to one million dollars."
    )


def test_word_unit_ranges_are_spoken_as_ranges():
    assert normalize_for_speech("It takes 10-15 years.") == (
        "It takes ten to fifteen years."
    )


def test_additional_exact_units_are_spoken_instead_of_spelled():
    source = "30 fps, 1.5 Gbit/s, 97Wh, -70 mV, and 100 μg."
    assert normalize_for_speech(source) == (
        "thirty frames per second, one point five gigabits per second, "
        "ninety-seven watt hours, minus seventy millivolts, and one hundred micrograms."
    )


def test_abbreviation_expansion_does_not_double_external_punctuation():
    assert normalize_for_speech("Use e.g.! Next.") == "Use for example! Next."


def test_chunking_hard_splits_individual_oversized_tokens():
    assert chunk_text("9" * 81, 80) == ["9" * 80, "9"]
    assert all(len(chunk) <= 80 for chunk in chunk_text("9" * 10_000, 80))
    with pytest.raises(ValueError, match="max_chars must be positive"):
        chunk_text("abc", 0)


def test_sentence_splitting_handles_abbreviations_unicode_and_attributions():
    assert chunk_text("No. Smith continued.", 100) == ["No. Smith continued."]
    assert chunk_text("Done. Élan begins.", len("Élan begins.")) == [
        "Done.",
        "Élan begins.",
    ]
    quote = "“And what did Able think?” I said."
    assert chunk_text(quote, 100) == [quote]


class RecordingEngine:
    max_chars = 80
    default_voice = "narrator"
    sample_rate = 24000

    def __init__(self):
        self.synthesized: list[tuple[str, str | None]] = []

    def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
        self.synthesized.append((text, voice))
        return AudioChunk(np.zeros(8, dtype=np.float32), self.sample_rate)


def test_article_normalization_happens_before_chunking():
    engine = RecordingEngine()
    engine.max_chars = 35

    synthesize_article("The total was $100000 and 5.6% more.", engine, progress=False)

    assert engine.synthesized == [
        ("The total was one hundred thousand", None),
        ("dollars and five point six percent", None),
        ("more.", None),
    ]
    assert all(len(text) <= engine.max_chars for text, _ in engine.synthesized)


def test_passages_are_normalized_without_crossing_voice_boundaries():
    engine = RecordingEngine()

    synthesize_passages(
        [("Chapter II.", "narrator"), ("It cost $1.50.", "quote")],
        engine,
        progress=False,
    )

    assert engine.synthesized == [
        ("Chapter two.", "narrator"),
        ("It cost one dollar and fifty cents.", "quote"),
    ]
