"""Config precedence, env interpolation, validation, and source parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from vocast.ingest.config import ConfigError, expand_env, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- defaults --------------------------------------------------------------


def test_defaults_apply_without_a_config_file(tmp_path: Path):
    config = load_config(env={"VOCAST_DATABASE_PATH": str(tmp_path / "x.db")})
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8080
    assert config.worker.concurrency == 1
    assert config.polling.default_interval_minutes == 15
    assert config.retention.enabled is False


def test_missing_explicit_config_file_is_an_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", env={})


def test_missing_vocast_config_env_target_is_an_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="missing file"):
        load_config(env={"VOCAST_CONFIG": str(tmp_path / "nope.yaml")})


def test_empty_config_file_is_accepted(tmp_path: Path):
    config = load_config(_write(tmp_path, ""), env={})
    assert config.server.port == 8080


# --- file parsing ----------------------------------------------------------


def test_yaml_values_are_read(tmp_path: Path):
    path = _write(
        tmp_path,
        """
server:
  host: 0.0.0.0
  port: 9000
  public_base_url: https://podcast.example.com/
database:
  path: /data/state.db
storage:
  library_path: /data/library
worker:
  concurrency: 3
  max_retries: 7
retention:
  enabled: true
  max_age_days: 30
tts:
  engine: kokoro
  voice: af_bella
""",
    )
    config = load_config(path, env={})

    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9000
    assert config.database.path == Path("/data/state.db")
    assert config.storage.library_path == Path("/data/library")
    assert config.worker.concurrency == 3
    assert config.worker.max_retries == 7
    assert config.retention.enabled is True
    assert config.retention.max_age_days == 30
    assert config.tts.voice == "af_bella"


def test_trailing_slash_is_stripped_from_public_base_url(tmp_path: Path):
    path = _write(
        tmp_path, "server:\n  public_base_url: https://podcast.example.com/\n"
    )
    assert load_config(path, env={}).server.public_base_url == (
        "https://podcast.example.com"
    )


def test_public_base_url_must_be_absolute(tmp_path: Path):
    path = _write(tmp_path, "server:\n  public_base_url: podcast.example.com\n")
    with pytest.raises(ConfigError, match="must start with http"):
        load_config(path, env={})


def test_non_numeric_port_is_rejected(tmp_path: Path):
    path = _write(tmp_path, "server:\n  port: eighty\n")
    with pytest.raises(ConfigError, match="server.port"):
        load_config(path, env={})


def test_zero_concurrency_is_rejected(tmp_path: Path):
    path = _write(tmp_path, "worker:\n  concurrency: 0\n")
    with pytest.raises(ConfigError, match="at least 1"):
        load_config(path, env={})


def test_top_level_list_is_rejected(tmp_path: Path):
    path = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping at the top level"):
        load_config(path, env={})


def test_invalid_yaml_is_reported_as_config_error(tmp_path: Path):
    path = _write(tmp_path, "server: {unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path, env={})


def test_section_that_should_be_a_mapping_is_validated(tmp_path: Path):
    path = _write(tmp_path, "server: 8080\n")
    with pytest.raises(ConfigError, match="`server` must be a mapping"):
        load_config(path, env={})


# --- environment overrides -------------------------------------------------


def test_env_overrides_the_file(tmp_path: Path):
    path = _write(tmp_path, "server:\n  port: 9000\n")
    config = load_config(path, env={"VOCAST_SERVER_PORT": "7000"})
    assert config.server.port == 7000


def test_env_supplies_the_admin_token(tmp_path: Path):
    config = load_config(_write(tmp_path, ""), env={"VOCAST_ADMIN_TOKEN": "s3cret"})
    assert config.admin_token == "s3cret"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False)],
)
def test_boolean_env_values(tmp_path: Path, raw: str, expected: bool):
    config = load_config(_write(tmp_path, ""), env={"VOCAST_RETENTION_ENABLED": raw})
    assert config.retention.enabled is expected


def test_unlimited_retention_count_becomes_none(tmp_path: Path):
    config = load_config(
        _write(tmp_path, ""), env={"VOCAST_RETENTION_MAX_EPISODES": "unlimited"}
    )
    assert config.retention.max_episodes is None


def test_log_level_env_is_upper_cased(tmp_path: Path):
    config = load_config(_write(tmp_path, ""), env={"VOCAST_LOG_LEVEL": "debug"})
    assert config.log_level == "DEBUG"


# --- ${VAR} interpolation --------------------------------------------------


def test_placeholder_is_replaced_from_the_environment():
    assert expand_env("Basic ${TOKEN}", {"TOKEN": "abc"}) == "Basic abc"


def test_placeholder_default_is_used_when_unset():
    assert expand_env("${MISSING:-fallback}", {}) == "fallback"


def test_placeholder_default_may_be_empty():
    assert expand_env("${MISSING:-}", {}) == ""


def test_missing_placeholder_without_a_default_is_an_error():
    with pytest.raises(ConfigError, match="not set"):
        expand_env("${MISSING}", {})


def test_placeholders_are_expanded_inside_nested_structures():
    result = expand_env({"a": [{"b": "${T}"}]}, {"T": "v"})
    assert result == {"a": [{"b": "v"}]}


def test_secrets_can_come_from_the_environment_instead_of_the_file(tmp_path: Path):
    path = _write(
        tmp_path,
        """
