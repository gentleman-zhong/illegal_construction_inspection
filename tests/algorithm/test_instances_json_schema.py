"""Unit tests for ``write_instances_json`` schema extensions.

The Stage 4 两违 post-filter adds several top-level metadata fields
(``height_filter_enabled``, ``hag_max_low_m``, ``hag_min_high_m``,
``confidence_peak_n``, ``confidence_sigma_n``, ``n_clusters_before_height_filter``,
``dtm_ground_count``, ``dtm_quality``) and per-cluster fields
(``hag_min``, ``hag_max``, ``hag_mean``, ``scenario``,
``confidence_score``, ``dbscan_label``, ``passed_height_filter``).

This test writes a synthetic payload to a tmp file, parses it back,
and asserts every field round-trips correctly. It also verifies the
``dtm_quality`` threshold mapping (``None`` → ``unavailable``;
``< 200`` → ``poor``; ``200..999`` → ``ok``; ``≥ 1000`` → ``good``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

from convert_point_ecef_and_3dtiles import (  # noqa: E402
    _hull_one_cluster,
    write_instances_json,
)


def _dummy_cluster(cid: int, **overrides) -> dict:
    base = {
        "id": cid,
        "dbscan_label": cid - 1,
        "num_points": 1100,
        "bbox_min_ecef": [0, 0, 0],
        "bbox_max_ecef": [1, 1, 1],
        "bbox_center_ecef": [0.5, 0.5, 0.5],
        "bbox_size": [1, 1, 1],
        "hull_vertices_ecef": [],
        "hull_triangles": [],
        "hag_min": 0.0,
        "hag_max": 4.8,
        "hag_mean": 2.1,
        "scenario": "ground",
        "confidence_score": 0.998,
        "passed_height_filter": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Top-level metadata
# ---------------------------------------------------------------------------
def test_full_metadata_round_trip(tmp_path: Path):
    out = tmp_path / "instances.json"
    clusters = [_dummy_cluster(1), _dummy_cluster(2)]
    write_instances_json(
        out, clusters,
        eps=3.0, min_points=120, n_input_points=940_826,
        height_filter_enabled=True,
        hag_max_low_m=5.0, hag_min_high_m=20.0,
        confidence_peak_n=1100, confidence_sigma_n=300.0,
        n_clusters_before_height_filter=4,
        dtm_ground_count=1500,
    )
    payload = json.loads(out.read_text())

    # Top-level required fields
    assert payload["eps"] == 3.0
    assert payload["min_points"] == 120
    assert payload["n_input_points"] == 940_826
    assert payload["n_clusters"] == 2
    assert payload["n_clusters_before_height_filter"] == 4
    assert payload["height_filter_enabled"] is True
    assert payload["hag_max_low_m"] == 5.0
    assert payload["hag_min_high_m"] == 20.0
    assert payload["confidence_peak_n"] == 1100
    assert payload["confidence_sigma_n"] == 300.0
    assert payload["dtm_ground_count"] == 1500
    assert payload["dtm_quality"] == "good"

    # Clusters preserved verbatim
    assert len(payload["clusters"]) == 2
    assert payload["clusters"][0]["id"] == 1


def test_minimal_metadata_default_filter_disabled(tmp_path: Path):
    """Calling without the new kwargs (legacy call sites / tests) gives
    ``height_filter_enabled=False`` and ``dtm_quality='unavailable'``."""
    out = tmp_path / "instances.json"
    clusters = [{"id": 1, "num_points": 100}]
    write_instances_json(out, clusters, eps=3.0, min_points=50,
                         n_input_points=100)
    payload = json.loads(out.read_text())
    assert payload["height_filter_enabled"] is False
    assert payload["hag_max_low_m"] is None
    assert payload["hag_min_high_m"] is None
    assert payload["confidence_peak_n"] is None
    assert payload["confidence_sigma_n"] is None
    assert payload["n_clusters_before_height_filter"] is None
    assert payload["dtm_ground_count"] is None
    assert payload["dtm_quality"] == "unavailable"


@pytest.mark.parametrize("count, expected_quality", [
    (None,  "unavailable"),
    (0,     "poor"),
    (50,    "poor"),
    (199,   "poor"),
    (200,   "ok"),
    (500,   "ok"),
    (999,   "ok"),
    (1000,  "good"),
    (5000,  "good"),
])
def test_dtm_quality_thresholds(tmp_path: Path, count, expected_quality):
    out = tmp_path / "instances.json"
    write_instances_json(out, [], eps=3.0, min_points=50,
                         n_input_points=0, dtm_ground_count=count)
    payload = json.loads(out.read_text())
    assert payload["dtm_ground_count"] == count
    assert payload["dtm_quality"] == expected_quality


# ---------------------------------------------------------------------------
# Cluster schema
# ---------------------------------------------------------------------------
def test_cluster_round_trip_preserves_all_fields(tmp_path: Path):
    out = tmp_path / "instances.json"
    cluster = _dummy_cluster(
        1,
        num_points=1142,
        hag_min=0.3, hag_max=4.8, hag_mean=2.1,
        scenario="ground", confidence_score=0.998,
        dbscan_label=12, passed_height_filter=True,
    )
    write_instances_json(out, [cluster], eps=3.0, min_points=50,
                         n_input_points=1142)
    payload = json.loads(out.read_text())
    c = payload["clusters"][0]
    assert c["id"] == 1
    assert c["num_points"] == 1142
    assert c["hag_min"] == 0.3
    assert c["hag_max"] == 4.8
    assert c["hag_mean"] == 2.1
    assert c["scenario"] == "ground"
    assert c["confidence_score"] == 0.998
    assert c["dbscan_label"] == 12
    assert c["passed_height_filter"] is True
    # Legacy fields preserved
    assert c["bbox_size"] == [1, 1, 1]
    assert c["bbox_center_ecef"] == [0.5, 0.5, 0.5]


def test_hull_metrics_are_written_for_a_rectangular_cluster():
    """Footprint is the ENU XY rectangle; volume is the 3D hull volume."""
    import numpy as np

    pts_enu = np.asarray([
        [x, y, z]
        for x in (0.0, 2.0)
        for y in (0.0, 3.0)
        for z in (0.0, 4.0)
    ])
    # ECEF translation must not affect either local metric.
    pts_ecef = pts_enu + np.asarray([2_000_000.0, 4_000_000.0, 4_000_000.0])
    _, cluster = _hull_one_cluster(0, pts_ecef, pts_enu)

    assert cluster["footprint_area_m2"] == pytest.approx(6.0)
    assert cluster["volume_m3"] == pytest.approx(24.0)


def test_legacy_cluster_without_new_fields_is_preserved(tmp_path: Path):
    """If a caller (or an old on-disk artifact) supplies a cluster dict
    without the new fields, write_instances_json still serializes it
    verbatim — backward compatible."""
    out = tmp_path / "instances.json"
    legacy_cluster = {
        "id": 1,
        "num_points": 500,
        "bbox_min_ecef": [0, 0, 0],
        "bbox_max_ecef": [1, 1, 1],
        "bbox_center_ecef": [0.5, 0.5, 0.5],
        "bbox_size": [1, 1, 1],
        "hull_vertices_ecef": [],
        "hull_triangles": [],
    }
    write_instances_json(out, [legacy_cluster], eps=3.0, min_points=50,
                         n_input_points=500)
    payload = json.loads(out.read_text())
    c = payload["clusters"][0]
    # New fields absent → JSON has them missing, but legacy fields are
    # present and identical.
    assert c["id"] == 1
    assert c["num_points"] == 500
    assert "hag_min" not in c
    assert "scenario" not in c


# ---------------------------------------------------------------------------
# File / directory semantics
# ---------------------------------------------------------------------------
def test_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c" / "instances.json"
    write_instances_json(nested, [], eps=3.0, min_points=50,
                         n_input_points=0)
    assert nested.is_file()


def test_overwrites_existing_file(tmp_path: Path):
    out = tmp_path / "instances.json"
    write_instances_json(out, [_dummy_cluster(1)], eps=3.0, min_points=50,
                         n_input_points=1100)
    first = json.loads(out.read_text())
    write_instances_json(out, [], eps=3.0, min_points=50, n_input_points=0)
    second = json.loads(out.read_text())
    assert len(first["clusters"]) == 1
    assert len(second["clusters"]) == 0
    assert second["n_clusters"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
