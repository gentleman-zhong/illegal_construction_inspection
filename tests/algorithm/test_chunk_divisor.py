"""Regression test for v0.8 Stage 1 change #10 — ``EXTRACT_CHUNK_DIVISOR``
default changed from 16 → 8 (v0.8 regression plan, change #10).

The chunksize in Pass 1+2 is computed as::

    chunksize = max(1, len(leaves) // (n_workers * EXTRACT_CHUNK_DIVISOR))

On a 128-core host with 64 workers and 4576 leaves:
- divisor=16 → chunksize=4 → 64×(4576/4) ≈ 73 k IPC trips
- divisor=8  → chunksize=8 → 64×(4576/8) ≈ 37 k IPC trips (half)

Pass 2 was already 5.4 s on the b-class tileset at divisor=16; the
benefit here is small (~1 s) but each IPC trip has variance on NFS,
so halving the count tightens the wall-time distribution.

Run::

    python -m pytest tests/algorithm/test_chunk_divisor.py -v
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


def test_chunk_divisor_default_is_eight():
    """With no env var set, ``EXTRACT_CHUNK_DIVISOR`` must default to 8.

    This pins the v0.8 change #10 — raising from 16 to 8 halves the IPC
    trip count in Pass 1+2.
    """
    old = os.environ.pop("ALGO_EXTRACT_CHUNK_DIVISOR", None)
    try:
        import importlib
        importlib.reload(algo_config)
        assert algo_config.EXTRACT_CHUNK_DIVISOR == 8, (
            f"EXTRACT_CHUNK_DIVISOR default is "
            f"{algo_config.EXTRACT_CHUNK_DIVISOR}, expected 8 "
            f"(v0.8 plan change #10)"
        )
    finally:
        if old is not None:
            os.environ["ALGO_EXTRACT_CHUNK_DIVISOR"] = old
        importlib.reload(algo_config)


def test_chunk_divisor_env_overrides_default():
    """``ALGO_EXTRACT_CHUNK_DIVISOR`` env var still overrides."""
    cases = [("4", 4), ("8", 8), ("16", 16), ("32", 32)]
    for raw, want in cases:
        os.environ["ALGO_EXTRACT_CHUNK_DIVISOR"] = raw
        try:
            import importlib
            importlib.reload(algo_config)
            assert algo_config.EXTRACT_CHUNK_DIVISOR == want, (
                f"env ALGO_EXTRACT_CHUNK_DIVISOR={raw}: got "
                f"{algo_config.EXTRACT_CHUNK_DIVISOR}, want {want}"
            )
        finally:
            os.environ.pop("ALGO_EXTRACT_CHUNK_DIVISOR", None)
    importlib.reload(algo_config)


def test_chunk_divisor_produces_smaller_chunksize_on_b_class_tileset():
    """Sanity: on the b-class shape (4576 leaves × 64 workers), the new
    default 8 yields chunksize=9 (vs old default 16 → chunksize=4).
    Just a numerical check."""
    n_leaves = 4576
    n_workers = 64
    old_div = 16
    new_div = 8
    old_chunk = max(1, n_leaves // (n_workers * old_div))
    new_chunk = max(1, n_leaves // (n_workers * new_div))
    assert new_chunk > old_chunk, (
        f"new divisor should yield bigger chunksize: "
        f"old={old_chunk}, new={new_chunk}"
    )
    # IPC trip count drops
    old_trips = (n_leaves + old_chunk - 1) // old_chunk * n_workers
    new_trips = (n_leaves + new_chunk - 1) // new_chunk * n_workers
    assert new_trips < old_trips, (
        f"new divisor should reduce IPC trips: "
        f"old={old_trips}, new={new_trips}"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))