"""Unit tests for the float32 rewrite of filter_vegetation.compute_exg.

Validates the memory-optimization rewrite (Priority 3) doesn't shift the
vegetation gate by more than f32 round-off. The EXG threshold is 0.05;
f32 noise of ~1e-7 is well below that, so a regression in compute_exg
that flips even a few points would be a hard fail.

We compare the new f32 path against a straightforward f64 reference
(``(2*g - r - b) / (r+g+b)`` element-wise) on a small synthetic input.

Run::

    python -m pytest tests/algorithm/test_compute_exg_f32.py -v
"""
from __future__ import annotations

import numpy as np
import sys
from pathlib import Path

# Make the algorithm package importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

from filter_vegetation import compute_exg  # noqa: E402


def _reference_exg_f64(r16: np.ndarray, g16: np.ndarray,
                       b16: np.ndarray) -> np.ndarray:
    """Float64 reference: (2*g - r - b) / (r + g + b)."""
    r = r16.astype(np.float64) / 65535.0
    g = g16.astype(np.float64) / 65535.0
    b = b16.astype(np.float64) / 65535.0
    s = np.maximum(r + g + b, 1e-6)
    return (g * 2.0 - r - b) / s


def test_compute_exg_matches_f64_reference_random():
    """f32 path should agree with f64 reference to <1e-6 absolute
    on random uint16 inputs."""
    rng = np.random.default_rng(0xDEADBEEF)
    r = rng.integers(0, 65536, size=10_000, dtype=np.uint16)
    g = rng.integers(0, 65536, size=10_000, dtype=np.uint16)
    b = rng.integers(0, 65536, size=10_000, dtype=np.uint16)

    f32 = compute_exg(r, g, b)
    f64 = _reference_exg_f64(r, g, b)

    # f32 mantissa is ~7 decimal digits; threshold for vegetation
    # detection is 0.05, so a 1e-6 abs error is 50,000x below the
    # gate — the gate cannot flip on numerical noise.
    max_abs = float(np.max(np.abs(f32 - f64)))
    assert max_abs < 1e-6, f"f32 vs f64 ExG abs error {max_abs:.2e} >= 1e-6"


def test_compute_exg_all_zeros():
    """ExG of all-black pixels should be a finite array of zeros
    (the max(s, 1e-6) prevents div-by-zero)."""
    z = np.zeros(100, dtype=np.uint16)
    out = compute_exg(z, z, z)
    assert out.shape == (100,)
    assert np.all(np.isfinite(out))
    # 2*0 - 0 - 0 = 0, divided by 1e-6 = 0 (numerator zero)
    assert np.allclose(out, 0.0, atol=1e-9)


def test_compute_exg_green_dominant_positive():
    """A green-dominant pixel should produce a positive ExG (the
    vegetation gate fires on ExG >= 0.05)."""
    # Pure green (g = 65535, r = b = 0) → ExG = (2*1 - 0 - 0)/1 = 2.0
    r = np.array([0], dtype=np.uint16)
    g = np.array([65535], dtype=np.uint16)
    b = np.array([0], dtype=np.uint16)
    out = compute_exg(r, g, b)
    assert out[0] > 1.99, f"expected ExG ~2.0 for pure green, got {out[0]}"


def test_compute_exg_red_dominant_negative():
    """A red-dominant pixel should produce a negative ExG."""
    r = np.array([65535], dtype=np.uint16)
    g = np.array([0], dtype=np.uint16)
    b = np.array([0], dtype=np.uint16)
    out = compute_exg(r, g, b)
    # (0 - 1 - 0) / 1 = -1
    assert out[0] < -0.99, f"expected ExG ~-1.0 for pure red, got {out[0]}"


def test_compute_exg_dtype_is_f32():
    """The new path should return float32 (saves 3.4 GiB peak vs f64
    on B's 50 M points). This is the primary memory benefit being
    protected against regression."""
    r = np.array([0, 100, 200], dtype=np.uint16)
    g = np.array([100, 200, 0], dtype=np.uint16)
    b = np.array([200, 0, 100], dtype=np.uint16)
    out = compute_exg(r, g, b)
    assert out.dtype == np.float32, f"expected float32, got {out.dtype}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
