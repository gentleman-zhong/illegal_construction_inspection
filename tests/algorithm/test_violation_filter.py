"""Unit tests for the 两违 (illegal-occupation / illegal-construction)
post-filter inside ``cluster_instances``.

The filter runs after DBSCAN:

1. For every cluster, compute ``hag_min / hag_max / hag_mean`` from a
   per-point ``height_above_ground`` array aligned with ``xyz_enu``.
2. A cluster is **kept** iff ``hag_max <= HAG_MAX_LOW_M`` (ground
   occupation / ground-floor 违建) **or** ``hag_min >= HAG_MIN_HIGH_M``
   (rooftop 违建). Clusters spanning the (5, 20) m band, or containing
   non-finite values, are rejected (fail-closed).
3. Each kept cluster receives a Gaussian
   ``confidence_score = exp(-0.5 * ((n - peak) / sigma)^2)``.
4. The output ``id`` is the confidence_score-desc rank (ties broken by
   num_points desc, then original DBSCAN label asc).
5. The returned ``labels`` array has rejected clusters' ids remapped
   to ``-1`` so the caller's ``labels >= 0`` mask treats them as noise.

We exercise every branch with synthetic data: ground / rooftop / mid
scenarios, boundary values (5.0, 20.0 inclusive), NaN / inf fail-closed,
shape mismatches, ``confidence_sigma_n <= 0``, the legacy
``height_above_ground=None`` path, and the multi-cluster ranking
order. The voxel-decimation path is exercised by the production
``cluster_instances`` signature; we keep ``voxel_m=0`` here so tests
don't depend on it.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

from convert_point_ecef_and_3dtiles import cluster_instances  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _blob(center_xyz: tuple[float, float, float], n: int,
          radius: float = 1.0,
          z: float | None = None,
          z_range: tuple[float, float] | None = None,
          z_jitter: float = 0.3,
          *, rng: np.random.Generator | None = None) -> np.ndarray:
    """Generate ``n`` points around ``center_xyz`` (gaussian, scaled by
    ``radius``).

    If ``z`` is supplied, all points have z = ``z`` ± ``z_jitter``
    (small jitter so the cloud is not perfectly coplanar — open3d's
    ``compute_convex_hull`` segfaults on perfectly flat clouds).
    If ``z_range`` is supplied, z is uniform in that range.
    Otherwise z is gaussian noise around ``center_xyz[2]``.
    """
    if rng is None:
        rng = np.random.default_rng(0xC0FFEE)
    out = np.empty((n, 3), dtype=np.float64)
    out[:, 0] = center_xyz[0] + rng.normal(scale=radius, size=n)
    out[:, 1] = center_xyz[1] + rng.normal(scale=radius, size=n)
    if z is not None:
        out[:, 2] = z + rng.normal(scale=z_jitter, size=n)
    elif z_range is not None:
        out[:, 2] = rng.uniform(z_range[0], z_range[1], size=n)
    else:
        out[:, 2] = center_xyz[2] + rng.normal(scale=radius, size=n)
    return out


def _hag(pts: np.ndarray, z_offset: float = 0.0) -> np.ndarray:
    """Synth a height-above-ground array = pts[:, 2] - z_offset.
    Independent of the actual ENU-z jitter on the cluster — these are
    the values a real CSF-fitted DTM would have produced.
    """
    if z_offset == 0.0:
        # Build a synthetic hag directly from the user's stated scenario
        # — for the "ground" / "rooftop" tests below the cluster is at
        # the scenario altitude (e.g. 2.0 m), so hag = 2.0 ± 0 (within
        # tolerance). For "mid" tests we synthesise later per-test.
        return np.full(len(pts), 2.0, dtype=np.float64)
    return pts[:, 2].astype(np.float64) - z_offset


# ---------------------------------------------------------------------------
# Single-cluster scenario tests (use one well-defined blob)
# ---------------------------------------------------------------------------
def test_ground_cluster_kept():
    pts = _blob((0, 0, 2.0), 800, z=2.0)
    hag = np.full(800, 2.0, dtype=np.float64)
    clusters, labels, _ = cluster_instances(pts, pts, eps=3.0,
                                          min_points=50, voxel_m=0,
                                          height_above_ground=hag)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["scenario"] == "ground"
    assert c["passed_height_filter"] is True
    assert c["hag_max"] <= 5.0 + 1e-9
    assert (labels == 0).sum() == 800


def test_rooftop_cluster_kept():
    pts = _blob((0, 0, 22.0), 800, z=22.0)
    hag = np.full(800, 22.0, dtype=np.float64)
    clusters, labels, _ = cluster_instances(pts, pts, eps=3.0,
                                          min_points=50, voxel_m=0,
                                          height_above_ground=hag)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["scenario"] == "rooftop"
    assert c["passed_height_filter"] is True
    assert c["hag_min"] >= 20.0 - 1e-9
    assert (labels == 0).sum() == 800


def test_mid_cluster_rejected():
    """hag_max in (5, 20) and hag_min in (5, 20) → cluster spans the
    gap → rejected as 'mid'."""
    pts = _blob((0, 0, 10.0), 800, z_range=(3.0, 18.0))
    hag = np.full(800, 10.0, dtype=np.float64)
    clusters, labels, _ = cluster_instances(pts, pts, eps=3.0,
                                          min_points=50, voxel_m=0,
                                          height_above_ground=hag)
    assert len(clusters) == 0
    # All 800 points must be remapped to noise (-1)
    assert (labels == -1).sum() == 800


def test_boundary_low_inclusive():
    """hag_max exactly 5.0 → kept (inclusive ``<=``)."""
    pts = _blob((0, 0, 5.0), 500, z=5.0)
    hag = np.full(500, 5.0, dtype=np.float64)
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                      min_points=50, voxel_m=0,
                                      height_above_ground=hag)
    assert len(clusters) == 1
    assert clusters[0]["scenario"] == "ground"


def test_boundary_high_inclusive():
    """hag_min exactly 20.0 → kept (inclusive ``>=``)."""
    pts = _blob((0, 0, 20.0), 500, z=20.0)
    hag = np.full(500, 20.0, dtype=np.float64)
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                      min_points=50, voxel_m=0,
                                      height_above_ground=hag)
    assert len(clusters) == 1
    assert clusters[0]["scenario"] == "rooftop"


def test_just_above_boundary_rejected():
    """hag_max = 5.0001 → mid (just outside inclusive <=)."""
    pts = _blob((0, 0, 5.0001), 500, z=5.0001)
    hag = np.full(500, 5.0001, dtype=np.float64)
    clusters, labels, _ = cluster_instances(pts, pts, eps=3.0,
                                          min_points=50, voxel_m=0,
                                          height_above_ground=hag)
    assert len(clusters) == 0
    assert (labels == -1).sum() == 500


def test_nan_point_fails_closed():
    """A cluster containing one NaN point is rejected (fail-closed)."""
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    hag = np.full(500, 2.0, dtype=np.float64)
    hag[42] = float("nan")
    clusters, labels, _ = cluster_instances(pts, pts, eps=3.0,
                                          min_points=50, voxel_m=0,
                                          height_above_ground=hag)
    assert len(clusters) == 0
    assert (labels == -1).sum() == 500


def test_pos_inf_point_fails_closed():
    """+inf in hag is treated as non-finite → reject."""
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    hag = np.full(500, 2.0, dtype=np.float64)
    hag[100] = float("inf")
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                     min_points=50, voxel_m=0,
                                     height_above_ground=hag)
    assert len(clusters) == 0


def test_neg_inf_point_fails_closed():
    """-inf in hag is treated as non-finite → reject."""
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    hag = np.full(500, 2.0, dtype=np.float64)
    hag[100] = float("-inf")
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                     min_points=50, voxel_m=0,
                                     height_above_ground=hag)
    assert len(clusters) == 0


# ---------------------------------------------------------------------------
# Confidence score (Gaussian) tests — single cluster
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_pts, expected_score, tol", [
    (1100, 1.000,  0.001),     # peak, delta=0
    (800,  0.607,  0.005),     # delta=-1.0
    (1400, 0.607,  0.005),     # delta=+1.0
    (500,  0.135,  0.005),     # delta=-2.0
    (1700, 0.135,  0.005),     # delta=+2.0
    (1500, 0.411,  0.005),     # delta=+1.333
    (2000, 0.0111, 0.001),     # delta=+3.0
    (100,  0.0039, 0.001),     # delta=-3.333
])
def test_confidence_score_gaussian(n_pts, expected_score, tol):
    pts = _blob((0, 0, 2.0), n_pts, z=2.0)
    hag = np.full(n_pts, 2.0, dtype=np.float64)
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                      min_points=50, voxel_m=0,
                                      height_above_ground=hag)
    assert len(clusters) == 1
    assert abs(clusters[0]["confidence_score"] - expected_score) < tol, (
        f"n={n_pts}: got {clusters[0]['confidence_score']:.4f}, "
        f"expected {expected_score:.4f}"
    )


def test_confidence_score_field_present():
    pts = _blob((0, 0, 2.0), 1100, z=2.0)
    hag = np.full(1100, 2.0, dtype=np.float64)
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                      min_points=50, voxel_m=0,
                                      height_above_ground=hag)
    assert "confidence_score" in clusters[0]
    assert isinstance(clusters[0]["confidence_score"], float)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_length_mismatch_raises():
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    bad_hag = np.zeros(100, dtype=np.float64)
    with pytest.raises(ValueError, match="height_above_ground length"):
        cluster_instances(pts, pts, eps=3.0, min_points=50, voxel_m=0,
                         height_above_ground=bad_hag)


def test_ndim_2d_raises():
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    bad_hag = np.zeros((500, 1), dtype=np.float64)
    with pytest.raises(ValueError, match="height_above_ground must be 1-D"):
        cluster_instances(pts, pts, eps=3.0, min_points=50, voxel_m=0,
                         height_above_ground=bad_hag)


def test_sigma_zero_raises():
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    hag = np.full(500, 2.0, dtype=np.float64)
    with pytest.raises(ValueError, match="confidence_sigma_n must be > 0"):
        cluster_instances(pts, pts, eps=3.0, min_points=50, voxel_m=0,
                         height_above_ground=hag, confidence_sigma_n=0)


def test_sigma_negative_raises():
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    hag = np.full(500, 2.0, dtype=np.float64)
    with pytest.raises(ValueError, match="confidence_sigma_n must be > 0"):
        cluster_instances(pts, pts, eps=3.0, min_points=50, voxel_m=0,
                         height_above_ground=hag, confidence_sigma_n=-1.0)


# ---------------------------------------------------------------------------
# Legacy / backward-compat path (height_above_ground=None)
# ---------------------------------------------------------------------------
def test_legacy_none_keeps_all_clusters():
    """With height_above_ground=None the filter is skipped entirely:
    all clusters kept, sorted by num_points desc (legacy)."""
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    clusters, labels, _ = cluster_instances(pts, pts, eps=3.0,
                                          min_points=50, voxel_m=0)
    assert len(clusters) == 1
    c = clusters[0]
    # No new fields added when filter is disabled
    assert "scenario" not in c
    assert "confidence_score" not in c
    assert "hag_min" not in c
    # Labels not remapped (cluster still at label 0)
    assert (labels == 0).sum() == 500


# ---------------------------------------------------------------------------
# n_clusters_before_filter return value
# ---------------------------------------------------------------------------
def test_pre_filter_count_returned_when_filter_runs():
    """``cluster_instances`` returns the pre-filter DBSCAN cluster
    count (299 in the test run, vs 148 after filtering) so the caller
    can populate instances.json's ``n_clusters_before_height_filter``
    with the *unfiltered* total."""
    rng = np.random.default_rng(0xBEEF)
    g_low  = _blob((0,   0,   2.0),  900, z=2.0,  rng=rng)
    g_high = _blob((50,  0,   22.0), 1200, z=22.0, rng=rng)
    m1     = _blob((100, 0,   10.0), 900,  z_range=(3.0, 18.0), rng=rng)
    m2     = _blob((150, 0,   10.0), 1100, z_range=(3.0, 18.0), rng=rng)
    pts = np.vstack([g_low, g_high, m1, m2])
    hag = np.concatenate([
        np.full(900,  2.0,  dtype=np.float64),
        np.full(1200, 22.0, dtype=np.float64),
        np.full(900,  10.0, dtype=np.float64),
        np.full(1100, 10.0, dtype=np.float64),
    ])
    _, _, n_before = cluster_instances(pts, pts, eps=3.0,
                                        min_points=50, voxel_m=0,
                                        height_above_ground=hag)
    # All 4 blobs are one DBSCAN cluster each → pre-filter count = 4.
    assert n_before == 4


def test_pre_filter_count_equals_post_count_when_no_filter():
    """Legacy path (height_above_ground=None) — pre-filter == post-filter."""
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    _, _, n_before = cluster_instances(pts, pts, eps=3.0,
                                        min_points=50, voxel_m=0)
    assert n_before == 1   # one cluster, no filter run


def test_zero_dbscan_clusters_with_height_filter_returns_empty_result():
    """Sparse differences are a valid empty result, not a None-count error."""
    pts = _blob((0, 0, 20.0), 20, z=20.0)
    hag = np.full(20, 20.0, dtype=np.float64)
    clusters, labels, n_before = cluster_instances(
        pts, pts, eps=3.0, min_points=120, voxel_m=0,
        height_above_ground=hag,
        hag_max_low_m=-99999.0,
        hag_min_high_m=13.0,
    )
    assert clusters == []
    assert n_before == 0
    assert (labels == -1).all()


# ---------------------------------------------------------------------------
# Multi-cluster ranking + label remap
# ---------------------------------------------------------------------------
def test_four_clusters_mixed_scenario():
    """1 ground + 1 rooftop + 2 mid → only the first two are kept.
    id=1 is whichever has the higher confidence_score. Rejected
    clusters' points are remapped to -1.
    """
    rng = np.random.default_rng(0xBEEF)
    # Build 4 well-separated blobs; each is one DBSCAN cluster.
    g_low  = _blob((0,   0,   2.0),  900, z=2.0,  rng=rng)
    g_high = _blob((50,  0,   22.0), 1200, z=22.0, rng=rng)
    m1     = _blob((100, 0,   10.0), 900,  z_range=(3.0, 18.0), rng=rng)
    m2     = _blob((150, 0,   10.0), 1100, z_range=(3.0, 18.0), rng=rng)
    pts = np.vstack([g_low, g_high, m1, m2])
    n_g_low, n_g_high, n_m1, n_m2 = 900, 1200, 900, 1100
    hag = np.concatenate([
        np.full(n_g_low,  2.0,  dtype=np.float64),    # ground
        np.full(n_g_high, 22.0, dtype=np.float64),    # rooftop
        np.full(n_m1,     10.0, dtype=np.float64),    # mid
        np.full(n_m2,     10.0, dtype=np.float64),    # mid
    ])

    clusters, labels, _ = cluster_instances(pts, pts, eps=3.0,
                                          min_points=50, voxel_m=0,
                                          height_above_ground=hag)
    assert len(clusters) == 2
    scenarios = {c["scenario"] for c in clusters}
    assert scenarios == {"ground", "rooftop"}

    # Ranking: g_high (1200 pts) closer to peak 1100 than g_low (900)
    # → id=1 → rooftop.
    assert clusters[0]["scenario"] == "rooftop"
    assert clusters[1]["scenario"] == "ground"
    assert clusters[0]["id"] == 1
    assert clusters[1]["id"] == 2

    # Rejected clusters' points → labels -1
    n_rejected = n_m1 + n_m2
    assert (labels == -1).sum() == n_rejected

    # Original 0-based DBSCAN labels are preserved on dbscan_label
    dbscan_labels = sorted(c["dbscan_label"] for c in clusters)
    assert dbscan_labels == [0, 1]   # the first two blobs' labels


def test_id_is_ranked_by_confidence_score():
    """When two clusters have very different num_points, the one closer
    to the peak (1100) wins id=1."""
    rng = np.random.default_rng(0xCAFE)
    big_ground   = _blob((0,  0,  2.0), 1500, z=2.0, rng=rng)   # score ≈ 0.607
    small_ground = _blob((50, 0,  2.0), 1100, z=2.0, rng=rng)   # score ≈ 1.000
    pts = np.vstack([big_ground, small_ground])
    hag = np.concatenate([
        np.full(1500, 2.0, dtype=np.float64),
        np.full(1100, 2.0, dtype=np.float64),
    ])

    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                     min_points=50, voxel_m=0,
                                     height_above_ground=hag)
    assert len(clusters) == 2
    assert clusters[0]["id"] == 1
    assert clusters[0]["num_points"] == 1100   # closer-to-peak wins
    assert clusters[0]["confidence_score"] > clusters[1]["confidence_score"]


def test_dbscan_label_preserved():
    """dbscan_label on each cluster is the original 0-based DBSCAN id."""
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    hag = np.full(500, 2.0, dtype=np.float64)
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                     min_points=50, voxel_m=0,
                                     height_above_ground=hag)
    assert clusters[0]["dbscan_label"] == 0


def test_hag_field_types():
    """hag_min / hag_max / hag_mean are Python floats (JSON-safe)."""
    pts = _blob((0, 0, 2.0), 500, z=2.0)
    hag = np.full(500, 2.0, dtype=np.float64)
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                     min_points=50, voxel_m=0,
                                     height_above_ground=hag)
    c = clusters[0]
    for f in ("hag_min", "hag_max", "hag_mean", "confidence_score"):
        assert isinstance(c[f], float), f"{f} should be float, got {type(c[f])}"


def test_passed_height_filter_field():
    """Every returned cluster has passed_height_filter=True (because
    rejected clusters are filtered out before the final list is built).
    """
    rng = np.random.default_rng(0xBEEF)
    g_pts = _blob((0,  0,  2.0), 500, z=2.0, rng=rng)
    m_pts = _blob((50, 0, 10.0), 500, z_range=(3.0, 18.0), rng=rng)
    pts = np.vstack([g_pts, m_pts])
    hag = np.concatenate([
        np.full(500, 2.0, dtype=np.float64),
        np.full(500, 10.0, dtype=np.float64),
    ])
    clusters, _, _ = cluster_instances(pts, pts, eps=3.0,
                                     min_points=50, voxel_m=0,
                                     height_above_ground=hag)
    assert len(clusters) == 1
    assert clusters[0]["passed_height_filter"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