sources:
  - name: FreshRSS
    kind: freshrss_feed
    url: https://freshrss.example.com/i/?a=rss
    headers:
      Authorization: "Basic ${FRESHRSS_TOKEN}"
""",
    )
    config = load_config(path, env={"FRESHRSS_TOKEN": "dG9rZW4="})
    assert config.sources[0].options["headers"]["Authorization"] == "Basic dG9rZW4="


# --- sources ---------------------------------------------------------------


def test_sources_are_parsed_with_adapter_options(tmp_path: Path):
    path = _write(
        tmp_path,
        """
sources:
  - name: Example Feed
    kind: rss
    url: https://example.com/feed.xml
    enabled: true
    poll_interval_minutes: 30
    max_entries_per_poll: 10
    headers:
      User-Agent: custom
""",
    )
    [source] = load_config(path, env={}).sources

    assert source.name == "Example Feed"
    assert source.kind == "rss"
    assert source.poll_interval_minutes == 30
    assert source.options["max_entries_per_poll"] == 10
    assert source.options["headers"] == {"User-Agent": "custom"}


def test_source_kind_defaults_to_rss(tmp_path: Path):
    path = _write(
        tmp_path, "sources:\n  - name: A\n    url: https://example.com/f.xml\n"
    )
    assert load_config(path, env={}).sources[0].kind == "rss"


def test_source_without_a_url_is_rejected(tmp_path: Path):
    path = _write(tmp_path, "sources:\n  - name: A\n")
    with pytest.raises(ConfigError, match="missing required key `url`"):
        load_config(path, env={})


def test_source_without_a_name_is_rejected(tmp_path: Path):
    path = _write(tmp_path, "sources:\n  - url: https://example.com/f.xml\n")
    with pytest.raises(ConfigError, match="missing required key `name`"):
        load_config(path, env={})


def test_sources_must_be_a_list(tmp_path: Path):
    path = _write(tmp_path, "sources:\n  name: A\n")
    with pytest.raises(ConfigError, match="`sources` must be a list"):
        load_config(path, env={})


def test_source_poll_interval_is_optional(tmp_path: Path):
    path = _write(
        tmp_path, "sources:\n  - name: A\n    url: https://example.com/f.xml\n"
    )
    assert load_config(path, env={}).sources[0].poll_interval_minutes is None


def test_shipped_example_config_is_valid():
    """The documented example must stay loadable as the schema evolves."""
    example = Path(__file__).parent.parent / "config.example.yaml"
    config = load_config(
        example,
        env={
            "FRESHRSS_TOKEN": "token",
            "MEMBERS_USER": "user",
            "MEMBERS_PASSWORD": "password",
        },
    )

    assert config.server.public_base_url == "https://podcast.example.com"
    assert config.retention.enabled is False
    assert {s.kind for s in config.sources} == {"rss", "freshrss_feed"}


def test_example_config_declares_no_literal_secrets():
    """Credentials in the example must be ${VAR} references, not values."""
    example = Path(__file__).parent.parent / "config.example.yaml"
    body = example.read_text()
    for line in body.splitlines():
        if any(key in line for key in ("password:", "username:", "admin_token:")):
            assert "${" in line, f"example config hardcodes a secret: {line.strip()}"


def test_configured_library_path_applies_to_legacy_commands(tmp_path: Path):
    """`vocast add` and the server must agree on where audio lives.

    They are wired up separately, so without this the manual workflow would
    write episodes the feed never shows.
    """
    from vocast import library
    from vocast.cli import _apply_storage_config

    original = library.LIBRARY_PATH
    custom = tmp_path / "custom-audio"
    config = _write(tmp_path, f"storage:\n  library_path: {custom}\n")
    try:
        _apply_storage_config(str(config))
        assert library.LIBRARY_PATH == custom
    finally:
        library.set_library_path(original)


# --- storage guard ---------------------------------------------------------


def test_verify_storage_creates_a_missing_library(tmp_path: Path):
    from vocast.ingest.config import StorageConfig
    from vocast.ingest.storage import verify_storage

    target = tmp_path / "library"
    verify_storage(StorageConfig(library_path=target))
    assert target.is_dir()


def test_verify_storage_rejects_an_unwritable_library(tmp_path: Path):
    import os

    from vocast.ingest.config import StorageConfig
    from vocast.ingest.storage import StorageUnavailableError, verify_storage

    target = tmp_path / "readonly"
    target.mkdir()
    os.chmod(target, 0o500)
    try:
        with pytest.raises(StorageUnavailableError, match="not writable"):
            verify_storage(StorageConfig(library_path=target))
    finally:
        os.chmod(target, 0o700)


def test_marker_requirement_catches_an_unmounted_share(tmp_path: Path):
    """An empty mountpoint must not be mistaken for an empty library."""
    from vocast.ingest.config import StorageConfig
    from vocast.ingest.storage import StorageUnavailableError, verify_storage

    target = tmp_path / "not-mounted"
    target.mkdir()
    with pytest.raises(StorageUnavailableError, match="not mounted yet"):
        verify_storage(StorageConfig(library_path=target, require_marker=True))


def test_marker_requirement_passes_once_the_share_is_mounted(tmp_path: Path):
    from vocast.ingest.config import STORAGE_MARKER, StorageConfig
    from vocast.ingest.storage import verify_storage

    target = tmp_path / "mounted"
    target.mkdir()
    (target / STORAGE_MARKER).touch()
    verify_storage(StorageConfig(library_path=target, require_marker=True))


def test_config_show_reports_whether_the_feed_is_protected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Operators need to verify protection without the secret being printed."""
    import argparse
    import json

    from vocast.ingest.cli_commands import cmd_config_show

    config = _write(tmp_path, "server:\n  feed_token: sup3r-s3cret\n")
    cmd_config_show(argparse.Namespace(config=str(config)))

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["server"]["feed_token"] == "<set>"
    assert "sup3r-s3cret" not in json.dumps(rendered)


