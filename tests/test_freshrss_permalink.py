from vocast.ingest.adapters.freshrss_api import _permalink_href

SITE = "https://daringfireball.net/"

DF_BODY = """
<p>Some commentary about the thing.</p>
<p>(Previously discussed in
   <a href="https://daringfireball.net/linked/2021/05/09/magic-lasso">May 2021</a>.)</p>
<div><a title="Permanent link to 'A Post'"
        href="https://daringfireball.net/linked/2026/07/24/a-post"> &#9733; </a></div>
"""


def test_the_permalink_is_preferred_over_an_earlier_self_link():
    """A link post can cite its own site mid-text; the permalink closes the item,
    so the last marked anchor is the one that identifies the post."""
    assert (
        _permalink_href(DF_BODY, SITE)
        == "https://daringfireball.net/linked/2026/07/24/a-post"
    )


def test_an_outbound_link_is_never_taken_as_the_permalink():
    body = '<p><a href="https://example.com/article">Permalink</a></p>'

    assert _permalink_href(body, SITE) is None


def test_an_unmarked_self_link_is_not_a_permalink():
    """Six Colors ends its posts with a plain outbound "continue reading" link
    and no permalink; guessing would send the listener somewhere unrelated."""
    body = '<p><a href="https://daringfireball.net/2026/07/other">Another post</a></p>'

    assert _permalink_href(body, SITE) is None


def test_rel_bookmark_is_recognised():
    body = '<a rel="bookmark" href="https://daringfireball.net/linked/2026/07/x">#</a>'

    assert _permalink_href(body, SITE) == "https://daringfireball.net/linked/2026/07/x"


def test_a_body_without_links_yields_nothing():
    assert _permalink_href("<p>Just prose.</p>", SITE) is None


def test_protocol_relative_and_javascript_hrefs_are_ignored():
    body = '<a title="Permalink" href="javascript:void(0)">&#9733;</a>'

    assert _permalink_href(body, SITE) is None
