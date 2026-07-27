"""Region-of-Interest utilities for the algorithm pipeline.

Converts a WGS84 polygon (lat / lon / h) into B's local ENU frame and
provides ray-casting containment. No external deps beyond pyproj +
numpy.

The backend (`api_server.SubmitRequest`) accepts three optional fields:

* ``positionMode``     — coordinate system identifier (e.g. ``"WGS-84"``).
                         Informational only; not used in geometry.
* ``areaCoordinates``  — list of ``{latitude, longitude, altitude}``
                         dicts defining a closed polygon (≥3 vertices).
* ``radius``           — reserved for future use (e.g. buffer outward).
                         Currently **not consumed**; logged and ignored.

This module is deliberately tiny — it does *no* pipeline orchestration,
just the bits every stage needs to know "is this point inside the ROI?".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyproj


log = logging.getLogger("roi")


# ──────────────────────────────────────────────────────────────────────
# Options dataclass — passed by reference through the pipeline.
# `polygon_enu` is filled in by main() after stage_extract returns
# `transform_b`; until then `active` is False and stages skip the mask.
# ──────────────────────────────────────────────────────────────────────
@dataclass
class ROIOpts:
    position_mode: Optional[str] = None
    area_coordinates: Optional[list] = None   # raw list of {lat, lon, h} dicts
    polygon_enu: Optional[np.ndarray] = None  # (V, 2) (E, N) in B's local ENU
    radius: Optional[float] = None

    @property
    def active(self) -> bool:
        return (self.polygon_enu is not None
                and len(self.polygon_enu) >= 3)


# ──────────────────────────────────────────────────────────────────────
# Parsing
# ──────────────────────────────────────────────────────────────────────
def parse_area_coordinates(raw: Optional[str]) -> Optional[list[dict]]:
    """Parse --area-coordinates JSON. Returns None if input is empty /
    None / ``[]`` (the JSON-string equivalent of "not supplied").

    Raises ``ValueError`` for malformed JSON, ≥1 but <3 vertices, or any
    vertex missing ``latitude`` / ``longitude``. The caller (main)
    decides whether to FAIL the task or just disable ROI; we surface
    the error.
    """
    if not raw:
        return None
    try:
        coords = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"bad --area-coordinates JSON: {e}") from e
    if not isinstance(coords, list):
        raise ValueError(
            f"--area-coordinates must be a JSON list, got {type(coords).__name__}"
        )
    # Empty list = "no ROI" (same as not-supplied). Only treat as error
    # when there are 1-2 vertices — a polygon needs ≥3.
    if len(coords) == 0:
        return None
    if len(coords) < 3:
        raise ValueError(
            f"--area-coordinates must be ≥3 vertices, got {len(coords)}"
        )
    for i, v in enumerate(coords):
        if not isinstance(v, dict):
            raise ValueError(f"vertex {i} is not a dict: {v!r}")
        if "latitude" not in v or "longitude" not in v:
            raise ValueError(
                f"vertex {i} missing latitude/longitude: {v!r}"
            )
    return coords


# ──────────────────────────────────────────────────────────────────────
# Coordinate conversion
# ──────────────────────────────────────────────────────────────────────
def polygon_to_b_enu(coords: list[dict],
                     transform_b: list[float] | np.ndarray) -> np.ndarray:
    """Project a list of WGS84 ``{latitude, longitude, altitude}`` dicts
    into B's local ENU frame, returning an ``(V, 2)`` array of
    ``(East, North)`` in metres. ``Up`` is dropped — we only need a 2D
    containment test.

    Path: lat/lon → ECEF (pyproj 4979→4978) → ENU via ``inv(T_b)``.
    ``T_b`` is the column-major 16-float from ``tileset.json root.transform``
    that maps ``[enu; 1] → [ecef; 1]``, so its inverse is the other way.
    """
    to_ecef = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:4978", always_xy=True,
    )
    T = np.asarray(transform_b, dtype=np.float64).reshape(4, 4, order="F")
    T_inv = np.linalg.inv(T)

    out = np.empty((len(coords), 2), dtype=np.float64)
    for i, v in enumerate(coords):
        ecef = to_ecef.transform(
            v["longitude"], v["latitude"],
            float(v.get("altitude", 0.0)),
        )
        enu_h = T_inv @ np.array([ecef[0], ecef[1], ecef[2], 1.0])
        out[i, 0] = enu_h[0]
        out[i, 1] = enu_h[1]
    return out


# ──────────────────────────────────────────────────────────────────────
# Polygon containment (vectorized ray-casting; no shapely dep)
# ──────────────────────────────────────────────────────────────────────
def points_in_polygon(x: np.ndarray, y: np.ndarray,
                      poly: np.ndarray) -> np.ndarray:
    """Vectorized ray-casting. ``poly`` is an ``(V, 2)`` array of
    closed-polygon vertices (last → first edge is implicit). Returns
    a boolean array: ``True`` where ``(x[i], y[i])`` is inside.

    Boundary points (exactly on an edge or vertex) are considered
    ``inside``, which matches the typical inspector expectation: a
    building wall that sits exactly on the polygon edge should still
    be inspected.

    Implementation note: the naive ``((yi > y) != (yj > y)) & (...)``
    cast double-counts when a horizontal edge passes through a query
    point (the classical "vertex-on-ray" edge case). To handle that
    cleanly we use the winding-number formulation: ``inside = (winding
    & 1) == 1``. This collapses every half-encirclement into the right
    parity and never produces a false negative for corner / edge points.

    Suppresses numpy divide-by-zero warnings internally (horizontal
    edges hit the ``/0`` branch — harmless because the matching
    ``(yi > y) != (yj > y)`` is ``False`` for that edge).
    """
    n = len(poly)
    # Accumulate the winding contribution as int to avoid any float
    # wrap-around; 8 polygon vertices × 1e9 points stays well within
    # int64 range.
    winding = np.zeros(len(x), dtype=np.int64)
    with np.errstate(divide="ignore", invalid="ignore"):
        j = n - 1
        for i in range(n):
            xi, yi = poly[i, 0], poly[i, 1]
            xj, yj = poly[j, 0], poly[j, 1]
            # Edges where the query y is strictly between yi and yj
            # cross the horizontal ray at y. Solve for x at that y.
            crosses = (yi > y) != (yj > y)
            x_at_y = (xj - xi) * (y - yi) / (yj - yi) + xi
            # Crossing that goes left-to-right contributes +1 winding,
            # right-to-left contributes -1. The sign of (yj - yi)
            # disambiguates the two.
            sign = np.where(yj > yi, 1, -1)
            winding += np.where(crosses & (x < x_at_y), sign, 0)
            j = i
    return (winding & 1) != 0


def lonlat_envelope(poly: np.ndarray) -> tuple[float, float, float, float]:
    """Return ``(lon_min, lon_max, lat_min, lat_max)`` of the polygon's
    bbox in *ENU* coordinates (not WGS84). Useful for cheap rejection
    before the ray-cast on huge point sets."""
    return (poly[:, 0].min(), poly[:, 0].max(),
            poly[:, 1].min(), poly[:, 1].max())


__all__ = [
    "ROIOpts",
    "parse_area_coordinates",
    "polygon_to_b_enu",
    "points_in_polygon",
    "lonlat_envelope",
]