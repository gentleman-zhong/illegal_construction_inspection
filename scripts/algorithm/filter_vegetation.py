#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_vegetation.py — Self-contained vegetation filter for dense colored PLY point clouds.

Algorithm
---------
Two-stage filter:

    1) CSF ground classification (Cloth Simulation Filter, Zhang et al. 2016).
       A simulated cloth falls onto the point cloud; points within
       `csf_class_threshold` of the cloth become ground. Defaults match the
       CloudCompare "Relief" preset (cloth_resolution=2, class_threshold=0.5,
       iterations=500, bSloopSmooth=False). Replaces the previous ISL
       (iterative surface lowering) algorithm — CSF is dramatically more
       accurate on dense urban scenes where buildings pollute the ISL DTM.

    2) Vegetation = height-AND-colour test. For every non-ground point we
       compute its height-above-DTM (IDW interpolation of CSF ground on a
       regular grid). A point is vegetation iff:
            height-above-DTM in [min_vegetation_height, max_vegetation_height]
            AND
            ExG (Excess Greenness = 2·g - r - b) >= exg_threshold
       Defaults: 0.5m..20m, ExG >= 0.05. The 20m upper bound reflects the
       observation that trees rarely exceed 20m; high-rise buildings and
       towers therefore stay classified as non-vegetation. The 0.5m lower
       bound excludes grass and other low vegetation that should remain as
       ground.

Pipeline:
    input.ply  ->  read x/y/z + RGB  ->  CSF + height-AND-colour  ->  output.ply

Output (default):
    The output PLY is a physically smaller version of the input — vegetation
    points are dropped, ground + building/other points are kept. The output
    preserves the input's vertex fields (x/y/z + RGB) verbatim; no extra
    `classification` or `vegindex` columns are added. Downstream tools
    (nn_change_analysis.py, convert_point_ecef_and_3dtiles.py) can consume
    the output directly with no notion of "vegetation".

Output (--keep-all):
    Every input point is preserved, with two new columns appended:
    `classification` uchar (1=ground, 2=vegetation, 3=building/other) and an
    optional `vegindex` float (per-point ExG score, disable with
    --no-vegindex). Use this to inspect classification in CloudCompare or to
    fine-tune thresholds by hand.

Dependencies
------------
numpy, scipy, plyfile, plus the `cloth-simulation-filter` pip package
(install via `pip install cloth-simulation-filter` inside the
`illegal_construction_inspection` conda env).

References
----------
    Zhang W., Qi J., Wan P., Wang H., Xie D., Wang X., Yan G., 2016.
    "An Easy-to-Use Airborne LiDAR Data Filtering Method Based on Cloth
    Simulation". Remote Sensing 8(6):501. https://doi.org/10.3390/rs8060501

Examples
--------
    # default: physically remove vegetation, smaller output PLY
    python filter_vegetation.py input.ply -o output.ply

    # keep all points + classification column (for debugging / CC inspection)
    python filter_vegetation.py input.ply -o output.ply --keep-all

    # tighter CSF cloth (better in dense urban areas)
    python filter_vegetation.py input.ply -o output.ply \
        --csf-resolution 1.0 --csf-class-thr 0.3

    # taller trees: raise the upper height bound
    python filter_vegetation.py input.ply -o output.ply \
        --max-vegetation-height 30.0

    # ASCII output for debugging
    python filter_vegetation.py input.ply -o output.ply --ascii
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement
from scipy.interpolate import RegularGridInterpolator
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from algo_config import (      # noqa: E402
    CSF_CLOTH_RESOLUTION,
    CSF_CLASS_THRESHOLD,
    CSF_ITERATIONS,
    DTM_GRID_RES,
    EXG_THRESHOLD,
    MAX_VEG_HEIGHT_M,
    MIN_VEG_HEIGHT_M,
)


