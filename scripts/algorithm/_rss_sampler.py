"""Background sampler that records peak RSS for the algorithm subprocess.

Why
---
The cgroup OOM-killer terminates processes with SIGKILL — no Python
exception, no ``error.log`` line, just a bare ``exit code -9``. To
post-mortem diagnose "did we hit the cap? by how much?", we sample
``/proc/self/status`` once per second on a daemon thread and record
the peak ``VmRSS`` seen across the run. The peak is logged at INFO
when ``stop()`` is called, giving the service log a single line
like::

    [rss] peak RSS = 6.3 GiB

…that future OOMs can be compared against.

How
---
* ``start(interval_s=1.0)`` — kicks a daemon thread that polls
  ``/proc/self/status`` until ``stop()`` is called.
* ``stop()`` — sets the stop flag and logs the peak. Safe to call
  even if ``start()`` was never called (no-op).

The sampler is read-only — no I/O on a hot path beyond one tiny file
open per second — and the daemon is True so the subprocess exit path
is not blocked.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("rss")

_stop = threading.Event()
_peak_bytes: list[int] = [0]
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def start(interval_s: float = 1.0) -> None:
    """Begin sampling VmRSS in a daemon thread.

    Idempotent: a second ``start()`` is a no-op (the existing thread
    keeps running)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _peak_bytes[0] = 0
        t = threading.Thread(
            target=_loop, args=(interval_s,),
            daemon=True, name="rss-sampler",
        )
        t.start()
        _thread = t


def stop() -> None:
    """Stop the sampler and emit a final ``[rss] peak RSS = X.X GiB``
    line at INFO. Safe to call when ``start()`` was never invoked."""
    _stop.set()
    peak = _peak_bytes[0]
    if peak > 0:
        log.info("[rss] peak RSS = %.1f GiB", peak / 1024 / 1024)


def peak_gib() -> float:
    """Return the running peak RSS in GiB (for tests / introspection)."""
    return _peak_bytes[0] / 1024 / 1024


def _loop(interval_s: float) -> None:
    """Daemon thread body. Polls ``/proc/self/status`` until ``_stop`` is set."""
    # /proc is Linux-only; on macOS / Windows we silently no-op rather
    # than crashing the algorithm. The function returns cleanly so the
    # daemon thread is GC'd when stop() is called.
    try:
        while not _stop.is_set():
            try:
                with open("/proc/self/status", "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            # Format: "VmRSS:     12345 kB"
                            kb = int(line.split()[1])
                            _peak_bytes[0] = max(_peak_bytes[0], kb)
                            break
            except (OSError, ValueError):
                # /proc not mounted (containers without it), file got
                # rotated, or line format changed — just skip this tick.
                pass
            _stop.wait(interval_s)
    except Exception:  # pragma: no cover  (defensive)
        log.exception("[rss] sampler crashed (peak will be partial)")
