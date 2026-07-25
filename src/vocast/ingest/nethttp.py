"""Guarded HTTP fetching for untrusted, user-supplied URLs.

Feed and article URLs come from whoever configures the service, and articles
themselves can redirect anywhere. This module is the single place where those
requests are made, so the safety rules live in one auditable spot:

* only http/https,
* a hard cap on response size, so a huge file cannot exhaust memory,
* connect/read timeouts on every request,
* redirects are re-validated, so a public URL cannot bounce to localhost,
* link-local, loopback, and private ranges are refused by default, which
  blocks cloud metadata endpoints such as 169.254.169.254.

Blocking private ranges is a *default*, not a guarantee: see the SSRF notes in
the README. Homelab users often legitimately need to reach a FreshRSS instance
on their LAN, so `allow_private` exists as an explicit opt-in.
"""

from __future__ import annotations

import gzip
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field

DEFAULT_USER_AGENT = "vocast/rss (+https://github.com/cnrmurphy/vocast)"

MAX_REDIRECTS = 5

# Headers whose values must never reach a log line or an error message.
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})


class FetchError(Exception):
    """A request failed. The message is safe to log and to show a user."""


class BlockedURLError(FetchError):
    """The URL was refused by policy before any request was made."""


@dataclass(frozen=True)
class FetchPolicy:
    timeout: float = 30.0
    max_bytes: int = 10 * 1024 * 1024
    allow_private: bool = False
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    body: bytes
    charset: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        return self.body.decode(self.charset or "utf-8", errors="replace")


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Replace credential values so headers can be safely logged."""
    return {
        key: ("<redacted>" if key.lower() in _SECRET_HEADERS else value)
        for key, value in headers.items()
    }


def validate_url(url: str, *, allow_private: bool = False) -> str:
    """Return the URL if it is safe to request, else raise BlockedURLError."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise BlockedURLError(f"malformed URL: {url!r}") from exc

    if parts.scheme not in ("http", "https"):
        raise BlockedURLError(
            f"unsupported URL scheme {parts.scheme!r} (only http and https are allowed)"
        )
    if not parts.hostname:
        raise BlockedURLError(f"URL has no host: {url!r}")
    if not allow_private:
        _reject_private_host(parts.hostname)
    return url


def _reject_private_host(hostname: str) -> None:
    for address in _resolve(hostname):
        if _is_private(address):
            raise BlockedURLError(
                f"refusing to connect to {hostname} ({address}): private, loopback, "
                "and link-local addresses are blocked. Set allow_private_urls: true "
                "to permit LAN sources."
            )


def _resolve(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to every address it maps to.

    Every result is checked, not just the first, so a host that resolves to
    both a public and a loopback address is still refused.
    """
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return [literal]

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise FetchError(f"could not resolve {hostname}: {exc}") from exc

    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise FetchError(f"could not resolve {hostname}")
    return addresses


def _is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_private or address.is_loopback or address.is_link_local:
        return True
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return True
    # ::ffff:169.254.169.254 and friends would otherwise slip past the checks
    # above, which only inspect the IPv6 form.
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and _is_private(mapped))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress urllib's automatic redirects so each hop can be validated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(
    url: str,
    *,
    policy: FetchPolicy | None = None,
    headers: dict[str, str] | None = None,
    accept: str | None = None,
    data: bytes | None = None,
) -> Response:
    """Fetch a URL, following redirects manually with per-hop validation.

    Passing `data` makes it a POST, which the FreshRSS API needs for its login
    exchange. The body is not re-sent across redirects: a 30x on a POST is
    either a protocol error or a downgrade attempt, so it is refused.
    """
    rules = policy or FetchPolicy()
    request_headers = {"User-Agent": rules.user_agent}
    if accept:
        request_headers["Accept"] = accept
    if headers:
        request_headers.update(headers)

    opener = urllib.request.build_opener(_NoRedirect)
    current = validate_url(url, allow_private=rules.allow_private)

    for _ in range(MAX_REDIRECTS + 1):
        request = urllib.request.Request(current, headers=request_headers, data=data)
        try:
            with opener.open(request, timeout=rules.timeout) as response:
                return _read_response(current, response, rules)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                if data is not None:
                    raise FetchError(
                        f"refusing to follow a redirect on a POST to {current} "
                        f"(HTTP {exc.code}); credentials would be re-sent to "
                        "another location"
                    ) from exc
                current = validate_url(
                    urllib.parse.urljoin(current, location),
                    allow_private=rules.allow_private,
                )
                continue
            raise FetchError(f"HTTP {exc.code} {exc.reason} from {current}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise FetchError(f"network error fetching {current}: {reason}") from exc
        except TimeoutError as exc:
            raise FetchError(f"timed out fetching {current}") from exc

    raise FetchError(f"too many redirects fetching {url}")


def _read_response(url: str, response, rules: FetchPolicy) -> Response:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > rules.max_bytes:
        raise FetchError(
            f"{url} reports {int(declared)} bytes, over the "
            f"{rules.max_bytes} byte limit"
        )

    # Read one byte past the cap so an oversized body that lies about (or
    # omits) Content-Length is still caught.
    raw = response.read(rules.max_bytes + 1)
    if len(raw) > rules.max_bytes:
        raise FetchError(f"{url} exceeded the {rules.max_bytes} byte limit")

    encoding = (response.headers.get("Content-Encoding") or "").lower()
    raw = _decompress(raw, encoding)
    return Response(
        url=url,
        status=response.status,
        body=raw,
        charset=response.headers.get_content_charset(),
        headers={k.lower(): v for k, v in response.headers.items()},
    )


def _decompress(raw: bytes, encoding: str) -> bytes:
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw)
    except (OSError, zlib.error) as exc:
        raise FetchError(f"could not decode {encoding} response: {exc}") from exc
    return raw


def basic_auth_header(username: str, password: str) -> str:
    import base64

    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"