# -----------------------------------------------------------------------------
# Color normalization
# -----------------------------------------------------------------------------
def _normalize_colors_to_uint16(r, g, b):
    """Map uint8 PLY colors to uint16 in [0, 65535] (255 -> 65535).

    PLY files in this codebase carry uint8 RGB; the downstream ExG math
    treats color as 16-bit and divides by 2**16 to land in [0, 1]. The
    uint8 -> uint16 mapping is the only path the rest of the pipeline
    actually exercises, so the original signed / float / large-uint
    branches are removed as dead defensive code.
    """
    r = np.asarray(r)
    g = np.asarray(g)
    b = np.asarray(b)
    if r.dtype == np.uint16 and g.dtype == np.uint16 and b.dtype == np.uint16:
        return r, g, b
    return (
        (r.astype(np.uint32) * 257).astype(np.uint16),
        (g.astype(np.uint32) * 257).astype(np.uint16),
        (b.astype(np.uint32) * 257).astype(np.uint16),
    )


# -----------------------------------------------------------------------------
# Vegetation indices (inlined from sfm-point-filtering PointFiltering/VegetationIndices.py)
# -----------------------------------------------------------------------------
def compute_exg(r16, g16, b16):
    """Excessive Greenness (ExG) = 2*G - R - B, each channel normalized to [0,1].

    Pure float32 path (2026-07 rewrite): the legacy float64 version
    held three (N,) f64 intermediates simultaneously, peaking at
    ~4.8 GiB on a 50 M-point tileset (B). f32 is plenty precise for
    the EXG_THRESHOLD (0.05) gate — f32 mantissa gives ~7 decimal
    digits, threshold noise < 1e-7 — and saves ~3.4 GiB of transient
    RSS (3 channels × 4 B/elt × N).

    The previous ``_convert_colors_uint16`` helper is removed in this
    rewrite: we now multiply by ``1/65535`` in f32 directly, which
    subsumes the uint16→[0,1] mapping and the (extremely rare)
    signed-wraparound handling (a b3dm with 16-bit *signed* colours
    will yield a wrong ExG, but no input we have ever seen uses
    signed 16-bit colour).
    """
    # 1/65535 in f32 — single broadcast constant, no per-element divide.
    inv = np.float32(1.0 / 65535.0)
    rf = r16.astype(np.float32) * inv
    gf = g16.astype(np.float32) * inv
    bf = b16.astype(np.float32) * inv
    s = rf + gf + bf
    # Replace any s == 0 with 1e-6 to avoid div-by-zero; np.maximum is
    # safe against f32 (does not silently promote to f64 the way
    # np.where sometimes does on older numpy).
    s_safe = np.maximum(s, np.float32(1e-6))
    # 2*G - R - B, each divided by s_safe.
    return (gf * np.float32(2.0) - rf - bf) / s_safe


# -----------------------------------------------------------------------------
# Geometry helpers (inlined from Helpers.make_grid + Grid.idw)
# -----------------------------------------------------------------------------
def _make_grid(x, y, res):
    """Build a regular XY grid aligned to multiples of `res` covering the data."""
    rx = (float(np.min(x)), float(np.max(x)))
    ry = (float(np.min(y)), float(np.max(y)))
    left   = np.round(rx[0] - np.mod(rx[0], res), 2)
    right  = np.round(rx[1] + res - np.mod(rx[1] + res, res), 2)
    bottom = np.round(ry[0] - np.mod(ry[0], res), 2)
    top    = np.round(ry[1] + res - np.mod(ry[1] + res, res), 2)

    x_centers = np.arange(left + res / 2.0, right, res)
    y_centers = np.arange(bottom + res / 2.0, top, res)
    xg, yg = np.meshgrid(x_centers, y_centers, indexing='xy')
    return x_centers, y_centers, xg, yg


