"""URL policy, size caps, redirect revalidation, and credential redaction."""

from __future__ import annotations

import io
import urllib.error
from email.message import Message

import pytest

from vocast.ingest import nethttp
from vocast.ingest.nethttp import (
    BlockedURLError,
    FetchError,
    FetchPolicy,
    fetch,
    redact_headers,
    validate_url,
)


class _FakeHTTPResponse:
    """Enough of http.client.HTTPResponse for the fetch path."""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._stream = io.BytesIO(body)
        message = Message()
        for key, value in (headers or {}).items():
            message[key] = value
        self.headers = message

    def read(self, amount: int | None = None) -> bytes:
        return self._stream.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch):
    """Resolve every hostname to a public address unless a test says otherwise."""
    monkeypatch.setattr(
        nethttp,
        "_resolve",
        lambda host: [nethttp.ipaddress.ip_address("93.184.216.34")],
    )


def _install_opener(monkeypatch: pytest.MonkeyPatch, handler):
    """Route opener.open through a callable taking (url, request)."""
    calls: list = []

    class FakeOpener:
        def open(self, request, timeout=None):
            calls.append(request)
            return handler(request.full_url, request)

    monkeypatch.setattr(nethttp.urllib.request, "build_opener", lambda *a: FakeOpener())
    return calls


# --- URL policy ------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://example.com/f", "http://example.com/f"])
def test_http_urls_are_allowed(url: str):
    assert validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/f",
        "gopher://example.com/f",
        "javascript:alert(1)",
    ],
)
def test_non_http_schemes_are_blocked(url: str):
    with pytest.raises(BlockedURLError, match="scheme"):
        validate_url(url)


def test_url_without_a_host_is_blocked():
    with pytest.raises(BlockedURLError):
        validate_url("http:///nohost")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.10",
        "172.16.0.1",
        "169.254.169.254",  # cloud metadata
        "::1",
        "fe80::1",
        "0.0.0.0",
    ],
)
def test_private_and_metadata_addresses_are_blocked(
    address: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        nethttp, "_resolve", lambda host: [nethttp.ipaddress.ip_address(address)]
    )
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(BlockedURLError, match="private"):
        validate_url(f"http://{host}/")


def test_ipv4_mapped_metadata_address_is_blocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        nethttp,
        "_resolve",
        lambda host: [nethttp.ipaddress.ip_address("::ffff:169.254.169.254")],
    )
    with pytest.raises(BlockedURLError):
        validate_url("http://metadata.example/")


def test_host_resolving_to_both_public_and_private_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        nethttp,
        "_resolve",
        lambda host: [
            nethttp.ipaddress.ip_address("93.184.216.34"),
            nethttp.ipaddress.ip_address("127.0.0.1"),
        ],
    )
    with pytest.raises(BlockedURLError):
        validate_url("http://sneaky.example/")


def test_private_addresses_are_allowed_when_opted_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        nethttp, "_resolve", lambda host: [nethttp.ipaddress.ip_address("192.168.1.10")]
    )
    assert validate_url("http://nas.local/feed", allow_private=True)


# --- size cap --------------------------------------------------------------


def test_oversized_declared_length_is_refused(monkeypatch: pytest.MonkeyPatch):
    _install_opener(
        monkeypatch,
        lambda url, req: _FakeHTTPResponse(b"x", headers={"Content-Length": "999999"}),
    )
    with pytest.raises(FetchError, match="over the"):
        fetch("https://example.com/big", policy=FetchPolicy(max_bytes=100))


def test_oversized_body_is_refused_even_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_opener(monkeypatch, lambda url, req: _FakeHTTPResponse(b"x" * 500))
    with pytest.raises(FetchError, match="exceeded"):
        fetch("https://example.com/big", policy=FetchPolicy(max_bytes=100))


def test_body_at_the_limit_is_accepted(monkeypatch: pytest.MonkeyPatch):
    _install_opener(monkeypatch, lambda url, req: _FakeHTTPResponse(b"x" * 100))
    response = fetch("https://example.com/ok", policy=FetchPolicy(max_bytes=100))
    assert len(response.body) == 100


# --- redirects -------------------------------------------------------------


def test_redirect_is_followed(monkeypatch: pytest.MonkeyPatch):
    def handler(url, request):
        if url.endswith("/start"):
            raise urllib.error.HTTPError(
                url, 302, "Found", _headers(Location="/final"), None
            )
        return _FakeHTTPResponse(b"arrived")

    _install_opener(monkeypatch, handler)
    assert fetch("https://example.com/start").body == b"arrived"


