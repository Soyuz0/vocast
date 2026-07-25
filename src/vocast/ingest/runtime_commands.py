"""argparse handlers for the long-running / one-shot runtime commands."""

from __future__ import annotations

import argparse
import sys

from .cli_commands import build_context
from .config import ConfigError
from .poller import Poller


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
        results = [poller.poll_source(source)]
    elif args.due_only:
        results = poller.poll_due().results
    else:
        results = poller.poll_all().results

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
