from vocast.quotes import Passage, quotes_from_xml, split_quoted

XML = """
<doc><main>
  <p>Ben Thompson, at Stratechery:</p>
  <quote><p>Open weight models must follow the terms of service.</p></quote>
  <p>To be clear, the leading labs disagree.</p>
</main></doc>
"""


def test_quotes_are_read_out_of_the_structured_output():
    assert quotes_from_xml(XML) == [
        "Open weight models must follow the terms of service."
    ]


def test_malformed_structure_yields_no_quotes():
    """Narration must not fail because the second extractor pass produced
    something unparseable."""
    assert quotes_from_xml("<doc><main><p>unclosed") == []
    assert quotes_from_xml(None) == []


def test_text_is_split_around_the_quote():
    text = (
        "Ben Thompson, at Stratechery:\n"
        "Open weight models must follow the terms of service.\n"
        "To be clear, the leading labs disagree."
    )

    assert split_quoted(text, quotes_from_xml(XML)) == [
        Passage("Ben Thompson, at Stratechery:", quoted=False),
        Passage("Open weight models must follow the terms of service.", quoted=True),
        Passage("To be clear, the leading labs disagree.", quoted=False),
    ]


def test_no_word_is_lost_or_duplicated_by_splitting():
    """The passages are what gets narrated, so together they must still be the
    article."""
    text = "Intro here.\nQuoted words.\nMiddle.\nSecond quote.\nEnd."
    quotes = ["Quoted words.", "Second quote."]

    joined = " ".join(p.text for p in split_quoted(text, quotes))

    assert joined.split() == text.split()


def test_line_breaks_inside_a_quote_do_not_prevent_matching():
    """The two extractor passes disagree about line breaks while agreeing on the
    words, so matching is done on normalised whitespace."""
    text = "Lead in:\nFirst line of quote\nsecond line of quote\nAfterwards."

    passages = split_quoted(text, ["First line of quote second line of quote"])

    assert [p.quoted for p in passages] == [False, True, False]
    assert passages[1].text == "First line of quote\nsecond line of quote"


def test_a_quote_that_cannot_be_located_is_ignored():
    """Better to narrate in one voice than to mangle the text around a guess."""
    text = "Just the article body."

    assert split_quoted(text, ["something that is not present"]) == [
        Passage("Just the article body.", quoted=False)
    ]


def test_an_article_without_quotes_is_a_single_passage():
    assert split_quoted("Plain article.", []) == [
        Passage("Plain article.", quoted=False)
    ]


def test_empty_text_yields_nothing():
    assert split_quoted("   ", ["x"]) == []
