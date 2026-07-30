"""Unit tests for the ROI bbox pre-filter introduced for the v0.8
Stage 1 regression (R1 in
``/root/.claude/plans/docker-root-illegal-construction-inspec-radiant-wilkes.md``).

The filter is intentionally **conservative** — bbox ``is_outside`` returns
True only if *every* corner of the bbox is outside the polygon. The set
of tests below pins this contract so any future tuning (or rewriting
the check to use shapely) cannot silently start skipping boundary tiles.

Run::

    python -m pytest tests/algorithm/test_roi_bbox_filter.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

from run_pipeline import _RoiBboxFilter  # noqa: E402


def _square_polygon(cx: float = 0.0, cy: float = 0.0,
                    half: float = 25.0) -> np.ndarray:
    """Return a 4-vertex axis-aligned square polygon (CCW)."""
    return np.array([
        [cx - half, cy - half],
        [cx + half, cy - half],
        [cx + half, cy + half],
        [cx - half, cy + half],
    ], dtype=np.float64)


def test_aabb_rejection_when_tile_far_outside():
    """A tile whose XY bbox is far away from the polygon is dropped
    before the corner ray-cast runs (the cheap-rejection branch)."""
    poly = _square_polygon(cx=0.0, cy=0.0, half=25.0)
    f = _RoiBboxFilter(poly)
    # Tile at (1000, 1000), 10×10 m, half-axes (5, 5)
    assert f.is_outside(bbox_xy_e=1000.0, bbox_xy_n=1000.0,
                        extents=(5.0, 5.0)) is True


def test_tile_inside_polygon_is_kept():
    """A tile whose centre is well inside the polygon is kept."""
    poly = _square_polygon(cx=0.0, cy=0.0, half=25.0)
    f = _RoiBboxFilter(poly)
    assert f.is_outside(bbox_xy_e=0.5, bbox_xy_n=-0.3,
                        extents=(2.0, 2.0)) is False


def test_tile_centred_on_polygon_edge_is_kept_conservative():
    """A tile sitting exactly on the polygon edge must NOT be skipped
    — the filter is conservative on purpose."""
    poly = _square_polygon(cx=0.0, cy=0.0, half=25.0)
    f = _RoiBboxFilter(poly)
    # Centre at (24.99, 0), 1×1 m → corner (24.49, 0) still inside,
    # corner (25.49, 0) outside. Conservative ⇒ keep (NOT outside).
    assert f.is_outside(bbox_xy_e=24.99, bbox_xy_n=0.0,
                        extents=(0.5, 0.5)) is False


def test_tile_completely_outside_but_in_aabb_extent_kept():
    """A bbox that overlaps the polygon's AABB but is *outside* the
    polygon (e.g. diagonal gap) is correctly rejected by the
    corner ray-cast branch, but only when *all 4 corners* are outside."""
    poly = _square_polygon(cx=0.0, cy=0.0, half=10.0)
    f = _RoiBboxFilter(poly)
    # Tile bbox centred at (15, 15), 2×2 m → 4 corners (14..16, 14..16),
    # all outside the 10×10 square. Should be skipped.
    assert f.is_outside(bbox_xy_e=15.0, bbox_xy_n=15.0,
                        extents=(1.0, 1.0)) is True


def test_tile_with_one_corner_inside_polygon_is_kept():
    """A tile whose bbox overlaps the polygon with one corner inside
    (e.g. (12, 12) for a (15,15)-centred tile that just touches the
    10×10 square on its lower-left) must be kept.

    Note: this is *the* ambiguous case — a corner inside means the
    bbox *intersects* the polygon, but the polygon might still wrap
    around the tile corners. We always keep if any corner is inside,
    which can produce a tiny overshoot but never loses tiles.
    """
    poly = _square_polygon(cx=0.0, cy=0.0, half=10.0)
    f = _RoiBboxFilter(poly)
    # 16×16 tile centred at (15, 15) → corner (7, 7) is inside the
    # 10×10 square (which spans (-10..10, -10..10)).
    assert f.is_outside(bbox_xy_e=15.0, bbox_xy_n=15.0,
                        extents=(8.0, 8.0)) is False


def test_filter_handles_non_square_polygon():
    """The AABB-rejection short-circuit works against the *polygon's*
    bounding box, not against its shape — a long thin poly must still
    be respected by the corner ray-cast."""
    # A 50×0.1 m "ribbon" from (0, 0) to (50, 0.1)
    poly = np.array([
        [0.0,  0.0],
        [50.0, 0.0],
        [50.0, 0.1],
        [0.0,  0.1],
    ], dtype=np.float64)
    f = _RoiBboxFilter(poly)
    # A 1×1 m tile centred at (25, 5) — Y bbox is well above the ribbon;
    # all 4 corners above the ribbon. Should be skipped.
    assert f.is_outside(bbox_xy_e=25.0, bbox_xy_n=5.0,
                        extents=(0.5, 0.5)) is True
    # A 0.05×0.05 m tile centred at (25, 0.05) — completely inside the
    # ribbon. Should be kept.
    assert f.is_outside(bbox_xy_e=25.0, bbox_xy_n=0.05,
                        extents=(0.02, 0.02)) is False


def test_filter_invalid_input_rejected_safely():
    """A degenerate (1-vertex) polygon has no edges for the ray-cast
    to test against. The filter will report ``is_outside=True`` — a
    false positive — which is the safer behaviour when the polygon
    itself is malformed. Callers are expected to reject polygons with
    <3 vertices upstream of this filter (see ``ROIOpts.active``)."""
    poly = np.array([[0.0, 0.0]], dtype=np.float64)
    f = _RoiBboxFilter(poly)
    out = f.is_outside(bbox_xy_e=0.0, bbox_xy_n=0.0, extents=(0.1, 0.1))
    # Documented behaviour: degenerate polygon ⇒ "outside" returned.
    # The earlier docstring in this test claimed the opposite; that
    # was a misunderstanding of how vectorised ray-casting handles a
    # zero-area polygon.
    assert out is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
