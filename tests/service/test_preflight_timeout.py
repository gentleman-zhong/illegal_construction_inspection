"""Tests for the timeout-bounded pre-flight in ``api_server``.

Two regression cases:

1. **NFS / kernel-D hang repro.** ``_preflight_with_timeout`` must
   return ``{"status": "timeout"}`` when the underlying scan blocks
   longer than ``timeout_s``, instead of blocking the FastAPI handler
   thread forever. The pre-flight already gave us 11+ minute hangs in
   production — see OPERATIONS.md for the incident.

2. **Exception isolation.** When the inner scan raises, the helper
   must surface the exception (``status == "error"``) instead of
   re-raising into the request handler.

3. **Happy path.** When the inner scan returns quickly, the helper
   returns ``status == "ok"`` with the correct count.

These tests use ``monkeypatch`` to inject a stub for
``_count_b3dm_vertices`` rather than touching real b3dm files —
the contract we're locking in is "the wrapper survives slow / broken
scans", not the scan itself.

Run::

    python -m pytest tests/service/test_preflight_timeout.py -v
"""
from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest


# We test the helper in isolation. The helper lives in
# ``scripts/service/api_server.py``; import via path manipulation so
# the test doesn't depend on the FastAPI app being constructed (which
# pulls in OSS config / network calls).
import sys
_SERVICE = Path(__file__).resolve().parent.parent.parent / "scripts" / "service"
_ALGO = _SERVICE.parent / "algorithm"
sys.path.insert(0, str(_SERVICE))


@pytest.fixture(scope="module")
def api_server_module():
    """Lazy-import api_server with a stubbed pre-flight detector.

    We monkeypatch the inner scan via ``find_leaf_b3dms_with_bbox``
    and ``b3dm_position_count`` so that the real imports do not have
    to succeed (this test only cares about the wrapper).
    """
    # Pre-set the env so api_server's "pre-flight OK" branch executes
    # a no-op scan against our stub.  We don't actually start uvicorn;
    # we only import the module to grab the helper.
    import importlib
    if "api_server" in sys.modules:
        del sys.modules["api_server"]
    return importlib.import_module("api_server")


def test_preflight_returns_ok_when_scan_is_fast(api_server_module, monkeypatch):
    """A fast inner scan returns status=ok with the right n_b."""
    sentinel = 42

    def fake_count(base_path):
        return sentinel

    monkeypatch.setattr(api_server_module, "_count_b3dm_vertices", fake_count)
    out = api_server_module._preflight_with_timeout(Path("/tmp"), timeout_s=2.0)
    assert out["status"] == "ok"
    assert out["n_b"] == sentinel


def test_preflight_returns_timeout_when_scan_hangs(api_server_module, monkeypatch):
    """A scan that blocks longer than timeout returns status=timeout.

    This is the NFS/RPC kernel-state-D regression: the underlying
    ``open()/read()`` cannot be interrupted by Python signals, so we
    bound the call via ``thread.join(timeout_s)`` and degrade
    fail-open when the budget is exceeded. Without this guard the
    submit handler would block indefinitely and every subsequent
    GET would return ``taskId not found``.
    """
    import threading

    started = threading.Event()
    release = threading.Event()

    def hanging_scan(base_path):
        # Mimic a stuck RPC: the worker thread parks on an Event the
        # test never sets. Verifies the wrapper doesn't wait for it.
        started.set()
        release.wait(timeout=30)
        return 0  # unreachable under timeout

    monkeypatch.setattr(api_server_module, "_count_b3dm_vertices", hanging_scan)

    t0 = time.monotonic()
    out = api_server_module._preflight_with_timeout(
        Path("/tmp"), timeout_s=0.3,
    )
    elapsed = time.monotonic() - t0

    assert out["status"] == "timeout", out
    # Should return in ~0.3s, definitely not 30s.
    assert elapsed < 1.0, f"timeout took {elapsed:.2f}s (>1s budget exceeded)"
    # Worker did actually start (so we know we tested the real branch).
    assert started.wait(timeout=1.0)


def test_preflight_returns_error_when_scan_raises(api_server_module, monkeypatch):
    """An inner exception is captured (not propagated to the caller)."""

    class _Boom(RuntimeError):
        pass

    def raising_scan(base_path):
        raise _Boom("bad b3dm header")

    monkeypatch.setattr(api_server_module, "_count_b3dm_vertices", raising_scan)
    out = api_server_module._preflight_with_timeout(Path("/tmp"), timeout_s=2.0)
    assert out["status"] == "error", out
    assert isinstance(out["exc"], _Boom)
    assert "bad b3dm header" in str(out["exc"])


def test_preflight_worker_thread_is_daemon(api_server_module, monkeypatch):
    """The leaked scan thread (on timeout) MUST be daemon so it doesn't
    block uvicorn shutdown. The helper is allowed to "leak" a stuck
    thread on NFS wedged stalls; daemon=True is what makes that safe.
    """
    import threading
    seen: dict = {}

    def hanging_scan(base_path):
        seen["thread"] = threading.current_thread()
        time.sleep(5)
        return 0

    monkeypatch.setattr(api_server_module, "_count_b3dm_vertices", hanging_scan)
    api_server_module._preflight_with_timeout(Path("/tmp"), timeout_s=0.2)
    # Give the OS a moment for the worker to actually start.
    time.sleep(0.05)
    assert "thread" in seen, "worker never started"
    assert seen["thread"].daemon, (
        "leaked pre-flight worker is non-daemon — would block uvicorn shutdown"
    )


def test_algo_disable_oom_preflight_env_disables_gate(monkeypatch):
    """``ALGO_DISABLE_OOM_PREFLIGHT=1`` must flip ``_PREFLIGHT_OK`` to False
    even when the underlying memory-estimator imports succeed.

    Regression for the operator opt-out: turning off the submit-time
    OOM early-reject so city-scale b3dm scans don't burn 30s on every
    submit (the gate degrades fail-open anyway; the in-pipeline check
    in ``stage_convert`` is the real safety).
    """
    monkeypatch.setenv("ALGO_DISABLE_OOM_PREFLIGHT", "1")
    # Force a fresh import so the module-level branch re-evaluates.
    if "api_server" in sys.modules:
        del sys.modules["api_server"]
    mod = importlib.import_module("api_server")
    assert mod._PREFLIGHT_OK is False, (
        "ALGO_DISABLE_OOM_PREFLIGHT=1 did not disable the submit-time "
        "OOM gate; check the env parse branch in api_server."
    )
