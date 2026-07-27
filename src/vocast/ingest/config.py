"""Configuration from a YAML file, overlaid with environment variables.

Precedence, lowest to highest: built-in defaults, the YAML file, then the
environment. Defaults are chosen so that an existing vocast install keeps
working unchanged (`~/.vocast/library`, `127.0.0.1:8080`).

Environment variable convention
-------------------------------
`VOCAST_<SECTION>_<KEY>`, upper-cased, e.g.

    VOCAST_CONFIG                            path to this YAML file
    VOCAST_SERVER_HOST / _PORT
    VOCAST_SERVER_PUBLIC_BASE_URL
    VOCAST_SERVER_FEED_TOKEN
    VOCAST_SERVER_AUDIO_BASE_URL
    VOCAST_SERVER_FEED_MAX_ITEMS
    VOCAST_SERVER_HIDE_AFTER_DOWNLOAD_HOURS
    VOCAST_FRESHRSS_MARK_READ_ON_DOWNLOAD
    VOCAST_DATABASE_PATH
    VOCAST_STORAGE_LIBRARY_PATH
    VOCAST_STORAGE_REQUIRE_MARKER
    VOCAST_POLLING_DEFAULT_INTERVAL_MINUTES
    VOCAST_POLLING_FULL_POLL_HOURS
    VOCAST_WORKER_CONCURRENCY
    VOCAST_WORKER_PROCESSING_TIMEOUT_MINUTES
    VOCAST_WORKER_MAX_RETRIES
    VOCAST_WORKER_BASE_RETRY_MINUTES
    VOCAST_WORKER_MAX_RETRY_MINUTES
    VOCAST_WORKER_NEWEST_FIRST
    VOCAST_WORKER_RECLAIM_ON_START
    VOCAST_WORKER_NICE
    VOCAST_WORKER_THREADS_PER_WORKER
    VOCAST_RETENTION_ENABLED / _MAX_AGE_DAYS / _MAX_EPISODES / _INCLUDE_MANUAL
    VOCAST_TTS_ENGINE / VOCAST_TTS_VOICE
    VOCAST_ADMIN_TOKEN
    VOCAST_LOG_LEVEL
    VOCAST_ALLOW_PRIVATE_URLS

Secrets never need to live in the YAML file: any string value may reference
the environment as `${VAR}`, or `${VAR:-fallback}`. A `${VAR}` with no value
set is an error, so a missing secret fails loudly instead of being sent
literally as an Authorization header.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

VOCAST_HOME = Path.home() / ".vocast"

DEFAULT_CONFIG_PATHS = (
    VOCAST_HOME / "config.yaml",
    Path("/data/config.yaml"),
)

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """The configuration is unusable. The message names the offending key."""


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    #: Absolute URL the feed is reachable at from the public internet. When
    #: unset, URLs are derived per-request, which breaks behind a proxy that
    #: terminates TLS.
    public_base_url: str | None = None
    #: When set, the feeds, audio, and cover art require `?token=<value>`.
    #: Podcast clients cannot send headers, so a query parameter is the only
    #: option; this is how commercial private podcast feeds work.
    feed_token: str | None = None
    #: Base URL for episode audio, when it differs from public_base_url. Lets a
    #: feed be published on a public endpoint while the audio it points at stays
    #: on a private network, so only titles ever leave.
    audio_base_url: str | None = None
    #: Drop episodes from the feed this many hours after they were marked read.
    #: The files are kept. None keeps everything listed.
    #:
    #: An episode is marked read when its audio is downloaded, or when the
    #: article is read in the reader. Neither means "listened to" -- a podcast
    #: client never reports playback back -- so set the delay long enough that
    #: having the file reliably implies you have heard it, and note that clients
    #: report an episode leaving the feed as withdrawn by the publisher.
    hide_after_read_hours: int | None = None
    #: Most recent episodes to include in a feed. Podcast clients neither want
    #: nor reliably handle tens of thousands of items, and every item costs a
    #: metadata read. None means no limit.
    feed_max_items: int | None = 300
    #: Size of the recents feed. Small on purpose: podcast clients re-parse the
    #: whole document on every refresh, so a feed holding the entire backlog is
    #: slow to update even when almost nothing in it has changed.
    recent_feed_items: int = 100


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path = VOCAST_HOME / "vocast.db"


#: Sentinel written into the library directory. Its absence is how the service
#: tells "the network share is not mounted yet" from "the share is empty".
STORAGE_MARKER = ".vocast-storage"


@dataclass(frozen=True)
class StorageConfig:
    library_path: Path = VOCAST_HOME / "library"
    #: Refuse to start unless STORAGE_MARKER exists in library_path. Guards a
    #: network mount: if it is not ready, the bind mount exposes an empty
    #: directory and episodes would be written somewhere they are never served
    #: from. Off by default, since local storage cannot have this problem.
    require_marker: bool = False


@dataclass(frozen=True)
class PollingConfig:
    default_interval_minutes: int = 15
    #: How often to walk a source's upstream stream completely, rather than
    #: stopping at the first page of already-known articles. Only a complete
    #: walk can tell that a queued article has since been read upstream. 0
    #: disables it.
    full_poll_hours: int = 0


@dataclass(frozen=True)
class WorkerConfig:
    concurrency: int = 1
    processing_timeout_minutes: int = 60
    max_retries: int = 5
    base_retry_minutes: int = 5
    max_retry_minutes: int = 360
    #: Narrate the most recently published article first instead of the
    #: longest-queued one. Off by default so existing setups keep FIFO order.
    newest_first: bool = False
    #: Requeue every in-flight claim at startup rather than waiting for
    #: processing_timeout_minutes. A restart abandons whatever was mid-synthesis,
    #: and waiting the full timeout leaves the newest articles stuck. Safe only
    #: when this process is the sole worker, so it is off by default: another
    #: `vocast worker` running alongside would have its live claims stolen.
    reclaim_on_start: bool = False
    #: Scheduling priority offset for synthesis threads (0-19, higher yields
    #: more). Synthesis is CPU-bound; nicing it keeps the machine usable for
    #: interactive work without slowing throughput much when otherwise idle.
    nice: int = 0
    #: Compute threads each worker's TTS engine may use. None divides the
    #: available CPUs among the workers. Left to the library's own default,
    #: every worker grabs every core, and N workers oversubscribe the machine
    #: N-fold: the threads then spend their time contending rather than working.
    threads_per_worker: int | None = None


@dataclass(frozen=True)
class RetentionConfig:
    enabled: bool = False
    max_age_days: int | None = 90
    max_episodes: int | None = 1000
    #: Whether episodes added by hand with `vocast add` are also swept. Off by
    #: default: those are deliberate and cannot be regenerated from a feed.
    include_manual: bool = False


@dataclass(frozen=True)
class FreshRSSConfig:
    """Writing back to FreshRSS. Reading from it needs nothing here."""

    #: Mark an article read once its episode audio has been downloaded. Off by
    #: default: it changes state in another application, and a client that
    #: downloads eagerly would mark things read before you hear them.
    mark_read_on_download: bool = False


@dataclass(frozen=True)
class TTSConfig:
    engine: str = "kokoro"
    voice: str | None = None
    #: Voice for block quotes, so a passage the author is quoting is audibly
    #: someone else's words. Unset means one voice throughout.
    quote_voice: str | None = None


@dataclass(frozen=True)
class SourceConfig:
    name: str
    kind: str
    url: str
    enabled: bool = True
    poll_interval_minutes: int | None = None
    #: Adapter-specific settings (headers, credentials, limits).
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    freshrss: FreshRSSConfig = field(default_factory=FreshRSSConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    sources: tuple[SourceConfig, ...] = ()
    admin_token: str | None = None
    log_level: str = "INFO"
    #: Global default for reaching LAN/loopback hosts; overridable per source.
    allow_private_urls: bool = False
    source_path: Path | None = None


def load_config(
    path: Path | str | None = None, env: Mapping[str, str] | None = None
) -> Config:
    """Build a Config from a YAML file plus environment overrides.

    With no explicit path, `VOCAST_CONFIG` is honored, then the first existing
    default location. A missing file is not an error unless it was requested
    explicitly, so vocast keeps working with no config at all.
    """
    environ = os.environ if env is None else env
    resolved = _resolve_path(path, environ)
    raw: dict[str, Any] = {}
    if resolved is not None:
        raw = _read_yaml(resolved, environ)

    config = _from_mapping(raw)
    config = replace(config, source_path=resolved)
    return _apply_env(config, environ)


def _resolve_path(path: Path | str | None, env: Mapping[str, str]) -> Path | None:
    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
        return candidate
    from_env = env.get("VOCAST_CONFIG")
    if from_env:
        candidate = Path(from_env)
        if not candidate.is_file():
            raise ConfigError(f"VOCAST_CONFIG points at a missing file: {candidate}")
        return candidate
    for default in DEFAULT_CONFIG_PATHS:
        if default.is_file():
            return default
    return None


def _read_yaml(path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging guard
        raise ConfigError(
            "reading a config file needs PyYAML; install vocast with its "
            "dependencies or drop the config file and use environment variables"
        ) from exc
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return expand_env(loaded, env)


def expand_env(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively substitute `${VAR}` / `${VAR:-default}` in string values."""
    environ = os.environ if env is None else env
    if isinstance(value, str):
        return _expand_string(value, environ)
    if isinstance(value, dict):
        return {k: expand_env(v, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, environ) for v in value]
    return value


