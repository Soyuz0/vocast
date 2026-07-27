from vocast.ingest.urlfix import HOST_REWRITES, corrected_url


def test_a_broken_host_is_replaced_with_its_working_mirror():
    """vitalik.ca publishes no A or AAAA record, so no amount of retrying helps;
    the mirror serves the identical path."""
    assert (
        corrected_url("https://vitalik.ca/general/2026/06/29/obfuscation1.html")
        == "https://vitalik.eth.limo/general/2026/06/29/obfuscation1.html"
    )


def test_the_www_form_is_rewritten_too():
    assert (
        corrected_url("https://www.vitalik.ca/x.html")
        == "https://vitalik.eth.limo/x.html"
    )


def test_the_path_query_and_fragment_survive():
    """Only the host is wrong; everything identifying the article must be kept."""
    rewritten = corrected_url("https://vitalik.ca/a/b.html?x=1&y=2#part")

    assert rewritten == "https://vitalik.eth.limo/a/b.html?x=1&y=2#part"


def test_an_unlisted_host_is_untouched():
    assert corrected_url("https://example.com/a") == "https://example.com/a"


def test_the_case_of_the_host_does_not_matter():
    assert corrected_url("https://VITALIK.CA/a") == "https://vitalik.eth.limo/a"


def test_values_that_are_not_urls_pass_through():
    """This runs over every ingested entry, so it must never raise."""
    assert corrected_url(None) is None
    assert corrected_url("") == ""
    assert corrected_url("not a url") == "not a url"


def test_rewrite_targets_are_not_themselves_rewritten():
    """A mapping whose target is also a key would rewrite in circles."""
    for target in HOST_REWRITES.values():
        assert target not in HOST_REWRITES


def test_the_rewrite_reaches_the_ingested_entry():
    """Applied at ingestion rather than only when fetching, so the feed link and
    the library's Original link point at the URL that actually works."""
    from vocast.ingest.adapters.rss import GenericRSSAdapter

    document = """<?xml version="1.0"?><rss version="2.0"><channel>
      <title>Vitalik</title>
      <item>
        <title>Obfuscation</title>
        <link>https://vitalik.ca/general/2026/06/29/obfuscation1.html</link>
        <guid>vitalik-obfuscation</guid>
      </item>
    </channel></rss>"""

    entries = GenericRSSAdapter(
        _source(), fetcher=lambda url, **kwargs: _Response(document)
    ).fetch_entries()

    assert entries[0].article_url == (
        "https://vitalik.eth.limo/general/2026/06/29/obfuscation1.html"
    )


class _Response:
    """The adapter reads raw bytes off the response, not decoded text."""

    def __init__(self, body: str) -> None:
        self.body = body.encode()
        self.charset = "utf-8"

    def text(self) -> str:
        return self.body.decode()


def _source():
    from vocast.ingest.models import Source
    from vocast.ingest.timeutils import utcnow

    return Source(
        id=1,
        name="Vitalik",
        kind="rss",
        url="https://vitalik.ca/feed.xml",
        enabled=True,
        poll_interval_minutes=60,
        config={},
        last_checked_at=None,
        last_success_at=None,
        last_error=None,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
