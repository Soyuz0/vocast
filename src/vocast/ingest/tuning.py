"""CPU tuning for concurrent synthesis.

Numeric libraries default to using every core for a single operation, which is
right for one job and wrong for several: with N workers each spawning a full set
of compute threads, the machine ends up N-fold oversubscribed and throughput
collapses while CPU still reads as fully busy.
"""

from __future__ import annotations

import os

from .logs import get_logger, kv

log = get_logger("tuning")


def available_cpus() -> int:
    """CPUs this process may actually use, honouring a cgroup quota.

    os.cpu_count() reports the host's cores, so under `docker --cpus=4` on a
    6-core machine it overstates what is available by 50%.
    """
    quota = _cgroup_quota()
    if quota:
        return quota
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def _cgroup_quota() -> int | None:
    # cgroup v2 exposes "max period" or "quota period" in one file.
    try:
        with open("/sys/fs/cgroup/cpu.max") as handle:
            raw = handle.read().split()
    except OSError:
        return None
    if len(raw) != 2 or raw[0] == "max":
        return None
    try:
        quota, period = int(raw[0]), int(raw[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, quota // period)


def threads_per_worker(concurrency: int, configured: int | None = None) -> int:
    if configured is not None:
        return max(1, configured)
    return max(1, available_cpus() // max(1, concurrency))


def apply_compute_threads(concurrency: int, configured: int | None = None) -> int:
    """Cap each worker's share of compute threads. Returns the value applied.

    Must run before the TTS engine is constructed: thread pools are sized on
    first use and cannot be shrunk afterwards.
    """
    threads = threads_per_worker(concurrency, configured)
    # Set for any library that reads these at import time.
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(var, str(threads))
    try:
        import torch

        torch.set_num_threads(threads)
    except (ImportError, RuntimeError) as exc:
        log.debug("could not set torch thread count %s", kv(error=exc))
    log.info(
        "compute threads capped %s",
        kv(per_worker=threads, workers=concurrency, cpus=available_cpus()),
    )
    return threads