def _expand_string(text: str, env: Mapping[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if fallback is not None:
            return fallback
        raise ConfigError(
            f"config references ${{{name}}} but that environment variable is not "
            f"set; set it, or write ${{{name}:-default}} to allow a fallback"
        )

    return _PLACEHOLDER.sub(substitute, text)


def _from_mapping(raw: dict[str, Any]) -> Config:
    server = _section(raw, "server")
    database = _section(raw, "database")
    storage = _section(raw, "storage")
    polling = _section(raw, "polling")
    worker = _section(raw, "worker")
    retention = _section(raw, "retention")
    tts = _section(raw, "tts")
    freshrss = _section(raw, "freshrss")

    return Config(
        server=ServerConfig(
            host=str(server.get("host", ServerConfig.host)),
            port=_as_int(server.get("port"), ServerConfig.port, "server.port"),
            public_base_url=_clean_base_url(server.get("public_base_url")),
            feed_token=_as_optional_str(server.get("feed_token")),
            audio_base_url=_clean_base_url(server.get("audio_base_url")),
            hide_after_read_hours=_as_optional_int(
                # The old key is still honoured: silently reverting to the
                # default would leave every episode listed forever.
                server.get(
                    "hide_after_read_hours", server.get("hide_after_download_hours")
                ),
                ServerConfig.hide_after_read_hours,
                "server.hide_after_read_hours",
            ),
            recent_feed_items=_as_int(
                server.get("recent_feed_items"),
                ServerConfig.recent_feed_items,
                "server.recent_feed_items",
            ),
            feed_max_items=_as_optional_int(
                server.get("feed_max_items"),
                ServerConfig.feed_max_items,
                "server.feed_max_items",
            ),
        ),
        database=DatabaseConfig(
            path=Path(str(database.get("path", DatabaseConfig.path))).expanduser()
        ),
        storage=StorageConfig(
            library_path=Path(
                str(storage.get("library_path", StorageConfig.library_path))
            ).expanduser(),
            require_marker=_as_bool(
                storage.get("require_marker"),
                StorageConfig.require_marker,
                "storage.require_marker",
            ),
        ),
        polling=PollingConfig(
            default_interval_minutes=_as_int(
                polling.get("default_interval_minutes"),
                PollingConfig.default_interval_minutes,
                "polling.default_interval_minutes",
                minimum=1,
            ),
            full_poll_hours=_as_int(
                polling.get("full_poll_hours"),
                PollingConfig.full_poll_hours,
                "polling.full_poll_hours",
                minimum=0,
            ),
        ),
        worker=WorkerConfig(
            concurrency=_as_int(
                worker.get("concurrency"),
                WorkerConfig.concurrency,
                "worker.concurrency",
                minimum=1,
            ),
            processing_timeout_minutes=_as_int(
                worker.get("processing_timeout_minutes"),
                WorkerConfig.processing_timeout_minutes,
                "worker.processing_timeout_minutes",
                minimum=1,
            ),
            max_retries=_as_int(
                worker.get("max_retries"),
                WorkerConfig.max_retries,
                "worker.max_retries",
                minimum=0,
            ),
            base_retry_minutes=_as_int(
                worker.get("base_retry_minutes"),
                WorkerConfig.base_retry_minutes,
                "worker.base_retry_minutes",
                minimum=1,
            ),
            max_retry_minutes=_as_int(
                worker.get("max_retry_minutes"),
                WorkerConfig.max_retry_minutes,
                "worker.max_retry_minutes",
                minimum=1,
            ),
            newest_first=_as_bool(
                worker.get("newest_first"),
                WorkerConfig.newest_first,
                "worker.newest_first",
            ),
            reclaim_on_start=_as_bool(
                worker.get("reclaim_on_start"),
                WorkerConfig.reclaim_on_start,
                "worker.reclaim_on_start",
            ),
            nice=_as_int(worker.get("nice"), WorkerConfig.nice, "worker.nice"),
            threads_per_worker=_as_optional_int(
                worker.get("threads_per_worker"),
                WorkerConfig.threads_per_worker,
                "worker.threads_per_worker",
            ),
        ),
        retention=RetentionConfig(
            enabled=_as_bool(
                retention.get("enabled"), RetentionConfig.enabled, "retention.enabled"
            ),
            max_age_days=_as_optional_int(
                retention.get("max_age_days"),
                RetentionConfig.max_age_days,
                "retention.max_age_days",
            ),
            max_episodes=_as_optional_int(
                retention.get("max_episodes"),
                RetentionConfig.max_episodes,
                "retention.max_episodes",
            ),
            include_manual=_as_bool(
                retention.get("include_manual"),
                RetentionConfig.include_manual,
                "retention.include_manual",
            ),
        ),
        freshrss=FreshRSSConfig(
            mark_read_on_download=_as_bool(
                freshrss.get("mark_read_on_download"),
                FreshRSSConfig.mark_read_on_download,
                "freshrss.mark_read_on_download",
            ),
        ),
        tts=TTSConfig(
            engine=str(tts.get("engine", TTSConfig.engine)),
            voice=_as_optional_str(tts.get("voice")),
            quote_voice=_as_optional_str(tts.get("quote_voice")),
        ),
        sources=tuple(_parse_sources(raw.get("sources"))),
        admin_token=_as_optional_str(raw.get("admin_token")),
        log_level=str(raw.get("log_level", Config.log_level)).upper(),
        allow_private_urls=_as_bool(
            raw.get("allow_private_urls"),
            Config.allow_private_urls,
            "allow_private_urls",
        ),
    )


def _parse_sources(raw: Any) -> list[SourceConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("`sources` must be a list")

    known_scalars = {"name", "kind", "url", "enabled", "poll_interval_minutes"}
    sources: list[SourceConfig] = []
    for index, item in enumerate(raw):
        where = f"sources[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping")
        for required in ("name", "url"):
            if not item.get(required):
                raise ConfigError(f"{where} is missing required key `{required}`")

        # Anything not a recognized scalar is adapter configuration (headers,
        # credentials, limits), so new adapter options need no changes here.
        options = {k: v for k, v in item.items() if k not in known_scalars}
        sources.append(
            SourceConfig(
                name=str(item["name"]),
                kind=str(item.get("kind", "rss")),
                url=str(item["url"]),
                enabled=_as_bool(item.get("enabled"), True, f"{where}.enabled"),
                poll_interval_minutes=_as_optional_int(
                    item.get("poll_interval_minutes"),
                    None,
                    f"{where}.poll_interval_minutes",
                ),
                options=options,
            )
        )
    return sources


_ENV_OVERRIDES: tuple[tuple[str, str, str, str], ...] = (
    ("VOCAST_SERVER_HOST", "server", "host", "str"),
    ("VOCAST_SERVER_PORT", "server", "port", "int"),
    ("VOCAST_SERVER_PUBLIC_BASE_URL", "server", "public_base_url", "base_url"),
    ("VOCAST_SERVER_FEED_TOKEN", "server", "feed_token", "opt_str"),
    ("VOCAST_SERVER_AUDIO_BASE_URL", "server", "audio_base_url", "base_url"),
    ("VOCAST_SERVER_FEED_MAX_ITEMS", "server", "feed_max_items", "opt_int"),
    (
        "VOCAST_SERVER_HIDE_AFTER_READ_HOURS",
        "server",
        "hide_after_read_hours",
        "opt_int",
    ),
    (
        "VOCAST_FRESHRSS_MARK_READ_ON_DOWNLOAD",
        "freshrss",
        "mark_read_on_download",
        "bool",
    ),
    ("VOCAST_DATABASE_PATH", "database", "path", "path"),
    ("VOCAST_STORAGE_LIBRARY_PATH", "storage", "library_path", "path"),
    ("VOCAST_STORAGE_REQUIRE_MARKER", "storage", "require_marker", "bool"),
    (
        "VOCAST_POLLING_DEFAULT_INTERVAL_MINUTES",
        "polling",
        "default_interval_minutes",
        "int",
    ),
    ("VOCAST_POLLING_FULL_POLL_HOURS", "polling", "full_poll_hours", "int"),
    ("VOCAST_WORKER_CONCURRENCY", "worker", "concurrency", "int"),
    (
        "VOCAST_WORKER_PROCESSING_TIMEOUT_MINUTES",
        "worker",
        "processing_timeout_minutes",
        "int",
    ),
    ("VOCAST_WORKER_MAX_RETRIES", "worker", "max_retries", "int"),
    ("VOCAST_WORKER_BASE_RETRY_MINUTES", "worker", "base_retry_minutes", "int"),
    ("VOCAST_WORKER_MAX_RETRY_MINUTES", "worker", "max_retry_minutes", "int"),
    ("VOCAST_WORKER_NEWEST_FIRST", "worker", "newest_first", "bool"),
    ("VOCAST_WORKER_RECLAIM_ON_START", "worker", "reclaim_on_start", "bool"),
    ("VOCAST_WORKER_NICE", "worker", "nice", "int"),
    ("VOCAST_WORKER_THREADS_PER_WORKER", "worker", "threads_per_worker", "opt_int"),
    ("VOCAST_RETENTION_ENABLED", "retention", "enabled", "bool"),
    ("VOCAST_RETENTION_MAX_AGE_DAYS", "retention", "max_age_days", "opt_int"),
    ("VOCAST_RETENTION_MAX_EPISODES", "retention", "max_episodes", "opt_int"),
    ("VOCAST_RETENTION_INCLUDE_MANUAL", "retention", "include_manual", "bool"),
    ("VOCAST_TTS_ENGINE", "tts", "engine", "str"),
    ("VOCAST_TTS_VOICE", "tts", "voice", "opt_str"),
    ("VOCAST_TTS_QUOTE_VOICE", "tts", "quote_voice", "opt_str"),
    ("VOCAST_ADMIN_TOKEN", "", "admin_token", "opt_str"),
    ("VOCAST_LOG_LEVEL", "", "log_level", "upper"),
    ("VOCAST_ALLOW_PRIVATE_URLS", "", "allow_private_urls", "bool"),
)


def _apply_env(config: Config, env: Mapping[str, str]) -> Config:
    for name, section, key, kind in _ENV_OVERRIDES:
        if name not in env:
            continue
        raw = env[name]
        value = _coerce_env(raw, kind, name)
        if section:
            current = getattr(config, section)
            config = replace(config, **{section: replace(current, **{key: value})})
        else:
            config = replace(config, **{key: value})
    return config


def _coerce_env(raw: str, kind: str, name: str) -> Any:
    if kind == "str":
        return raw
    if kind == "upper":
        return raw.upper()
    if kind == "opt_str":
        return raw or None
    if kind == "path":
        return Path(raw).expanduser()
    if kind == "base_url":
        return _clean_base_url(raw)
    if kind == "bool":
        return _as_bool(raw, False, name)
    if kind == "int":
        return _as_int(raw, 0, name)
    if kind == "opt_int":
        return _as_optional_int(raw, None, name)
    raise AssertionError(f"unhandled env coercion {kind!r}")


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"`{name}` must be a mapping")
    return value


def _clean_base_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().rstrip("/")
    if not text:
        return None
    if not text.startswith(("http://", "https://")):
        raise ConfigError(
            f"public_base_url must start with http:// or https:// (got {text!r})"
        )
    return text


def _as_int(value: Any, default: int, where: str, *, minimum: int | None = None) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be a whole number (got {value!r})") from None
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{where} must be at least {minimum} (got {parsed})")
    return parsed


def _as_optional_int(value: Any, default: int | None, where: str) -> int | None:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("", "none", "null", "unlimited"):
        return None
    return _as_int(value, default or 0, where, minimum=0)


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def _as_bool(value: Any, default: bool, where: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{where} must be a boolean (got {value!r})")
