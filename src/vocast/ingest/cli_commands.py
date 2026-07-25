"""argparse handlers for the ingestion subcommands.

Kept out of `vocast.cli` so the existing command surface stays readable, and
so importing these never drags in the TTS engine.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .adapters import supported_kinds
from .config import ConfigError, load_config
from .context import AppContext
from .logs import configure_logging
from .models import EntryStatus
from .nethttp import BlockedURLError, validate_url
from .repository import DuplicateSourceError


def build_context(args: argparse.Namespace) -> AppContext:
    """Load config (honoring --config/--db overrides) and open the database."""
    config = load_config(getattr(args, "config", None))
    if getattr(args, "db", None):
        from dataclasses import replace
        from pathlib import Path

        config = replace(
            config, database=replace(config.database, path=Path(args.db).expanduser())
        )
    configure_logging(config.log_level)
    context = AppContext.create(config)
    context.sync_configured_sources()
    return context


# --- source management -----------------------------------------------------


def cmd_source_add(args: argparse.Namespace) -> int:
    kinds = supported_kinds()
    if args.kind not in kinds:
        print(
            f"error: unknown source kind {args.kind!r} (choose from {', '.join(kinds)})",
            file=sys.stderr,
        )
        return 1

    context = build_context(args)
    try:
        validate_url(args.url, allow_private=context.config.allow_private_urls)
    except BlockedURLError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    options = _parse_headers(args.header)
    if args.username and args.password:
        options["username"] = args.username
        options["password"] = args.password
    if args.allow_private:
        options["allow_private_urls"] = True

    interval = args.interval or context.config.polling.default_interval_minutes
    try:
        source = context.sources.add(
            name=args.name,
            kind=args.kind,
            url=args.url,
            enabled=not args.disabled,
            poll_interval_minutes=interval,
            config=options,
        )
    except DuplicateSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"added source {source.id}: {source.name} ({source.kind})")
    print(f"  url:      {source.url}")
    print(f"  interval: every {source.poll_interval_minutes} min")
    print(f"  feed:     /feeds/source/{source.id}.xml")
    return 0


def cmd_source_list(args: argparse.Namespace) -> int:
    context = build_context(args)
    sources = context.sources.list()
    if not sources:
        print("(no sources)")
        print("add one with: vocast source add --name NAME --url URL")
        return 0

    counts = _entry_counts_by_source(context)
    rows = [
        (
            str(s.id),
            s.name,
            s.kind,
            "yes" if s.enabled else "no",
            f"{s.poll_interval_minutes}m",
            str(counts.get(s.id, 0)),
            _relative(s.last_success_at),
            "yes" if s.last_error else "",
        )
        for s in sources
    ]
    headers = ("ID", "NAME", "KIND", "ON", "EVERY", "ENTRIES", "LAST OK", "ERR")
    _print_table(headers, rows)

    failing = [s for s in sources if s.last_error]
    if failing:
        print()
        for source in failing:
            print(f"! source {source.id} ({source.name}): {source.last_error}")
    return 0


def cmd_source_enable(args: argparse.Namespace) -> int:
    return _set_enabled(args, enabled=True)


def cmd_source_disable(args: argparse.Namespace) -> int:
    return _set_enabled(args, enabled=False)


def _set_enabled(args: argparse.Namespace, *, enabled: bool) -> int:
    context = build_context(args)
    if not context.sources.set_enabled(args.source_id, enabled):
        print(f"error: no source with id {args.source_id}", file=sys.stderr)
        return 1
    print(f"source {args.source_id} {'enabled' if enabled else 'disabled'}")
    return 0


def cmd_source_remove(args: argparse.Namespace) -> int:
    context = build_context(args)
    source = context.sources.get(args.source_id)
    if source is None:
        print(f"error: no source with id {args.source_id}", file=sys.stderr)
        return 1

    tracked = len(context.entries.list(source_id=source.id, limit=1_000_000))
    if not args.yes:
        print(f'Removing "{source.name}" also forgets {tracked} tracked article(s).')
        print("Generated audio stays in the library; re-adding the source may")
        print("regenerate those articles.")
        try:
            answer = input(f'Remove source {source.id} "{source.name}"? [y/N] ')
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0

    context.sources.remove(source.id)
    print(f'Removed source {source.id} "{source.name}"')
    return 0


# --- entry management ------------------------------------------------------


def cmd_entry_list(args: argparse.Namespace) -> int:
    context = build_context(args)
    status = None
    if args.status:
        try:
            status = EntryStatus(args.status)
        except ValueError:
            valid = ", ".join(s.value for s in EntryStatus)
            print(
                f"error: unknown status {args.status!r} (choose from {valid})",
                file=sys.stderr,
            )
            return 1

    entries = context.entries.list(
        status=status, source_id=args.source_id, limit=args.limit
    )
    if not entries:
        print("(no entries)")
        return 0

    rows = [
        (
            str(e.id),
            str(e.source_id),
            e.status.value,
            _truncate(e.title, 48),
            str(e.retry_count),
            _relative(e.published_at),
        )
        for e in entries
    ]
    _print_table(("ID", "SRC", "STATUS", "TITLE", "TRY", "PUBLISHED"), rows)

    if args.verbose:
        print()
        for entry in entries:
            print(f"entry {entry.id}: {entry.article_url}")
            if entry.error_message:
                print(f"  error: {entry.error_message}")
    return 0


def cmd_entry_retry(args: argparse.Namespace) -> int:
    context = build_context(args)
    entry = context.entries.get(args.entry_id)
    if entry is None:
        print(f"error: no entry with id {args.entry_id}", file=sys.stderr)
        return 1
    if entry.status is EntryStatus.PROCESSING and not args.force:
        print(
            f"error: entry {entry.id} is being processed right now; "
            "pass --force to requeue it anyway",
            file=sys.stderr,
        )
        return 1

    context.entries.requeue(entry.id)
    print(f"entry {entry.id} requeued: {entry.title}")
    print("run `vocast worker` (or `vocast run`) to process it")
    return 0


def cmd_entry_show(args: argparse.Namespace) -> int:
    context = build_context(args)
    entry = context.entries.get(args.entry_id)
    if entry is None:
        print(f"error: no entry with id {args.entry_id}", file=sys.stderr)
        return 1
    source = context.sources.get(entry.source_id)
    print(f"entry:     {entry.id}")
    print(f"title:     {entry.title}")
    print(f"url:       {entry.article_url}")
    print(f"source:    {entry.source_id} ({source.name if source else 'removed'})")
    print(f"status:    {entry.status.value}")
    print(f"guid:      {entry.external_guid}")
    print(f"published: {_iso(entry.published_at)}")
    print(f"retries:   {entry.retry_count}")
    print(f"episode:   {entry.vocast_episode_id or '-'}")
    if entry.next_retry_at:
        print(f"retry at:  {_iso(entry.next_retry_at)}")
    if entry.error_message:
        print(f"error:     {entry.error_message}")
    return 0


# --- helpers ---------------------------------------------------------------


def _entry_counts_by_source(context: AppContext) -> dict[int, int]:
    counts: dict[int, int] = {}
    for source in context.sources.list():
        counts[source.id] = len(
            context.entries.list(source_id=source.id, limit=1_000_000)
        )
    return counts


def _parse_headers(raw: list[str] | None) -> dict[str, object]:
    """Turn repeated --header 'Name: value' flags into adapter options."""
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for item in raw:
        name, separator, value = item.partition(":")
        if not separator:
            raise SystemExit(
                f"error: --header must look like 'Name: value' (got {item!r})"
            )
        headers[name.strip()] = value.strip()
    return {"headers": headers} if headers else {}


def _print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))
    ]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip())
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else "-"


def _relative(value: datetime | None) -> str:
    if value is None:
        return "never"
    delta = datetime.now(timezone.utc) - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def cmd_config_show(args: argparse.Namespace) -> int:
    """Print the effective configuration, with secrets masked."""
    try:
        config = load_config(getattr(args, "config", None))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    rendered = {
        "config_file": str(config.source_path) if config.source_path else None,
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "public_base_url": config.server.public_base_url,
        },
        "database": {"path": str(config.database.path)},
        "storage": {"library_path": str(config.storage.library_path)},
        "polling": {
            "default_interval_minutes": config.polling.default_interval_minutes
        },
        "worker": {
            "concurrency": config.worker.concurrency,
            "processing_timeout_minutes": config.worker.processing_timeout_minutes,
            "max_retries": config.worker.max_retries,
            "base_retry_minutes": config.worker.base_retry_minutes,
            "max_retry_minutes": config.worker.max_retry_minutes,
        },
        "retention": {
            "enabled": config.retention.enabled,
            "max_age_days": config.retention.max_age_days,
            "max_episodes": config.retention.max_episodes,
        },
        "tts": {"engine": config.tts.engine, "voice": config.tts.voice},
        "allow_private_urls": config.allow_private_urls,
        "log_level": config.log_level,
        "admin_token": "<set>" if config.admin_token else None,
        "sources": [
            {
                "name": s.name,
                "kind": s.kind,
                "url": s.url,
                "enabled": s.enabled,
                "options": _mask_options(s.options),
            }
            for s in config.sources
        ],
    }
    print(json.dumps(rendered, indent=2))
    return 0


_SECRET_OPTION_KEYS = frozenset({"password", "username", "token", "api_key"})


def _mask_options(options: dict[str, object]) -> dict[str, object]:
    masked: dict[str, object] = {}
    for key, value in options.items():
        if key.lower() in _SECRET_OPTION_KEYS:
            masked[key] = "<set>"
        elif key.lower() == "headers" and isinstance(value, dict):
            from .nethttp import redact_headers

            masked[key] = redact_headers({str(k): str(v) for k, v in value.items()})
        else:
            masked[key] = value
    return masked
