"""Rewriting article URLs whose published host does not work.

A publisher can advertise a hostname that cannot be fetched at all, which is
not something retries or a different user agent can solve. Where a working
mirror serves the same paths, rewriting the host recovers the article instead of
failing it.

Kept as an explicit, tiny map rather than a heuristic. Guessing at replacement
hosts would risk narrating a different site's article under this one's title,
and each entry here is a specific, verified case.
"""

from __future__ import annotations

import urllib.parse

#: Broken host -> working host serving the same paths.
#:
#: vitalik.ca publishes no A or AAAA record for its apex, so every article URL
#: from that feed is unresolvable; www.vitalik.ca resolves but times out. The
#: IPFS gateway mirror serves the identical path structure. The feed itself only
#: exists under the .ca name, so the URLs cannot simply be re-subscribed.
HOST_REWRITES: dict[str, str] = {
    "vitalik.ca": "vitalik.eth.limo",
    "www.vitalik.ca": "vitalik.eth.limo",
}


def corrected_url(url: str | None) -> str | None:
    """The URL to use in place of one whose host is known to be broken.

    Returns the input unchanged when there is nothing to rewrite, including for
    a value that is not a URL, so this is safe to apply to every entry.
    """
    if not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    replacement = HOST_REWRITES.get(host)
    if not replacement:
        return url
    netloc = replacement if parts.port is None else f"{replacement}:{parts.port}"
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
