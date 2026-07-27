"""Marking articles read in FreshRSS, via the Google Reader API.

The only direction of sync that is expressible: a podcast client never reports
playback position, so "listened" cannot be observed. Downloading the audio is
the closest available signal, and this is what acts on it.

Kept apart from the source adapters because it writes. Everything else in the
ingestion path only reads from FreshRSS.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any

from .logs import get_logger, kv
from .models import Source
from .nethttp import FetchError, FetchPolicy, fetch

log = get_logger("freshrss-write")

READ_STATE = "user/-/state/com.google/read"


class FreshRSSWriteError(Exception):
    """A write to FreshRSS failed. Never fatal to anything else."""


class FreshRSSWriter:
    """Marks articles read. One instance per source.

    The write token is fetched once and reused, and re-fetched if it is
    rejected: FreshRSS invalidates it on password change or session expiry.
    """

    def __init__(
        self,
        source: Source,
        *,
        policy: FetchPolicy | None = None,
        fetcher: Callable[..., Any] = fetch,
    ) -> None:
        self._source = source
        self._fetcher = fetcher
        config = source.config or {}
        base = policy or FetchPolicy()
        self._policy = FetchPolicy(
            timeout=float(config.get("timeout_seconds", 30.0)),
            max_bytes=1024 * 1024,
            allow_private=bool(config.get("allow_private_urls", base.allow_private)),
            user_agent=base.user_agent,
        )
        self._auth: str | None = None
        self._token: str | None = None

    def mark_read(self, external_guid: str) -> None:
        """Mark one article read. Raises FreshRSSWriteError on failure."""
        try:
            self._mark(external_guid, retry_on_stale_token=True)
        except FetchError as exc:
            raise FreshRSSWriteError(str(exc)) from exc

    def mark_unread(self, external_guid: str) -> None:
        """Remove the read marker. Raises FreshRSSWriteError on failure.

        Undoing a download has to reach upstream: read reconciliation ignores
        anything no longer in the unread stream, so an entry left read in
        FreshRSS would be dropped again on the next full poll.
        """
        try:
            self._mark(external_guid, retry_on_stale_token=True, read=False)
        except FetchError as exc:
            raise FreshRSSWriteError(str(exc)) from exc

    # -- internals ---------------------------------------------------------

    def _mark(
        self, guid: str, *, retry_on_stale_token: bool, read: bool = True
    ) -> None:
        # "a" adds the tag, "r" removes it; the endpoint is otherwise identical.
        body = urllib.parse.urlencode(
            {"a" if read else "r": READ_STATE, "i": guid, "T": self._write_token()}
        ).encode()
        url = f"{self._base()}/api/greader.php/reader/api/0/edit-tag"
        try:
            response = self._fetcher(
                url,
                policy=self._policy,
                headers={
                    "Authorization": f"GoogleLogin auth={self._client_auth()}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=body,
            )
        except FetchError:
            if not retry_on_stale_token:
                raise
            # A rejected token is indistinguishable from other 4xx here, so
            # discard both and try once with fresh credentials.
            self._auth = self._token = None
            self._mark(guid, retry_on_stale_token=False, read=read)
            return

        text = response.text().strip()
        if text.upper() != "OK":
            raise FreshRSSWriteError(
                f"FreshRSS did not accept the read marker change for {guid}: "
                f"{text[:120]!r}"
            )

    def _client_auth(self) -> str:
        if self._auth:
            return self._auth
        config = self._source.config or {}
        username = config.get("username")
        password = config.get("api_password") or config.get("password")
        if not username or not password:
            raise FreshRSSWriteError(
                f"source {self._source.id} needs `username` and `api_password` to "
                "mark articles read"
            )
        response = self._fetcher(
            f"{self._base()}/api/greader.php/accounts/ClientLogin",
            policy=self._policy,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=urllib.parse.urlencode(
                {"Email": str(username), "Passwd": str(password)}
            ).encode(),
        )
        for line in response.text().splitlines():
            if line.startswith("Auth="):
                self._auth = line[len("Auth=") :].strip()
                return self._auth
        raise FreshRSSWriteError("FreshRSS ClientLogin returned no Auth token")

    def _write_token(self) -> str:
        if self._token:
            return self._token
        response = self._fetcher(
            f"{self._base()}/api/greader.php/reader/api/0/token",
            policy=self._policy,
            headers={"Authorization": f"GoogleLogin auth={self._client_auth()}"},
        )
        token = response.text().strip()
        if not token:
            raise FreshRSSWriteError("FreshRSS returned an empty write token")
        self._token = token
        return token

    def _base(self) -> str:
        return self._source.url.rstrip("/")


def mark_read_in_background(
    writer: FreshRSSWriter, entry_id: int, guid: str, on_success: Callable[[int], None]
) -> None:
    """Mark read without blocking the caller.

    Called from the request that served the audio, so it must not add latency,
    and a FreshRSS outage must not turn into a failed download.
    """
    import threading

    def run() -> None:
        try:
            writer.mark_read(guid)
        except (FreshRSSWriteError, FetchError) as exc:
            log.warning(
                "could not mark article read upstream %s",
                kv(entry_id=entry_id, error=exc),
            )
            return
        on_success(entry_id)
        log.info("marked read in FreshRSS %s", kv(entry_id=entry_id))

    threading.Thread(target=run, name=f"mark-read-{entry_id}", daemon=True).start()