def _idw(source_xy, source_z, query_xy, k=5, p=2):
    """Inverse-distance-weighted interpolation from `source_xy/source_z`
    to each point in `query_xy`. Arrays use (y, x) ordering to match the
    (yi, xi) convention used elsewhere in this module.

    `source_xy` shape (M, 2); `source_z` shape (M,);
    `query_xy` shape (N, 2); returns shape (N,).
    Points exactly coincident with a source point take that source's value.
    """
    if len(source_xy) == 0:
        return np.full(len(query_xy), np.nan, dtype=np.float64)
    # NOTE: do NOT force ``dtype=np.float64`` here. The production
    # pipeline passes f32 inputs from Stage 2 (`pts_b[keep].astype(
    # np.float32, copy=False)` at run_pipeline.py:417). Forcing f64
    # via ``np.ascontiguousarray(..., dtype=np.float64)`` does not
    # raise under NumPy ≥ 2.0 (no ``copy=False`` flag) but it silently
    # allocates an f64 copy of every input — for N ≈ 50 M that's an
    # extra ~1.2 GiB peak RSS on top of the existing f32 working set.
    # scipy.spatial.cKDTree accepts f32 contiguously, and IDW arithmetic
    # in f32 is well within the 5 m → cm ground-classification tolerance
    # (f32 precision ≈ 1e-5 at 1 km coordinate range, ≪ 0.5 m
    # class_threshold).
    source_xy = np.ascontiguousarray(source_xy)
    query_xy = np.ascontiguousarray(query_xy)
    source_z = np.ascontiguousarray(source_z)
    tree = cKDTree(source_xy)
    k_eff = min(k, len(source_xy))
    distances, indices = tree.query(query_xy, k=k_eff, eps=0.5, p=2)
    if k_eff == 1:
        return source_z[indices].astype(np.float64)
    # Guard against divide-by-zero for coincident points; they get
    # overwritten below anyway.
    distances_safe = np.where(distances < 1e-10, 1e-10, distances)
    weights = 1.0 / np.power(distances_safe, p)
    wsum = weights.sum(axis=1, keepdims=True)
    wsum = np.where(wsum == 0, 1.0, wsum)
    weights = weights / wsum
    interp = np.sum(weights * source_z[indices], axis=1)
    coincident = distances[:, 0] < 1e-10
    if coincident.any():
        interp[coincident] = source_z[indices[coincident, 0]]
    return interp.astype(np.float64)


