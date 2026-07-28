"""Decide what the public internet may reach.

Only the podcast itself is published: the feed documents, the audio they
enclose, and the cover art. A podcast client fetches all three, so all three
have to be reachable; nothing else does. Everything else -- the library, the
phone reader, the API, health -- is answered as though it does not exist, even
with a valid token, because those are for the tailnet.

Two rules, in order. A path that is not part of the podcast is refused outright.
A path that is gets the token check, since Funnel makes it world-reachable and a
podcast client cannot send an Authorization header.

This is deliberately the second of two layers. Funnel is configured to publish
only these paths as well, so a mistake in either one alone does not expose the
library. Applied as middleware rather than per route so that a route added later
is private by default rather than by remembering.

Requests that did not arrive through Funnel -- from the tailnet, from localhost,
from the container's own health check -- are left alone.
"""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

#: Injected by tailscaled on Funnel requests. Verified unspoofable: a client
#: setting, clearing or contradicting it has the value overwritten before the
#: request reaches us, as are X-Forwarded-* headers.
FUNNEL_HEADER = "tailscale-funnel-request"

LIBRARY_TOKEN_COOKIE = "vocast_library_token"

#: Exactly what a podcast client needs, and nothing else. Prefixes end in "/" so
#: that "/audio/" cannot be satisfied by a path merely starting with those
#: letters. Paths are matched absolutely: the app serves its routes at the root.
PUBLISHED_EXACT = frozenset({"/feed.xml", "/cover.jpg"})
PUBLISHED_PREFIXES = ("/feeds/", "/audio/")


def is_public_request(request: Request) -> bool:
    """Whether this request came from the internet rather than the tailnet."""
    return FUNNEL_HEADER in request.headers


def supplied_token(request: Request) -> str | None:
    """The token from the query string, or the cookie set after a first visit."""
    return request.query_params.get("token") or request.cookies.get(
        LIBRARY_TOKEN_COOKIE
    )


def is_published(path: str) -> bool:
    """Whether this path is part of the podcast, and so allowed from outside."""
    return path in PUBLISHED_EXACT or path.startswith(PUBLISHED_PREFIXES)


def public_access_denied(expected: str, request: Request) -> Response | None:
    """The response to send an internet request, or None to let it through."""
    if not is_public_request(request):
        return None
    if not is_published(request.url.path):
        # 404 rather than 403: a refusal would confirm that a library exists
        # here, and the point is that from the internet it does not.
        return PlainTextResponse("not found", status_code=404)
    if not expected:
        return None
    supplied = supplied_token(request)
    if supplied and secrets.compare_digest(supplied, expected):
        return None
    return PlainTextResponse(
        "a valid ?token= is required over the public internet",
        status_code=401,
    )