def test_config_show_reports_an_unprotected_feed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    import argparse
    import json

    from vocast.ingest.cli_commands import cmd_config_show

    cmd_config_show(argparse.Namespace(config=str(_write(tmp_path, ""))))
    assert json.loads(capsys.readouterr().out)["server"]["feed_token"] is None


# --- cpu tuning ------------------------------------------------------------


def test_threads_are_divided_among_workers(monkeypatch: pytest.MonkeyPatch):
    """N workers each taking every core oversubscribes the machine N-fold."""
    from vocast.ingest import tuning

    monkeypatch.setattr(tuning, "available_cpus", lambda: 8)
    assert tuning.threads_per_worker(4) == 2
    assert tuning.threads_per_worker(1) == 8


def test_at_least_one_thread_per_worker(monkeypatch: pytest.MonkeyPatch):
    from vocast.ingest import tuning

    monkeypatch.setattr(tuning, "available_cpus", lambda: 2)
    assert tuning.threads_per_worker(8) == 1


def test_explicit_thread_count_wins(monkeypatch: pytest.MonkeyPatch):
    from vocast.ingest import tuning

    monkeypatch.setattr(tuning, "available_cpus", lambda: 16)
    assert tuning.threads_per_worker(4, 3) == 3


def test_cgroup_quota_is_preferred_over_host_cores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Under a container CPU limit, host core count overstates what we have."""
    from vocast.ingest import tuning

    quota = tmp_path / "cpu.max"
    quota.write_text("400000 100000\n")
    real_open = open

    def fake_open(path, *a, **k):
        if str(path) == "/sys/fs/cgroup/cpu.max":
            return real_open(quota)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    assert tuning.available_cpus() == 4


def test_unlimited_cgroup_falls_back_to_affinity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from vocast.ingest import tuning

    quota = tmp_path / "cpu.max"
    quota.write_text("max 100000\n")
    real_open = open

    def fake_open(path, *a, **k):
        if str(path) == "/sys/fs/cgroup/cpu.max":
            return real_open(quota)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    assert tuning.available_cpus() >= 1


# --- engine selection ------------------------------------------------------


def test_unknown_engine_names_the_available_ones():
    from vocast.engines import get_engine

    with pytest.raises(ValueError, match="kokoro-onnx"):
        get_engine("festival")


def test_onnx_engine_reports_chunking_matched_to_the_pytorch_one():
    """Chunk size sets cancellation granularity, so it must not grow."""
    from vocast.engines.kokoro_engine import KokoroEngine
    from vocast.engines.kokoro_onnx_engine import KokoroOnnxEngine

    assert KokoroOnnxEngine.MAX_CHARS == KokoroEngine.MAX_CHARS
    assert KokoroOnnxEngine.SAMPLE_RATE == KokoroEngine.SAMPLE_RATE
    assert KokoroOnnxEngine.DEFAULT_VOICE == KokoroEngine.DEFAULT_VOICE


def test_onnx_model_dir_honours_the_environment(monkeypatch: pytest.MonkeyPatch):
    from vocast.engines.kokoro_onnx_engine import default_model_dir

    monkeypatch.setenv("VOCAST_TTS_MODEL_DIR", "/data/cache/onnx")
    assert default_model_dir() == Path("/data/cache/onnx")


def test_onnx_engine_reuses_existing_model_files(tmp_path: Path):
    """A present file must never trigger a 325 MB download."""
    from vocast.engines.kokoro_onnx_engine import MODEL_FILE, _ensure_file

    (tmp_path / MODEL_FILE).write_bytes(b"pretend-model")
    assert _ensure_file(tmp_path, MODEL_FILE) == tmp_path / MODEL_FILE
