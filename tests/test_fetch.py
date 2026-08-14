import json

from lxml import html

from vocast import fetch
from vocast.fetch import _json_text_length, repair_orphaned_text_blocks


def test_bare_prose_between_article_blocks_is_restored_to_paragraphs():
    first = "The opening argument is substantial. " * 14
    second = "The conclusion should also be narrated. " * 14
    document = (
        "<html><body><main><h2>Finding</h2>"
        f"{first}<a href='https://example.com/source'>the source</a> confirms it.\n\n"
        "<blockquote>The quoted evidence.</blockquote>"
        f"{second}</main></body></html>"
    )

    repaired = html.fromstring(repair_orphaned_text_blocks(document))
    main = repaired.xpath("//main")[0]

    assert [child.tag for child in main] == ["h2", "p", "blockquote", "p"]
    assert "the source confirms it" in " ".join(main[1].text_content().split())
    assert first.strip() in main[1].text_content()
    assert second.strip() in main[3].text_content()


def test_well_formed_html_is_not_rewritten():
    document = "<html><body><main><p>Already a paragraph.</p></main></body></html>"

    assert repair_orphaned_text_blocks(document) == document


def test_extraction_length_handles_missing_and_invalid_results():
    assert _json_text_length('{"text": "complete article"}') == 16
    assert _json_text_length(None) == 0
    assert _json_text_length("not json") == 0


def test_article_extraction_uses_repaired_prose_only_when_it_recovers_more(
    monkeypatch,
):
    prose = "Text a browser displays but the extractor cannot see. " * 20
    document = f"<main><h2>Heading</h2>{prose}<blockquote>Quote</blockquote></main>"
    structured_inputs = []

    def extract(candidate, **options):
        repaired = f"<p>{prose}" in candidate
        if options.get("output_format") == "xml":
            structured_inputs.append(candidate)
            return "<doc><main><quote>Quote</quote></main></doc>"
        text = prose if repaired else "Short surviving text."
        return json.dumps({"title": "Heading", "text": text})

    monkeypatch.setattr(fetch.trafilatura, "extract", extract)

    _, text, _, quotes = fetch.fetch_article_parts(
        "https://example.com/post", html_fetcher=lambda _: document
    )

    assert text == prose.strip()
    assert quotes == ["Quote"]
    assert f"<p>{prose}" in structured_inputs[0]


def test_article_extraction_keeps_the_original_when_repair_is_not_an_improvement(
    monkeypatch,
):
    prose = "Bare text that might not all belong to the article. " * 20
    document = f"<main><h2>Heading</h2>{prose}<blockquote>Quote</blockquote></main>"

    def extract(candidate, **options):
        if options.get("output_format") == "xml":
            return "<doc><main /></doc>"
        repaired = f"<p>{prose}" in candidate
        text = "Less useful." if repaired else "The complete original extraction."
        return json.dumps({"title": "Heading", "text": text})

    monkeypatch.setattr(fetch.trafilatura, "extract", extract)

    _, text, _, _ = fetch.fetch_article_parts(
        "https://example.com/post", html_fetcher=lambda _: document
    )

    assert text == "The complete original extraction."
