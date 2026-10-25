"""Worker model lifetime without a real model, clock delays, or background thread."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from vocast.ingest import worker as worker_module
from vocast.ingest.config import WorkerConfig
from vocast.ingest.worker import Worker, WorkerLoop, WorkOutcome


class LoopHarness:
    def __init__(self, monkeypatch, *, timeout=10, stop_at=30):
        self.now = 0.0
        self.stop_at = stop_at
        self.worker = Mock(spec=Worker)
        self.worker.config = WorkerConfig(model_idle_seconds=timeout)
        self.worker.process_next.return_value = None
        self.loop = WorkerLoop(self.worker, idle_seconds=5)
        self.releases = []
        self.worker.release_engine.side_effect = self.record_release
        monkeypatch.setattr(
            worker_module, "time", SimpleNamespace(monotonic=lambda: self.now)
        )
        monkeypatch.setattr(self.loop._stop, "wait", self.wait)

    def record_release(self):
        assert not self.loop.busy
        self.releases.append(self.now)

    def wait(self, seconds):
        self.now += seconds
        if self.now >= self.stop_at:
            self.loop._stop.set()

    def run(self):
        self.loop._run()


@pytest.fixture
def harness(monkeypatch):
    return LoopHarness(monkeypatch)


def test_worker_delegates_model_release_to_its_generator():
    generator = Mock()
    worker = Worker(entries=Mock(), generator=generator)

    worker.release_engine()

    generator.release_engine.assert_called_once_with()


def test_idle_model_is_released_once_at_timeout_and_again_on_exit(harness):
    harness.run()

    assert harness.releases == [10, 30]


def test_zero_timeout_keeps_the_model_until_exit(monkeypatch):
    harness = LoopHarness(monkeypatch, timeout=0)

    harness.run()

    assert harness.releases == [30]


def test_each_article_restarts_the_idle_timer(harness):
    outcomes = iter([None, None, WorkOutcome(entry_id=1, episode_id="episode")])
    harness.worker.process_next.side_effect = lambda: next(outcomes, None)

    harness.run()

    assert harness.releases == [20, 30]


def test_shutdown_does_not_release_a_model_during_active_generation(harness):
    def generate():
        assert harness.loop.busy
        harness.now += 1000
        harness.loop.stop(timeout=0)
        assert harness.releases == []
        return WorkOutcome(entry_id=1, episode_id="episode")

    harness.worker.process_next.side_effect = generate

    harness.run()

    assert harness.releases == [1000]


def test_pause_releases_immediately_even_when_idle_unloading_is_disabled(monkeypatch):
    harness = LoopHarness(monkeypatch, timeout=0)
    harness.loop._is_paused = lambda: True

    harness.run()

    assert harness.releases == [0, 30]
    harness.worker.process_next.assert_not_called()


def test_resuming_after_pause_starts_a_new_model_lifetime(harness):
    harness.loop._is_paused = lambda: harness.now < 10
    outcomes = iter([WorkOutcome(entry_id=1, episode_id="episode")])
    harness.worker.process_next.side_effect = lambda: next(outcomes, None)

    harness.run()

    assert harness.releases == [0, 20, 30]


def test_pause_during_generation_waits_until_the_attempt_returns(harness):
    paused = False
    harness.loop._is_paused = lambda: paused

    def generate():
        nonlocal paused
        paused = True
        assert harness.loop.busy
        assert harness.releases == []
        return WorkOutcome(entry_id=1, retrying=True)

    harness.worker.process_next.side_effect = generate

    harness.run()

    assert harness.releases == [0, 30]
    harness.worker.process_next.assert_called_once_with()


def test_unload_failure_is_retried_without_stopping_the_worker(harness):
    attempts = 0

    def release():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("model cleanup failed")
        harness.record_release()

    harness.worker.release_engine.side_effect = release

    harness.run()

    assert attempts == 3
    assert harness.releases == [15, 30]


def test_loop_failure_still_releases_the_model(harness):
    harness.worker.reclaim_stale.side_effect = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        harness.run()

    assert harness.releases == [0]


def test_processing_error_does_not_prevent_idle_model_release(harness):
    outcomes = iter([RuntimeError("database unavailable")])

    def process():
        outcome = next(outcomes, None)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    harness.worker.process_next.side_effect = process

    harness.run()

    assert harness.releases == [10, 30]


def test_failed_article_restarts_the_idle_timer_after_earlier_empty_polls(harness):
    harness.stop_at = 140
    failed = False

    def process():
        nonlocal failed
        if harness.now == 10 and not failed:
            failed = True
            harness.now += 100
            raise RuntimeError("could not persist generated episode")

    harness.worker.process_next.side_effect = process

    harness.run()

    assert harness.releases == [120, 140]
