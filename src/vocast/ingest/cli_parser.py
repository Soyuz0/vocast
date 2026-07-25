"""Subparser registration for the ingestion commands.

Separated from the handlers so `vocast --help` can be built without importing
the database, config, or TTS layers.
"""

from __future__ import annotations

import argparse


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="config file (default: $VOCAST_CONFIG, else ~/.vocast/config.yaml)",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="override the ingestion database path",
    )


def register_parsers(sub: argparse._SubParsersAction) -> None:
    _register_source(sub)
    _register_entry(sub)
    _register_runtime(sub)
    _register_config(sub)


def _register_source(sub: argparse._SubParsersAction) -> None:
    from .cli_commands import (
        cmd_source_add,
        cmd_source_disable,
        cmd_source_enable,
        cmd_source_list,
        cmd_source_remove,
    )

    parser = sub.add_parser("source", help="manage RSS/Atom/FreshRSS sources")
    actions = parser.add_subparsers(dest="source_cmd", required=True)

    add = actions.add_parser("add", help="track a new feed")
    add.add_argument("--name", required=True, help="display name for the source")
    add.add_argument("--url", required=True, help="feed URL (RSS or Atom)")
    add.add_argument(
        "--kind",
        default="rss",
        help="source kind: rss or freshrss_feed (default: rss)",
    )
    add.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="MINUTES",
        help="poll interval (default: the configured global interval)",
    )
    add.add_argument(
        "--header",
        action="append",
        metavar="'Name: value'",
        help="extra request header; repeatable",
    )
    add.add_argument("--username", default=None, help="HTTP Basic Auth username")
    add.add_argument("--password", default=None, help="HTTP Basic Auth password")
    add.add_argument(
        "--allow-private",
        action="store_true",
        help="permit a LAN/loopback host for this source (e.g. a local FreshRSS)",
    )
    add.add_argument(
        "--disabled", action="store_true", help="add the source without enabling it"
    )
    _add_common(add)
    add.set_defaults(func=cmd_source_add)

    listing = actions.add_parser("list", help="show tracked sources")
    _add_common(listing)
    listing.set_defaults(func=cmd_source_list)

    enable = actions.add_parser("enable", help="resume polling a source")
    enable.add_argument("source_id", type=int)
    _add_common(enable)
    enable.set_defaults(func=cmd_source_enable)

    disable = actions.add_parser("disable", help="stop polling a source")
    disable.add_argument("source_id", type=int)
    _add_common(disable)
    disable.set_defaults(func=cmd_source_disable)

    remove = actions.add_parser("remove", help="forget a source and its entries")
    remove.add_argument("source_id", type=int)
    remove.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    _add_common(remove)
    remove.set_defaults(func=cmd_source_remove)


def _register_entry(sub: argparse._SubParsersAction) -> None:
    from .cli_commands import cmd_entry_list, cmd_entry_retry, cmd_entry_show

    parser = sub.add_parser("entry", help="inspect and requeue discovered articles")
    actions = parser.add_subparsers(dest="entry_cmd", required=True)

    listing = actions.add_parser("list", help="show discovered articles")
    listing.add_argument(
        "--status",
        default=None,
        help="pending, processing, ready, failed, ignored, or expired",
    )
    listing.add_argument("--source-id", type=int, default=None)
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument(
        "-v", "--verbose", action="store_true", help="also show URLs and errors"
    )
    _add_common(listing)
    listing.set_defaults(func=cmd_entry_list)

    retry = actions.add_parser("retry", help="put a failed article back in the queue")
    retry.add_argument("entry_id", type=int)
    retry.add_argument(
        "--force", action="store_true", help="requeue even if it looks in-flight"
    )
    _add_common(retry)
    retry.set_defaults(func=cmd_entry_retry)

    show = actions.add_parser("show", help="show one article in full")
    show.add_argument("entry_id", type=int)
    _add_common(show)
    show.set_defaults(func=cmd_entry_show)


def _register_runtime(sub: argparse._SubParsersAction) -> None:
    from .runtime_commands import cmd_poll

    poll = sub.add_parser("poll", help="fetch sources once and queue new articles")
    poll.add_argument(
        "--source-id", type=int, default=None, help="poll only this source"
    )
    poll.add_argument(
        "--due-only",
        action="store_true",
        help="respect poll intervals instead of fetching immediately",
    )
    _add_common(poll)
    poll.set_defaults(func=cmd_poll)


def _register_config(sub: argparse._SubParsersAction) -> None:
    from .cli_commands import cmd_config_show

    parser = sub.add_parser("config", help="inspect configuration")
    actions = parser.add_subparsers(dest="config_cmd", required=True)

    show = actions.add_parser("show", help="print effective config, secrets masked")
    _add_common(show)
    show.set_defaults(func=cmd_config_show)
