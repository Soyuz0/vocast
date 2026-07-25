"""Wiring — build the service's collaborators from a Config.

This is the one place that performs startup side effects (opening the
database, pointing the library at its directory, reconciling configured
sources). Everything downstream receives its dependencies explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import library
from .config import Config, SourceConfig, load_config
from .db import Database, open_database
from .logs import get_logger, kv
from .nethttp import FetchPolicy
from .repository import (
    DuplicateSourceError,
    EntryRepository,
    SettingsRepository,
    SourceRepository,
)

log = get_logger("context")


@dataclass
class AppContext:
    config: Config
    db: Database
    sources: SourceRepository
    entries: EntryRepository
    settings: SettingsRepository

    @classmethod
    def create(cls, config: Config | None = None) -> AppContext:
        resolved = config or load_config()
        library.set_library_path(resolved.storage.library_path)
        resolved.storage.library_path.mkdir(parents=True, exist_ok=True)
        db = open_database(resolved.database.path)
        return cls(
            config=resolved,
            db=db,
            sources=SourceRepository(db),
            entries=EntryRepository(db),
            settings=SettingsRepository(db),
        )

    def fetch_policy(self) -> FetchPolicy:
        return FetchPolicy(allow_private=self.config.allow_private_urls)

    def sync_configured_sources(self) -> int:
        """Reconcile the config file's `sources:` block into the database.

        Declaring a source in YAML is a convenience for reproducible
        deployments; sources added via the CLI or API are equally valid and are
        never removed by this. A bad entry is logged and skipped so one typo
        cannot stop the service from starting.
        """
        applied = 0
        for source in self.config.sources:
            if self._sync_one(source):
                applied += 1
        return applied

    def _sync_one(self, source: SourceConfig) -> bool:
        interval = (
            source.poll_interval_minutes
            if source.poll_interval_minutes is not None
            else self.config.polling.default_interval_minutes
        )
        try:
            stored = self.sources.upsert(
                name=source.name,
                kind=source.kind,
                url=source.url,
                enabled=source.enabled,
                poll_interval_minutes=interval,
                config=source.options,
            )
        except (DuplicateSourceError, ValueError) as exc:
            log.warning(
                "config source skipped %s",
                kv(source=source.name, url=source.url, error=str(exc)),
            )
            return False
        log.info(
            "config source applied %s",
            kv(
                source_id=stored.id,
                source=stored.name,
                kind=stored.kind,
                enabled=stored.enabled,
            ),
        )
        return True

    def close(self) -> None:
        self.db.close()
