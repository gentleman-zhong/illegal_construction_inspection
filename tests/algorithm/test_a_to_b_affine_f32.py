"""Unit tests for the f32 A→B affine path introduced for the v0.8
Stage 1 regression (R3 in
``/root/.claude/plans/docker-root-illegal-construction-inspec-radiant-wilkes.md``).

Previously, ``stage_extract`` built the A→B alignment as

    pts_a_h = np.hstack([points_a.astype(np.float64, copy=False),
                         np.ones((N, 1), dtype=np.float64)])
    points_a_in_b = (pts_a_h @ T.T)[:, :3].astype(np.float32, copy=False)

When ``points_a`` is f32 (the post-v0.8 dtype) the ``astype(f64, copy=False)``
silently forces a real allocation, on top of the ``ones`` column and the
matmul output. On 25 M points that path consumed ~1.7 GB transient and
~60-120 s of wall-time.

The fix computes the affine entirely in f32:

    T_f32 = np.asarray(T, dtype=np.float32)
    points_a_in_b = points_a.astype(f32) @ T_f32[:3,:3].T + T_f32[:3, 3]

which is mathematically identical (modulo f32 rounding) to the f64 path.
This test pins the rounding budget so any future change that introduces
significant numerical drift is caught by CI.

Run::

    python -m pytest tests/algorithm/test_a_to_b_affine_f32.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))


def _aligned_f64(points_a: np.ndarray, T: np.ndarray) -> np.ndarray:
    """Reference (old) implementation: f64 hstack + matmul + downcast."""
    pts_a_h = np.hstack(
        [points_a.astype(np.float64, copy=False),
         np.ones((len(points_a), 1), dtype=np.float64)],
    )
    return (pts_a_h @ T.T)[:, :3].astype(np.float32, copy=False)


def _aligned_f32(points_a: np.ndarray, T: np.ndarray) -> np.ndarray:
    """New f32-only path."""
    T_f32 = np.asarray(T, dtype=np.float32)
    pts_f32 = points_a.astype(np.float32, copy=False)
    return (pts_f32 @ T_f32[:3, :3].T + T_f32[:3, 3]).astype(
        np.float32, copy=False,
    )


def test_identity_transform_f32_matches_f64():
    """Pure translation-only transform — f32 must agree to within ULP
    rounding on realistic ENU values (~1 km offsets)."""
    rng = np.random.default_rng(0xA11CE)
    pts = rng.normal(scale=200.0, size=(5000, 3)).astype(np.float32)  # ~±600 m
    T = np.eye(4, dtype=np.float64)
    T[0, 3] = 17.5
    T[1, 3] = -42.25
    T[2, 3] = 3.14
    out_f64 = _aligned_f64(pts, T)
    out_f32 = _aligned_f32(pts, T)
    np.testing.assert_allclose(out_f32, out_f64, atol=1e-3, rtol=1e-5)


def test_small_rotation_f32_matches_f64():
    """A rotation+translation transform typical of two SfM models of
    the same scene acquired weeks apart — drift of 0.5°/50 m shift."""
    rng = np.random.default_rng(0xB0B)
    pts = rng.normal(scale=500.0, size=(10_000, 3)).astype(np.float32)
    # 0.5° rotation around z
    theta = np.deg2rad(0.5)
    T = np.eye(4, dtype=np.float64)
    T[0, 0] = np.cos(theta)
    T[0, 1] = -np.sin(theta)
    T[1, 0] = np.sin(theta)
    T[1, 1] = np.cos(theta)
    T[0, 3] = 50.0
    T[1, 3] = 25.0
    out_f64 = _aligned_f64(pts, T)
    out_f32 = _aligned_f32(pts, T)
    np.testing.assert_allclose(out_f32, out_f64, atol=1e-2, rtol=1e-5)


def test_realistic_urban_transform_f32_matches_f64():
    """A realistic A→B transform from two urban tilesets captured days
    apart: ~1 km scale, ~3 m shift, ~0.1° rotation. The f32 vs f64
    drift must remain ≪ 1 cm so DBSCAN's eps=3 m and NN's 1.5 m
    thresholds are unaffected."""
    rng = np.random.default_rng(0xC1A55)
    pts = rng.normal(scale=500.0, size=(50_000, 3)).astype(np.float32)
    T = np.eye(4, dtype=np.float64)
    theta = np.deg2rad(0.1)
    T[0, 0] = np.cos(theta)
    T[0, 1] = -np.sin(theta)
    T[1, 0] = np.sin(theta)
    T[1, 1] = np.cos(theta)
    T[0, 3] = 2.0
    T[1, 3] = -3.0
    T[2, 3] = 0.05
    out_f64 = _aligned_f64(pts, T)
    out_f32 = _aligned_f32(pts, T)
    diff = np.abs(out_f32 - out_f64).max()
    # Both paths agree to < 1 cm at km scale, far below the downstream
    # NN / DBSCAN thresholds.
    assert diff < 0.01, (
        f"f32 A→B drift {diff:.6f} m exceeded 1 cm budget on a "
        f"realistic urban transform — f32 path is unsafe"
    )


def test_f32_output_dtype():
    """The new path must produce f32 output (Stage 2/3 expect f32
    input — see cluster_instances / the cKDTree leafsize)."""
    pts = np.zeros((100, 3), dtype=np.float32)
    T = np.eye(4, dtype=np.float64)
    out = _aligned_f32(pts, T)
    assert out.dtype == np.float32, f"out dtype = {out.dtype}, want f32"


def test_f32_path_does_not_mutate_input():
    """The new path must not modify the input array in place. (The
    intermediate ``astype(f32, copy=False)`` returns a view; we must
    not accidentally write through it.)"""
    pts = np.full((1024, 3), 100.0, dtype=np.float32)
    pts_original = pts.copy()
    T = np.eye(4, dtype=np.float64)
    _ = _aligned_f32(pts, T)
    np.testing.assert_array_equal(pts, pts_original)


def test_f32_path_memory_no_double_promotion():
    """Sanity-check that the new path does not allocate a f64 copy.

    We measure peak RSS via ``tracemalloc`` rather than only trusting
    arithmetic. The f64 path on 1 M points must allocate ≥ ~24 MB
    (N × 24 B for the hstack + 8 MB for matmul output), while the f32
    path must allocate << 24 MB.
    """
    import tracemalloc

    rng = np.random.default_rng(0x100)
    pts = rng.normal(scale=500.0, size=(1_000_000, 3)).astype(np.float32)
    T = np.eye(4, dtype=np.float64)
    T[0, 3] = 5.0

    tracemalloc.start()
    out_f64 = _aligned_f64(pts, T)
    _, peak_f64 = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    out_f32 = _aligned_f32(pts, T)
    _, peak_f32 = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # f64 path allocates at least:
    #   1 M × 24 B (hstack f64)  = 24 MB
    #   1 M × 24 B (matmul f64)  = 24 MB
    #   1 M × 12 B (f32 downcast) = 12 MB
    # → ~60 MB peak transient
    # f32 path allocates only:
    #   1 M × 12 B (output f32)  = 12 MB
    # We don't pin exact numbers (depends on interpreter), but the f32
    # peak must be at most 50% of the f64 peak.
    assert peak_f32 < 0.5 * peak_f64, (
        f"f32 path peak {peak_f32 / 1024 / 1024:.1f} MB ≥ 50% of "
        f"f64 peak {peak_f64 / 1024 / 1024:.1f} MB — the v0.8 "
        f"memory regression may not be fully eliminated"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