def test_redirect_to_a_private_address_is_blocked(monkeypatch: pytest.MonkeyPatch):
    def handler(url, request):
        raise urllib.error.HTTPError(
            url, 302, "Found", _headers(Location="http://169.254.169.254/latest"), None
        )

    _install_opener(monkeypatch, handler)

    def resolve(host):
        if host == "example.com":
            return [nethttp.ipaddress.ip_address("93.184.216.34")]
        return [nethttp.ipaddress.ip_address("169.254.169.254")]

    monkeypatch.setattr(nethttp, "_resolve", resolve)

    with pytest.raises(BlockedURLError):
        fetch("https://example.com/start")


def test_redirect_loop_is_bounded(monkeypatch: pytest.MonkeyPatch):
    def handler(url, request):
        raise urllib.error.HTTPError(
            url, 302, "Found", _headers(Location="/again"), None
        )

    _install_opener(monkeypatch, handler)
    with pytest.raises(FetchError, match="too many redirects"):
        fetch("https://example.com/loop")


# --- errors ----------------------------------------------------------------


def test_http_error_status_becomes_a_fetch_error(monkeypatch: pytest.MonkeyPatch):
    def handler(url, request):
        raise urllib.error.HTTPError(url, 404, "Not Found", _headers(), None)

    _install_opener(monkeypatch, handler)
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch("https://example.com/missing")


def test_network_error_becomes_a_fetch_error(monkeypatch: pytest.MonkeyPatch):
    def handler(url, request):
        raise urllib.error.URLError("connection refused")

    _install_opener(monkeypatch, handler)
    with pytest.raises(FetchError, match="network error"):
        fetch("https://example.com/down")


def test_configured_headers_reach_the_request(monkeypatch: pytest.MonkeyPatch):
    calls = _install_opener(monkeypatch, lambda url, req: _FakeHTTPResponse(b""))
    fetch("https://example.com/f", headers={"Authorization": "Basic zzz"})

    assert calls[0].get_header("Authorization") == "Basic zzz"


def test_gzip_body_is_decompressed(monkeypatch: pytest.MonkeyPatch):
    import gzip

    _install_opener(
        monkeypatch,
        lambda url, req: _FakeHTTPResponse(
            gzip.compress(b"hello"), headers={"Content-Encoding": "gzip"}
        ),
    )
    assert fetch("https://example.com/z").body == b"hello"


# --- redaction -------------------------------------------------------------


@pytest.mark.parametrize(
    "header", ["Authorization", "authorization", "Cookie", "Proxy-Authorization"]
)
def test_credentials_are_redacted_for_logging(header: str):
    assert redact_headers({header: "secret-value"})[header] == "<redacted>"


def test_redaction_keeps_harmless_headers():
    assert redact_headers({"User-Agent": "vocast"}) == {"User-Agent": "vocast"}


def _headers(**fields: str) -> Message:
    message = Message()
    for key, value in fields.items():
        message[key] = value
    return message


# --- POST ------------------------------------------------------------------


def test_data_makes_it_a_post_and_is_sent(monkeypatch: pytest.MonkeyPatch):
    calls = _install_opener(monkeypatch, lambda url, req: _FakeHTTPResponse(b"ok"))
    fetch("https://example.com/login", data=b"Email=a&Passwd=b")

    assert calls[0].data == b"Email=a&Passwd=b"
    assert calls[0].get_method() == "POST"


def test_redirect_on_a_post_is_refused(monkeypatch: pytest.MonkeyPatch):
    """Following it would re-send credentials to another location."""

    def handler(url, request):
        raise urllib.error.HTTPError(
            url, 302, "Found", _headers(Location="https://evil.example/"), None
        )

    _install_opener(monkeypatch, handler)
    with pytest.raises(FetchError, match="refusing to follow a redirect on a POST"):
        fetch("https://example.com/login", data=b"Passwd=secret")


def test_post_credentials_are_not_echoed_in_the_error(monkeypatch: pytest.MonkeyPatch):
    def handler(url, request):
        raise urllib.error.HTTPError(
            url, 302, "Found", _headers(Location="/elsewhere"), None
        )

    _install_opener(monkeypatch, handler)
    with pytest.raises(FetchError) as excinfo:
        fetch("https://example.com/login", data=b"Passwd=hunter2")
    assert "hunter2" not in str(excinfo.value)
