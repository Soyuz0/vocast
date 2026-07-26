"""Require the feed token on anything reachable from the public internet.

Tailscale Funnel publishes *every* path, not a chosen subset, so a route cannot
be treated as private just because it is normally used over the tailnet. Rather
than remembering to guard each new route, this closes the whole surface at once:
if a request arrived through Funnel, it needs the token.

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


def is_public_request(request: Request) -> bool:
    """Whether this request came from the internet rather than the tailnet."""
    return FUNNEL_HEADER in request.headers


def supplied_token(request: Request) -> str | None:
    """The token from the query string, or the cookie set after a first visit."""
    return request.query_params.get("token") or request.cookies.get(
        LIBRARY_TOKEN_COOKIE
    )


def public_access_denied(expected: str, request: Request) -> Response | None:
    """A 401 when an internet request lacks the token, else None."""
    if not expected or not is_public_request(request):
        return None
    supplied = supplied_token(request)
    if supplied and secrets.compare_digest(supplied, expected):
        return None
    return PlainTextResponse(
        "a valid ?token= is required over the public internet",
        status_code=401,
    )
