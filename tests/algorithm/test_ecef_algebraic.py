"""Regression test for the ECEF algebraic rewrite in ``run_pipeline.stage_convert``.

The v0.8 memory-optimization pass replaced the original

    homog = np.hstack([pts_diff, np.ones((N, 1))])
    ecef  = (homog @ T.T)[:, :3]

with the equivalent affine split

    ecef = pts_diff @ T[:3, :3].T + T[3, :3]

but ``T[3, :3]`` is the homogeneous row of the column-major transform
(``[0, 0, 0]``), **not** the translation column. The translation is at
``T[:3, 3]`` — the ECEF coordinates of the local origin (millions of
meters for Shanghai). Using ``T[3, :3]`` drops the entire ECEF
translation, offsetting every output ECEF by the local origin's ECEF
coordinates (~4,651 km for Shanghai).

This test asserts the corrected algebraic form

    ecef = pts @ T[:3, :3].T + T[:3, 3]

is **bit-exact** with the original ``(homog @ T.T)[:, :3]`` form across
identity, realistic, and random transform configurations, and also
asserts the magnitude of the corrected output is in the ECEF range
(millions of meters), not local metres.

Run::

    python -m pytest tests/algorithm/test_ecef_algebraic.py -v
"""
from __future__ import annotations

import numpy as np


# ----- helpers (mirror exactly what run_pipeline.stage_convert does) -----


def _make_column_major_transform(R: np.ndarray, t: np.ndarray) -> list[float]:
    """Return a column-major flat 16-element transform array (3D Tiles spec)."""
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T.flatten(order="F").tolist()


def _to_T(transform_b) -> np.ndarray:
    """Mirror run_pipeline's ``np.asarray(...).reshape(4, 4, order="F")``."""
    return np.asarray(transform_b, dtype=np.float64).reshape(4, 4, order="F")


def _ecef_old(pts: np.ndarray, T: np.ndarray) -> np.ndarray:
    """Original (homog @ T.T)[:, :3] form — the ground truth."""
    homog = np.hstack([pts, np.ones((len(pts), 1))])
    return (homog @ T.T)[:, :3]


def _ecef_fix(pts: np.ndarray, T: np.ndarray) -> np.ndarray:
    """Corrected algebraic form — ``T[:3, 3]`` (translation column)."""
    return pts @ T[:3, :3].T + T[:3, 3]


def _ecef_bug(pts: np.ndarray, T: np.ndarray) -> np.ndarray:
    """Buggy form — ``T[3, :3]`` (homogeneous row = [0,0,0]). For control."""
    return pts @ T[:3, :3].T + T[3, :3]


# ----- tests -----


def test_identity_transform():
    """Identity rotation, small translation. FIX must equal OLD bit-exactly."""
    R = np.eye(3)
    t = np.array([10.0, 20.0, 30.0])
    T = _to_T(_make_column_major_transform(R, t))
    pts = np.array([[1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0]])

    fix = _ecef_fix(pts, T)
    old = _ecef_old(pts, T)

    np.testing.assert_array_equal(fix, old)
    # Sanity: with the identity rotation, ECEF == local + translation.
    np.testing.assert_allclose(fix, pts + t)


def test_shanghai_realistic_transform():
    """Realistic Shanghai ECEF transform (~4.5 Mm translation).

    Pins the failure mode: the buggy form yields coordinates in the tens
    of meters (just the local points), while the corrected form yields
    coordinates in the millions.
    """
    R = np.array([[0.7648, 0.0, 0.6442],
                  [0.0,    1.0, 0.0],
                  [-0.6442, 0.0, 0.7648]])
    t = np.array([-2.851e6, 4.651e6, 3.280e6])  # Shanghai ECEF origin
    T = _to_T(_make_column_major_transform(R, t))
    pts = np.array([[100.0, 50.0, 5.0],
                    [-200.0, 30.0, 10.0]])

    fix = _ecef_fix(pts, T)
    old = _ecef_old(pts, T)
    bug = _ecef_bug(pts, T)

    np.testing.assert_array_equal(fix, old)

    # Magnitudes must be in ECEF range (millions of meters).
    assert np.abs(fix).max() > 1e6, (
        f"FIX gives |ECEF|={np.abs(fix).max():.0f} m — not ECEF range; "
        f"the fix regressed to the bug."
    )

    # And demonstrate the buggy form indeed fails the same assertion.
    assert np.abs(bug).max() < 1e3, (
        f"BUGGY form |ECEF|={np.abs(bug).max():.0f} m — the original bug "
        f"is dropped here too; the regression test is set up wrong."
    )

    # Quantify the bug: |fix - bug| must be ~|t|, not 0.
    diff = np.abs(fix - bug).max()
    assert diff > 1e6, (
        f"|fix - bug|={diff:.0f} m — expected millions; the two "
        f"forms are too close, the bug control is broken."
    )


def test_random_transforms():
    """Bit-exact equivalence across 50 random rigid transforms + 100 random pts."""
    rng = np.random.default_rng(42)
    n_failed = 0
    for _ in range(50):
        # Random rotation via QR.
        A = rng.standard_normal((3, 3))
        Q, _ = np.linalg.qr(A)
        if np.linalg.det(Q) < 0:
            Q[:, 0] = -Q[:, 0]
        t = rng.uniform(-5e6, 5e6, size=3)
        T = _to_T(_make_column_major_transform(Q, t))
        pts = rng.standard_normal((100, 3)) * 100

        fix = _ecef_fix(pts, T)
        old = _ecef_old(pts, T)
        if not np.array_equal(fix, old):
            n_failed += 1
    assert n_failed == 0, (
        f"ECEF algebraic form diverged from original on {n_failed}/50 "
        f"random transforms — bit-exact equivalence is broken."
    )


def test_buggy_form_differs_from_fix():
    """Pin-point control test: the buggy form (``T[3, :3]``) DOES differ
    from the fix on at least one transform. Catches a test setup where
    ``_ecef_bug`` is silently made equivalent to ``_ecef_fix``.
    """
    R = np.eye(3)
    t = np.array([-2.851e6, 4.651e6, 3.280e6])
    T = _to_T(_make_column_major_transform(R, t))
    pts = np.array([[100.0, 50.0, 5.0]])

    fix = _ecef_fix(pts, T)
    bug = _ecef_bug(pts, T)

    # BUG = pts (no translation), FIX = pts + t.  Diff must equal |t|.
    np.testing.assert_allclose(bug, pts, atol=1e-9)
    np.testing.assert_allclose(fix, pts + t, atol=1e-9)
    assert np.abs(fix - bug).max() > 1e6
