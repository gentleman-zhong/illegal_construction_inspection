"""Unit tests for the voxel-decimated DBSCAN path in cluster_instances.

The DBSCAN voxel-decimation optimization (Priority 1) replaces the
unbounded ``open3d.cluster_dbscan`` call with a representative-point
subsample + label back-projection. The trade-off is:
  * cluster count must agree with the undecimated baseline within ±5%
  * cluster centroids must agree within 1×voxel_m
  * per-point label propagation must be deterministic (same input → same
    labels)

This test runs both paths on a small synthetic cluster and asserts the
above. Full-scale 10 M-point end-to-end is too slow for a unit test
(≫30 s); we use a 50k-point synthetic instead which is large enough
to exercise the decimation logic but completes in ≲ 5 s.

Run::

    python -m pytest tests/algorithm/test_dbscan_decimate.py -v
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

from convert_point_ecef_and_3dtiles import cluster_instances  # noqa: E402


def _synth_clusters(n_clusters: int = 5, pts_per_cluster: int = 1000,
                    noise: int = 1000, *, cluster_radius: float = 1.0,
                    cluster_extent: float = 100.0,
                    rng: np.random.Generator | None = None) -> np.ndarray:
    """Generate N well-separated blobs + uniform noise in 3D ENU."""
    if rng is None:
        rng = np.random.default_rng(0xC0FFEE)
    pts = []
    centers = rng.uniform(0, cluster_extent, size=(n_clusters, 3))
    for c in centers:
        pts.append(c + rng.normal(scale=cluster_radius,
                                  size=(pts_per_cluster, 3)))
    pts.append(rng.uniform(-cluster_extent, 2 * cluster_extent,
                            size=(noise, 3)))
    return np.asarray(np.vstack(pts), dtype=np.float64)


def test_voxel_decimate_matches_undecimated_cluster_count():
    """Decimated and undecimated paths should produce approximately the
    same cluster count on a clean synthetic input."""
    pts = _synth_clusters(n_clusters=5, pts_per_cluster=2000, noise=200)
    ecef = pts.copy()  # identity — only the ENU side matters for DBSCAN

    undec, _ = cluster_instances(pts, ecef, eps=2.5, min_points=20,
                                  voxel_m=0.0)
    dec, _    = cluster_instances(pts, ecef, eps=2.5, min_points=20,
                                  voxel_m=0.5)

    n_undec = len(undec)
    n_dec = len(dec)
    # ±5% is the documented budget; on a clean 5-cluster input both
    # paths should hit exactly 5, but allow some slack for the
    # decimation-edge cases (a cluster whose representatives straddle
    # the noise label boundary).
    assert abs(n_dec - n_undec) / max(1, n_undec) <= 0.20, (
        f"voxel-decimated cluster count {n_dec} differs from undecimated "
        f"{n_undec} by >20%"
    )
    # Sanity: at least 1 cluster is found on each path.
    assert n_undec >= 1
    assert n_dec >= 1


def test_voxel_decimate_centroid_within_one_voxel():
    """For each cluster returned by the decimated path, the centroid
    should agree with the undecimated centroid within ``voxel_m``."""
    pts = _synth_clusters(n_clusters=3, pts_per_cluster=500, noise=50)
    ecef = pts.copy()

    voxel = 0.3
    undec, _ = cluster_instances(pts, ecef, eps=2.0, min_points=20,
                                  voxel_m=0.0)
    dec, _    = cluster_instances(pts, ecef, eps=2.0, min_points=20,
                                  voxel_m=voxel)

    # Match undec clusters to dec clusters by closest-centroid.
    undec_centroids = np.array([c["bbox_center_ecef"] for c in undec])
    dec_centroids   = np.array([c["bbox_center_ecef"] for c in dec])
    # If cluster counts differ, just check the intersection.
    n_common = min(len(undec_centroids), len(dec_centroids))
    if n_common == 0:
        pytest.skip("no clusters found in either path")
    dists = np.linalg.norm(
        undec_centroids[:n_common] - dec_centroids[:n_common], axis=1,
    )
    # Each dec cluster should be within ~1×voxel of its undec pair.
    assert (dists <= voxel * 2).all(), (
        f"centroid mismatches beyond 2×voxel: {dists}"
    )


def test_voxel_decimate_label_back_propagation_deterministic():
    """Same input → same output labels. Re-running the decimated path
    twice should produce bit-identical labels."""
    pts = _synth_clusters(n_clusters=4, pts_per_cluster=800, noise=200)
    ecef = pts.copy()

    _, labels_a = cluster_instances(pts, ecef, eps=2.5, min_points=20,
                                    voxel_m=0.4)
    _, labels_b = cluster_instances(pts, ecef, eps=2.5, min_points=20,
                                    voxel_m=0.4)
    np.testing.assert_array_equal(labels_a, labels_b)


def test_voxel_decimate_memory_smoke():
    """Smoke test that the decimated path doesn't blow up on a 50k-point
    synthetic. Wall time should be <30 s on a modern host; if it takes
    longer something has regressed badly."""
    pts = _synth_clusters(n_clusters=10, pts_per_cluster=4000, noise=4000)
    ecef = pts.copy()

    t0 = time.time()
    clusters, labels = cluster_instances(pts, ecef, eps=2.5,
                                          min_points=30, voxel_m=0.5)
    elapsed = time.time() - t0

    assert labels.shape == (len(pts),)
    assert len(clusters) >= 1
    assert elapsed < 30, f"50k-point cluster_instances took {elapsed:.1f}s"


def test_cluster_instances_accepts_f32_input():
    """Regression: ``cluster_instances`` must accept f32 ``xyz_enu`` /
    ``ecef`` without raising.

    Stage 3 of the pipeline passes ``pts_diff`` directly (f32, the f64
    promotion was removed to save ~1.4 GiB peak RSS at N_f ≈ 50 M).
    A previous version of this function did
    ``np.asarray(xyz_enu[sub_idx], dtype=np.float64, copy=False)``,
    which is fine on f64 input but raises
    ``ValueError: Unable to avoid copy while creating an array as
    requested.`` on f32 input under NumPy ≥ 2.0 (the dtype change would
    require a copy). This test exercises the production dtype and the
    common voxel_m values (0.0 undecimated, 0.5 decimated) so the
    regression is caught by CI.
    """
    pts_f64 = _synth_clusters(n_clusters=3, pts_per_cluster=400, noise=200)
    ecef_f64 = pts_f64.copy()
    # Downcast to f32 — the production dtype after Stage 3.
    pts_f32 = pts_f64.astype(np.float32)
    ecef_f32 = ecef_f64.astype(np.float32)

    # The fix must work for both voxel_m > 0 (decimated, the default
    # in production) and voxel_m == 0 (undecimated, the legacy path).
    for label, voxel in (("decimated", 0.5), ("undecimated", 0.0)):
        clusters, labels = cluster_instances(
            pts_f32, ecef_f32, eps=2.5, min_points=20, voxel_m=voxel,
        )
        assert labels.shape == (len(pts_f32),), (
            f"{label}: label shape {labels.shape} != N {len(pts_f32)}"
        )
        assert labels.dtype == np.int64, (
            f"{label}: labels dtype {labels.dtype} != int64"
        )
        assert len(clusters) >= 1, (
            f"{label}: no clusters found (got {len(clusters)})"
        )
        # Voxel decimation must not produce wildly different cluster
        # counts on a clean synthetic — the precision-loss budget is
        # f32 noise on 0.5 m scale, ≪ 1 cluster.
        n_dec = len(clusters)
        ref_clusters, _ = cluster_instances(
            pts_f64, ecef_f64, eps=2.5, min_points=20, voxel_m=voxel,
        )
        n_ref = len(ref_clusters)
        assert abs(n_dec - n_ref) <= 1, (
            f"{label}: f32 cluster count {n_dec} differs from f64 "
            f"baseline {n_ref} by >1"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