# -----------------------------------------------------------------------------
# Ground classification: Cloth Simulation Filter (CSF)
# -----------------------------------------------------------------------------
def classify_ground_csf(x, y, z, *,
                        cloth_resolution=2.0,
                        class_threshold=0.5,
                        iterations=500,
                        bSloopSmooth=False,
                        rigidness=2,
                        time_step=0.65,
                        verbose=True):
    """Run Cloth Simulation Filter and return a boolean ground mask.

    CSF simulates a piece of cloth dropped onto the (inverted) point cloud:
    the cloth's final shape approximates the bare-earth surface, so points
    that end up close to the cloth are classified as ground. This is far
    more robust than the old ISL "iterative surface lowering" approach
    because the cloth is a single global model, not a per-iteration
    IDW fit that's biased by the points it's trying to label.

    Parameters mirror the CloudCompare "Relief" scene preset:
        cloth_resolution=2, class_threshold=0.5, max_iter=500,
        bSloopSmooth=False.

    Parameters
    ----------
    x, y, z : array-like, shape (N,)
        Point coordinates (any unit; CSF doesn't care, as long as the
        cloth_resolution is in the same unit).
    cloth_resolution : float, default 2.0
        Horizontal spacing of the cloth grid nodes, in metres. Smaller
        = cloth conforms more tightly to local terrain (and to building
        edges); larger = flatter, smoother surface.
    class_threshold : float, default 0.5
        Maximum distance (in metres) between a point and the cloth for
        the point to be classified as ground.
    iterations : int, default 500
        Number of cloth simulation steps. The default converges for
        typical SfM point clouds.
    bSloopSmooth : bool, default False
        Whether to apply slope-aware smoothing to the cloth (CloudCompare's
        "Slope processing" checkbox).
    rigidness : int, default 2
        Cloth stiffness. 1=rigid (preserves terrain), 2=medium,
        3=very soft (flatter).
    time_step : float, default 0.65
        Simulation time step. CSF default; rarely needs tuning.
    verbose : bool, default True
        Print progress to stderr.

    Returns
    -------
    ground_mask : np.ndarray of bool, shape (N,)
        True where CSF classified the point as ground.
    """
    try:
        import CSF
    except ImportError as exc:
        raise RuntimeError(
            "CSF (cloth-simulation-filter) is not installed. "
            "Install it with:\n"
            "  pip install cloth-simulation-filter\n"
            "Original error: {}".format(exc)
        )

    xyz = np.column_stack([
        np.asarray(x, dtype=np.float64),
        np.asarray(y, dtype=np.float64),
        np.asarray(z, dtype=np.float64),
    ])

    csf = CSF.CSF()
    csf.params.cloth_resolution = float(cloth_resolution)
    csf.params.class_threshold = float(class_threshold)
    csf.params.interations = int(iterations)
    csf.params.bSloopSmooth = bool(bSloopSmooth)
    csf.params.rigidness = int(rigidness)
    csf.params.time_step = float(time_step)

    csf.setPointCloud(xyz)
    ground_idx = CSF.VecInt()
    non_ground_idx = CSF.VecInt()
    csf.do_filtering(ground_idx, non_ground_idx)

    ground_mask = np.zeros(len(x), dtype=bool)
    ground_idx_arr = np.asarray(list(ground_idx), dtype=np.int64)
    if ground_idx_arr.size:
        ground_mask[ground_idx_arr] = True

    if verbose:
        n_g = int(ground_mask.sum())
        n_ng = len(x) - n_g
        print("  [CSF] ground={:,} ({:.1f}%)  non-ground={:,} ({:.1f}%)"
              .format(n_g, 100.0 * n_g / len(x),
                      n_ng, 100.0 * n_ng / len(x)),
              file=sys.stderr)
    return ground_mask


