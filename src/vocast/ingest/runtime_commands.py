"""argparse handlers for the long-running / one-shot runtime commands."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from .cli_commands import build_context
from .config import ConfigError
from .context import AppContext
from .generator import VocastEpisodeGenerator
from .poller import Poller
from .worker import Worker, WorkerLoop


def cmd_poll(args: argparse.Namespace) -> int:
    """Fetch sources once. Exits non-zero only if every source failed."""
    try:
        context = build_context(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    poller = Poller(
        sources=context.sources,
        entries=context.entries,
        policy=context.fetch_policy(),
    )

    if args.source_id is not None:
        source = context.sources.get(args.source_id)
        if source is None:
            print(f"error: no source with id {args.source_id}", file=sys.stderr)
            return 1
        results = [poller.poll_source(source, full=args.full)]
    elif args.due_only:
        results = poller.poll_due().results
    else:
        results = [
            poller.poll_source(s, full=args.full)
            for s in context.sources.all(enabled_only=True)
        ]

    if not results:
        print("no sources to poll")
        print("add one with: vocast source add --name NAME --url URL")
        return 0

    inserted = 0
    for result in results:
        inserted += result.inserted
        if result.skipped:
            print(f"- {result.source_name}: already polling, skipped")
        elif result.ok:
            print(
                f"- {result.source_name}: {result.discovered} listed, "
                f"{result.inserted} new"
            )
        else:
            print(f"! {result.source_name}: {result.error}", file=sys.stderr)

    attempted = [r for r in results if not r.skipped]
    failed = [r for r in attempted if not r.ok]
    print(f"\nqueued {inserted} new article(s)")
    if inserted:
        print("run `vocast worker` to turn them into episodes")

    # Partial failure is normal for a feed reader, so only a total wipeout is
    # worth a non-zero exit.
    if attempted and len(failed) == len(attempted):
        return 1
    return 0


def build_worker(context: AppContext) -> Worker:
    """Assemble a worker around the real vocast pipeline."""
    generator = VocastEpisodeGenerator(
        engine_name=context.config.tts.engine,
        voice=context.config.tts.voice,
        policy=context.fetch_policy(),
    )
    return Worker(
        entries=context.entries, generator=generator, config=context.config.worker
    )


def cmd_worker(args: argparse.Namespace) -> int:
    context = build_context(args)
    worker = build_worker(context)
    worker.reclaim_stale()

    if args.once or args.max_entries is not None:
        outcomes = worker.drain(max_entries=args.max_entries)
        if not outcomes:
            print("queue is empty")
            return 0
        for outcome in outcomes:
            if outcome.ok:
                print(f"- entry {outcome.entry_id}: {outcome.episode_id}")
            elif outcome.retrying:
                print(f"- entry {outcome.entry_id}: will retry ({outcome.error})")
            else:
                print(f"! entry {outcome.entry_id}: failed ({outcome.error})")
        succeeded = sum(1 for o in outcomes if o.ok)
        print(f"\ngenerated {succeeded} of {len(outcomes)} episode(s)")
        return 0 if succeeded else 1

    loop = WorkerLoop(worker)
    print("vocast worker running; press Ctrl-C to stop")
    loop.start()
    try:
        while loop.running:
            loop.join(timeout=1.0)
    except KeyboardInterrupt:
        print("\nstopping after the current episode...")
    finally:
        loop.stop()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the HTTP server, the poller, and the worker in one process."""
    from .config import load_config
    from .logs import configure_logging
    from .service import run_service

    config = load_config(getattr(args, "config", None))
    if getattr(args, "db", None):
        from dataclasses import replace
        from pathlib import Path

        config = replace(
            config, database=replace(config.database, path=Path(args.db).expanduser())
        )
    configure_logging(config.log_level)

    host = args.host or config.server.host
    port = args.port or config.server.port
    print(f"vocast running on http://{host}:{port}")
    print(f"  combined feed: http://{host}:{port}/feeds/all.xml")
    print(f"  health:        http://{host}:{port}/api/health")
    if config.server.public_base_url:
        print(f"  public feed:   {config.server.public_base_url}/feeds/all.xml")

    return run_service(
        config,
        host=args.host,
        port=args.port,
        with_poller=not args.no_poller,
        with_worker=not args.no_worker,
    )


def cmd_retention_apply(args: argparse.Namespace) -> int:
    from .retention import Retention

    context = build_context(args)
    config = context.config.retention
    if not config.enabled and not args.force:
        print("retention is disabled in config; pass --force to run it once anyway")
        return 0

    retention = Retention(
        entries=context.entries,
        config=replace(config, enabled=True),
        library_path=context.config.storage.library_path,
        include_manual=args.include_manual or config.include_manual,
    )
    report = retention.apply(dry_run=args.dry_run)

    if not report.removed and not report.refused:
        print("nothing to remove")
        return 0
    verb = "would remove" if args.dry_run else "removed"
    for episode_id in report.removed:
        print(f"- {verb} {episode_id}")
    for episode_id, reason in report.refused:
        print(f"! kept {episode_id}: {reason}", file=sys.stderr)
    print(f"\n{verb} {report.count} episode(s), {report.freed_bytes // 1024} KiB")
    print(
        "note: podcast apps that already downloaded these episodes keep their "
        "local copies"
    )
    return 0


def cmd_backfill_text(args: argparse.Namespace) -> int:
    """Re-extract article text for episodes generated before it was stored.

    Only fetches and extracts; no synthesis happens, so this is cheap and safe
    to re-run. Existing audio is never touched.
    """
    from .. import library
    from ..fetch import fetch_article
    from .nethttp import fetch as guarded_fetch

    context = build_context(args)
    policy = context.fetch_policy()

    missing = [
        entry
        for entry in library.list_entries()
        if entry.source and entry.article_text() is None
    ]
    if not missing:
        print("every episode already has its article text")
        return 0

    print(f"{len(missing)} episode(s) missing article text")
    if args.limit:
        missing = missing[: args.limit]

    filled = failed = 0
    for entry in missing:
        try:
            _, text, _ = fetch_article(
                entry.source,
                html_fetcher=lambda url: guarded_fetch(url, policy=policy).text(),
            )
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"  ! {entry.short_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        entry.article_path().write_text(text, encoding="utf-8")
        filled += 1
        if not args.quiet:
            print(f"  + {entry.short_id}  {entry.title[:56]}")

    print(f"\nfilled {filled}, failed {failed}")
    return 0
