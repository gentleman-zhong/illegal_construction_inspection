"""Unit test for the EXTRACT_MAX_WORKERS default bound introduced for
the v0.8 Stage 1 regression (R2 in
``/root/.claude/plans/docker-root-illegal-construction-inspec-radiant-wilkes.md``).

The previous default of ``8`` capped inner-pool concurrency to ~6% of
the machine on a 128-core box, which made Pass 2 wall-time scale with
worker count instead of CPU count. The default is now
``min(os.cpu_count() or 8, 64)`` — verified here both as a default and
as an env-var override.

Run::

    python -m pytest tests/algorithm/test_extract_workers.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

import algo_config  # noqa: E402


def test_default_bounded_by_cpu_count():
    """With no env-var set, ``EXTRACT_MAX_WORKERS`` must be at most
    ``os.cpu_count()`` and at most 64."""
    # Save and clear any inherited env var
    old = os.environ.pop("ALGO_EXTRACT_MAX_WORKERS", None)
    try:
        import importlib
        # Force re-evaluation of the module under different cpu_count values
        for fake_cpu in (4, 8, 16, 32, 64, 128):
            with mock.patch.object(os, "cpu_count", return_value=fake_cpu):
                importlib.reload(algo_config)
                w = algo_config.EXTRACT_MAX_WORKERS
                assert 1 <= w <= 64, (
                    f"cpu_count={fake_cpu}: got EXTRACT_MAX_WORKERS={w}, "
                    "must be in [1, 64]"
                )
                # When the fake CPU is small enough, we use it as-is.
                # When it's large, we're capped at 64.
                expected = min(fake_cpu, 64)
                assert w == expected, (
                    f"cpu_count={fake_cpu}: got {w}, expected {expected}"
                )
    finally:
        if old is not None:
            os.environ["ALGO_EXTRACT_MAX_WORKERS"] = old
        # Final reload to restore whatever env was set at import time
        importlib.reload(algo_config)


def test_env_var_overrides_default():
    """``ALGO_EXTRACT_MAX_WORKERS`` env var overrides the auto default."""
    cases = [("1", 1), ("8", 8), ("32", 32), ("64", 64)]
    for raw, want in cases:
        os.environ["ALGO_EXTRACT_MAX_WORKERS"] = raw
        try:
            import importlib
            importlib.reload(algo_config)
            assert algo_config.EXTRACT_MAX_WORKERS == want, (
                f"env ALGO_EXTRACT_MAX_WORKERS={raw}: got "
                f"{algo_config.EXTRACT_MAX_WORKERS}, want {want}"
            )
        finally:
            os.environ.pop("ALGO_EXTRACT_MAX_WORKERS", None)
    importlib.reload(algo_config)


def test_no_cpu_count_falls_back_to_eight():
    """When ``os.cpu_count()`` returns ``None`` (e.g. exotic containers),
    the default falls back to ``8`` rather than crashing."""
    old = os.environ.pop("ALGO_EXTRACT_MAX_WORKERS", None)
    try:
        import importlib
        with mock.patch.object(os, "cpu_count", return_value=None):
            importlib.reload(algo_config)
        # Falls back to 8 (the documented fallback)
        assert algo_config.EXTRACT_MAX_WORKERS == 8, (
            f"cpu_count=None: got {algo_config.EXTRACT_MAX_WORKERS}, "
            "want 8 (documented fallback)"
        )
    finally:
        if old is not None:
            os.environ["ALGO_EXTRACT_MAX_WORKERS"] = old
        importlib.reload(algo_config)


def test_exposed_in_all():
    """``algo_config.EXTRACT_MAX_WORKERS`` must be importable from the
    package surface so other modules (point_cloud_extraction, run_pipeline)
    can read it directly."""
    from algo_config import __all__
    assert "EXTRACT_MAX_WORKERS" in __all__


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
