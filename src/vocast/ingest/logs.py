"""Structured-ish logging helpers.

A homelab service usually has no log pipeline, just `docker logs` and grep.
So records are plain text with a trailing `key=value` tail: greppable by field,
readable without tooling, and cheap. Values containing spaces are quoted.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER_NAME = "vocast.ingest"


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def kv(**fields: Any) -> str:
    """Render fields as a `key=value` tail, skipping None values."""
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if any(char.isspace() for char in text) or '"' in text:
            text = '"' + text.replace('"', "'").replace("\n", " ") + '"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


def configure_logging(level: str = "INFO") -> None:
    """Install a basic stderr handler unless the host app already set one up."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