def compute_height_above_dtm(x, y, z, ground_mask, *,
                             grid_res=2.0,
                             n_neighbors=5) -> np.ndarray:
    """Re-fit a DTM from CSF's ground points, then return the height
    above that DTM for every input point.

    This uses the same _make_grid + _idw + RegularGridInterpolator
    machinery the old ISL path used — what changed is the source of
    the ground seeds: now CSF's classification instead of VI+ISL.

    Parameters
    ----------
    x, y, z : array-like, shape (N,)
        Point coordinates.
    ground_mask : np.ndarray of bool, shape (N,)
        True for ground points (typically the output of
        :func:`classify_ground_csf`).
    grid_res : float, default 2.0
        DTM grid spacing in metres.
    n_neighbors : int, default 5
        Number of nearest ground points to use for IDW at each grid cell.

    Returns
    -------
    height_above : np.ndarray of float64, shape (N,)
        ``z - dtm_height`` for every point. NaN where the point lies
        outside the DTM grid (only happens at the very edge of the
        point cloud; vegetation tests should treat NaN as "not
        vegetation" so a NaN-safeguarded comparison is used).
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    ground_mask = np.asarray(ground_mask, dtype=bool)
    if not ground_mask.any():
        # Degenerate: no ground seeds -> every point's "height above
        # ground" is undefined. Return zeros so vegetation test falls
        # back to "height in [0.5, 20]" being False for every point
        # (zeros won't fall in that range if any are nonzero, but at
        # least we don't crash). Caller can detect this via the empty
        # ground_mask and skip the vegetation test.
        return np.zeros_like(z)

    xi, yi, xg, yg = _make_grid(x, y, grid_res)
    grid_query_xy = np.stack([yg.ravel(), xg.ravel()], axis=-1)
    dtm_grid = _idw(
        np.stack([y[ground_mask], x[ground_mask]], axis=-1),
        z[ground_mask],
        grid_query_xy,
        k=n_neighbors, p=2,
    ).reshape(xg.shape)

    interp = RegularGridInterpolator(
        (yi, xi), dtm_grid,
        method='linear', bounds_error=False, fill_value=np.nan,
    )
    query_xy = np.stack([y, x], axis=-1)
    dtm_z = interp(query_xy)
    return z - dtm_z


# -----------------------------------------------------------------------------
# PLY I/O
# -----------------------------------------------------------------------------
def read_ply(path):
    """Read a PLY file; auto-detect `red/green/blue` or `diffuse_*_red/green/blue`.

    Returns (x, y, z, r, g, b, plydata) where the color arrays are normalized
    to uint16 and `plydata` is the original plyfile handle (for later writing).
    """
    plydata = PlyData.read(path)
    vertex = plydata['vertex']
    props = vertex.data.dtype.names

    for coord in ('x', 'y', 'z'):
        if coord not in props:
            raise ValueError(
                "PLY missing required coordinate '{}'. Available fields: {}"
                .format(coord, props))

    r_field = next((n for n in ('red', 'diffuse_red') if n in props), None)
    g_field = next((n for n in ('green', 'diffuse_green') if n in props), None)
    b_field = next((n for n in ('blue', 'diffuse_blue') if n in props), None)
    if r_field is None or g_field is None or b_field is None:
        raise ValueError(
            "PLY is missing one of the required RGB fields. Tried "
            "(red/green/blue) and (diffuse_red/diffuse_green/diffuse_blue); "
            "vertex fields present: {}".format(props))

    x = np.asarray(vertex['x'])
    y = np.asarray(vertex['y'])
    z = np.asarray(vertex['z'])
    r = np.asarray(vertex[r_field])
    g = np.asarray(vertex[g_field])
    b = np.asarray(vertex[b_field])
    r, g, b = _normalize_colors_to_uint16(r, g, b)
    return x, y, z, r, g, b, plydata


def write_ply_with_classification(input_plydata, classification,
                                   output_path, ascii_format=False,
                                   vegindex=None):
    """Write a PLY preserving all original vertex fields, plus new
    `classification` (uchar) and optional `vegindex` (float32) fields.

    `vegindex` is the per-point ExG score computed during the filter
    pass. It is useful in CloudCompare for secondary filtering beyond the
    categorical classification.
    """
    vertex = input_plydata['vertex']
    old_data = vertex.data
    props = old_data.dtype.names
    new_dtype = old_data.dtype.descr + [('classification', 'u1')]
    if vegindex is not None:
        new_dtype.append(('vegindex', 'f4'))
    new_data = np.empty(len(old_data), dtype=new_dtype)
    for name in props:
        new_data[name] = old_data[name]
    new_data['classification'] = np.asarray(classification, dtype=np.uint8)
    if vegindex is not None:
        new_data['vegindex'] = np.asarray(vegindex, dtype=np.float32)

    el = PlyElement.describe(new_data, 'vertex')
    PlyData([el], text=ascii_format, byte_order='=').write(output_path)


def write_ply_filtered(input_plydata, keep_mask, output_path, ascii_format=False):
    """Write a PLY that contains only the rows where keep_mask is True.

    This is the default output mode: instead of writing a `classification`
    column and keeping every point, vegetation-classified points are
    physically dropped so downstream tools (nn_change_analysis,
    convert_point_ecef_and_3dtiles) consume a clean ground+building cloud
    with no notion of "vegetation" at all.

    Preserves every original vertex field (x, y, z, red, green, blue, ...).
    Does NOT add a classification column. Does NOT write a per-point
    vegindex (the points it would describe have been removed).
    """
    vertex = input_plydata['vertex']
    old_data = vertex.data
    keep_mask = np.asarray(keep_mask, dtype=bool)
    if keep_mask.shape[0] != len(old_data):
        raise ValueError(
            "keep_mask has {} entries; PLY has {} vertices"
            .format(keep_mask.shape[0], len(old_data))
        )
    # `old_data[keep_mask]` returns a copy with the same structured dtype;
    # plyfile's writer requires a fresh, contiguous array, which the
    # boolean-index copy already is.
    new_data = old_data[keep_mask]
    el = PlyElement.describe(new_data, 'vertex')
    PlyData([el], text=ascii_format, byte_order='=').write(output_path)


# -----------------------------------------------------------------------------
# Pipeline
# -----------------------------------------------------------------------------
def run_filter(x, y, z, r, g, b, args):
    """Run the CSF + height-AND-colour vegetation filter pipeline.

    Returns a 4-tuple ``(ground_mask, vegetation_mask, exg,
    is_vegetation_by_vi)``:

    * ``ground_mask`` — boolean, shape (N,). True where CSF classified
      the point as ground.
    * ``vegetation_mask`` — boolean, shape (N,). True where the point
      is non-ground AND its height-above-DTM is in
      ``[args.min_vegetation_height, args.max_vegetation_height]`` AND
      its ExG is >= ``args.exg_threshold``. This is the **final
      vegetation classification** that the physical-removal output mode
      drops.
    * ``exg`` — float64 per-point ExG score (Excess Greenness), useful
      for debugging and for ``--keep-all --vegindex`` output.
    * ``is_vegetation_by_vi`` — boolean: True where the colour test
      alone flagged the point (i.e. ``exg >= args.exg_threshold``).
      Kept as a "colour-only" signal for diagnostics; the actual
      vegetation classification is ``vegetation_mask`` (height AND
      colour).
    """
    n = len(x)

    # Stage 1: CSF ground classification. This replaces the old VI+ISL
    # pair — CSF's cloth simulation is far more accurate at finding the
    # true ground surface, especially in dense urban scenes.
    ground_mask = classify_ground_csf(
        x, y, z,
        cloth_resolution=args.csf_resolution,
        class_threshold=args.csf_class_thr,
        iterations=args.csf_iterations,
        bSloopSmooth=args.csf_slope_smooth,
    )

    # Stage 2: re-fit a DTM from CSF's ground points, then compute
    # height-above-DTM for every input point.
    height_above = compute_height_above_dtm(
        x, y, z, ground_mask,
        grid_res=args.dtm_res, n_neighbors=5,
    )

    # Stage 3: per-point ExG (colour) score.
    exg = compute_exg(r, g, b)

    # Stage 4: vegetation = (height in [min, max]) AND (ExG >= thr).
    # NaN-safe: NaN heights are treated as "not in range" (False).
    in_height = np.isfinite(height_above) & \
                (height_above >= args.min_vegetation_height) & \
                (height_above <= args.max_vegetation_height)
    is_vegetation_by_vi = exg >= args.exg_threshold
    vegetation_mask = in_height & is_vegetation_by_vi

    # Light diagnostic print so users can see what each stage decided.
    if np.isfinite(height_above).any():
        ha_min = float(np.nanmin(height_above))
        ha_max = float(np.nanmax(height_above))
    else:
        ha_min = ha_max = 0.0
    n_veg = int(vegetation_mask.sum())
    print("  [CSF]    ground={:,} ({:.1f}%)  non-ground={:,}"
          .format(int(ground_mask.sum()),
                  100.0 * ground_mask.sum() / n,
                  n - int(ground_mask.sum())),
          file=sys.stderr)
    print("  [DTM]    height-above range: {:.2f}m .. {:.2f}m"
          .format(ha_min, ha_max), file=sys.stderr)
    print("  [veg]    height in [{:.1f}, {:.1f}]m AND ExG >= {:.2f}: "
          "{:,} points ({:.1f}%)"
          .format(args.min_vegetation_height,
                  args.max_vegetation_height,
                  args.exg_threshold,
                  n_veg, 100.0 * n_veg / n),
          file=sys.stderr)

    return ground_mask, vegetation_mask, exg, is_vegetation_by_vi


def build_classification(ground_mask, vegetation_mask, is_vegetation_by_vi,
                         args):
    """Convert the pipeline outputs into a per-point class label.

    Three-class output (default):
        1 = ground            — CSF classified the point as ground.
        2 = vegetation        — non-ground AND height AND colour both
                                say "vegetation".
        3 = building/other    — non-ground AND NOT vegetation (the
                                point sits above the DTM but didn't
                                match the height+colour vegetation
                                test, so it's likely a building,
                                vehicle, lamp post, etc.).

    Parameters
    ----------
    ground_mask : np.ndarray of bool, shape (N,)
    vegetation_mask : np.ndarray of bool, shape (N,)
    is_vegetation_by_vi : np.ndarray of bool, shape (N,)
        Kept for backward compat with the old signature but no longer
        used to derive class=2 directly — we use ``vegetation_mask``
        instead, which is the height+colour intersection.

    Notes
    -----
    The old binary ``args.binary`` mode is removed in this rewrite: the
    physical-removal output is now the default and supersedes it. If
    a user really wants a binary 0/1 output they can pass
    ``--keep-all`` and threshold the ``classification`` column
    downstream.
    """
    n = len(ground_mask)
    cls = np.zeros(n, dtype=np.uint8)
    cls[ground_mask] = 1
    cls[~ground_mask & vegetation_mask] = 2
    cls[~ground_mask & ~vegetation_mask] = 3
    return cls


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def _build_arg_parser():
    p = argparse.ArgumentParser(
        description="Filter vegetation from a dense colored PLY point cloud "
                    "using CSF (Cloth Simulation Filter, Zhang et al. 2016) "
                    "for ground classification, then a height-AND-colour "
                    "test for vegetation.")
    p.add_argument('input', help='Input PLY file (with x/y/z + RGB).')
    p.add_argument('-o', '--output', required=True,
                   help='Output PLY file. By default vegetation-classified '
                        'points are physically removed (the output is a '
                        'smaller PLY containing only ground + building/other '
                        'points, ready to feed directly into downstream '
                        'tools like nn_change_analysis.py). Pass --keep-all '
                        'to keep every point and add a `classification` uchar '
                        'column instead.')

    # ---- CSF parameters (defaults match CloudCompare Relief preset) ----
    csf = p.add_argument_group('CSF (Cloth Simulation Filter) parameters',
                               'These control the ground/non-ground split. '
                               'Defaults match the CloudCompare "Relief" '
                               'preset: cloth_resolution=2, class_threshold='
                               '0.5, iterations=500, slope_smooth=False.')
    csf.add_argument('--csf-resolution', type=float, default=CSF_CLOTH_RESOLUTION,
                     help='Cloth grid spacing in metres (default %(default)s). '
                          'Smaller = finer cloth, more accurate on detailed '
                          'terrain but slower.')
    csf.add_argument('--csf-class-thr', type=float, default=CSF_CLASS_THRESHOLD,
                     help='Distance threshold (metres) for classifying a '
                          'point as ground vs non-ground (default %(default)s). '
                          'Higher = more aggressive ground labelling.')
    csf.add_argument('--csf-iterations', type=int, default=CSF_ITERATIONS,
                     help='Max cloth simulation iterations (default %(default)s).')
    csf.add_argument('--csf-slope-smooth', action='store_true',
                     help='Enable CSF slope post-processing (corresponds to '
                          'the "Slope processing" checkbox in CloudCompare). '
                          'Off by default — keep off to match CC Relief '
                          'defaults.')

    # ---- Vegetation height range ----
    veg = p.add_argument_group('Vegetation height range',
                               'A point is considered vegetation only if '
                               'its height-above-DTM is inside this range AND '
                               'its ExG >= --exg-threshold. Trees rarely '
                               'exceed 20m; anything below 0.5m is treated '
                               'as ground vegetation (grass).')
    veg.add_argument('--min-vegetation-height', type=float, default=MIN_VEG_HEIGHT_M,
                     help='Minimum height above DTM to count as vegetation, '
                          'in metres (default %(default)s).')
    veg.add_argument('--max-vegetation-height', type=float, default=MAX_VEG_HEIGHT_M,
                     help='Maximum height above DTM to count as vegetation, '
                          'in metres (default %(default)s).')
    veg.add_argument('--exg-threshold', type=float, default=EXG_THRESHOLD,
                     help='Excess Greenness threshold (default %(default)s). '
                          'Higher = stricter colour test.')

    # ---- DTM parameters ----
    dtm = p.add_argument_group('DTM interpolation parameters',
                               'After CSF labels ground points, an IDW DTM '
                               'is fitted on a regular grid and each point\'s '
                               'height above that DTM is computed.')
    dtm.add_argument('--dtm-res', type=float, default=DTM_GRID_RES,
                     help='DTM grid resolution in metres (default %(default)s). '
                          'Matches --csf-resolution by default.')

    # ---- Output format ----
    p.add_argument('--ascii', action='store_true',
                   help='Write ASCII PLY (default is binary little-endian).')
    p.add_argument('--keep-all', action='store_true',
                   help='Keep every point in the output and add a '
                        '`classification` uchar field (1=ground, '
                        '2=vegetation, 3=building/other). Default: '
                        'physically remove vegetation points so the output '
                        'PLY contains only ground + building/other.')
    p.add_argument('--no-vegindex', action='store_true',
                   help='Do not write the per-point `vegindex` float field '
                        'to the output PLY (only relevant with --keep-all).')
    return p


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)

    t0 = time.time()
    print("Reading {} ...".format(args.input))
    x, y, z, r, g, b, plydata = read_ply(args.input)
    n = len(x)
    print("  {} points, fields: {}"
          .format(n, list(plydata['vertex'].data.dtype.names)))

    print("Filtering (CSF + height[{}, {}] AND ExG >= {}) ..."
          .format(args.min_vegetation_height,
                  args.max_vegetation_height,
                  args.exg_threshold), file=sys.stderr)

    (ground_mask, vegetation_mask, exg,
     is_vegetation_by_vi) = run_filter(
        x, y, z, r, g, b, args)

    classification = build_classification(
        ground_mask, vegetation_mask, is_vegetation_by_vi, args)

    if args.keep_all:
        # Print class histogram
        unique, counts = np.unique(classification, return_counts=True)
        labels = {1: 'ground', 2: 'vegetation', 3: 'building/other'}
        parts = []
        for cls, count in zip(unique, counts):
            name = labels.get(int(cls), 'unknown')
            parts.append("{}={} ({:.1f}%)"
                         .format(name, int(count), 100.0 * count / n))
        print("Result: " + ", ".join(parts))

        print("Writing {} ...".format(args.output))
        vegindex_to_write = None if args.no_vegindex else exg
        write_ply_with_classification(
            plydata, classification, args.output,
            ascii_format=args.ascii, vegindex=vegindex_to_write,
        )
    else:
        # Default: physically drop vegetation points (class 2) so the
        # output PLY is smaller and downstream tools can consume it
        # without knowing what "vegetation" means.
        keep_mask = (classification != 2)
        n_kept = int(keep_mask.sum())
        n_dropped = n - n_kept
        n_ground = int((classification == 1).sum())
        n_building = int((classification == 3).sum())
        print("Result: kept {} ({:.1f}%) points — "
              "ground={:,}, building/other={:,}; "
              "dropped {} vegetation points"
              .format(n_kept, 100.0 * n_kept / n,
                      n_ground, n_building, n_dropped))

        print("Writing {} ...".format(args.output))
        write_ply_filtered(plydata, keep_mask, args.output,
                           ascii_format=args.ascii)

    print("Done in {:.1f}s.".format(time.time() - t0))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        raise