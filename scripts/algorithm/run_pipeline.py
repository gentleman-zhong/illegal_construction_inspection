#!/usr/bin/env python3
"""End-to-end pipeline orchestrator: two 3D Tiles roots -> colored 3D Tiles + DBSCAN instances.

Composes four existing scripts as pure in-memory stages so the point
cloud flows through the pipeline exactly once (no PLY hand-offs between
stages). The intermediate PLY/npy dumps are opt-in via
``--keep-intermediates`` and live in ``<out_dir>/intermediates/``.

Pipeline order:

    1. extract_leaf_vertices      (tileset_a, tileset_b)
    2. filter_vegetation          (drops vegetation from B in ENU)
    3. nn_change_analysis         (B -> A nearest-neighbour, keeps
                                  B points whose distance >= the
                                  change-detection threshold)
    4. convert_point_ecef_and_3dtiles
                                 (ENU -> ECEF, DBSCAN cluster,
                                  drop noise, LAS, py3dtiles convert)

Algorithm parameters are kept as static module-level constants
(see :data:`_CONFIG`) so the pipeline is reproducible without CLI flags.
Only ``tileset_a``, ``tileset_b``, ``out_dir`` and
``--keep-intermediates`` are configurable from the command line.

Usage::

    python run_pipeline.py <tileset_a> <tileset_b> -o <out_dir> [--keep-intermediates]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

# Repository-relative imports: this file lives in scripts/, alongside the
# four pipeline libraries. Add this directory to sys.path so the imports
# resolve regardless of how the script is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from point_cloud_extraction import (  # noqa: E402
    extract_point_cloud,
    load_root_transform,
)
from filter_vegetation import (  # noqa: E402
    classify_ground_csf,
    compute_exg,
    compute_height_above_dtm,
)
from convert_point_ecef_and_3dtiles import (  # noqa: E402
    _estimate_peak_gib,
    _read_cgroup_memory_max_gib,
    cluster_instances,
    convert_las_to_3dtiles,
    save_ecef_arrays_to_las,
    write_instances_json,
    write_ply,
)
from roi import (  # noqa: E402
    ROIOpts,
    parse_area_coordinates,
    points_in_polygon,
    polygon_to_b_enu,
)
from algo_config import (                            # noqa: E402
    CSF_CLASS_THRESHOLD,
    CSF_CLOTH_RESOLUTION,
    CSF_ITERATIONS,
    CSF_SUBSAMPLE_RES,
    DBSCAN_EPS_M,
    DBSCAN_MIN_POINTS,
    DBSCAN_VOXEL_M,
    DTM_GRID_RES,
    EXG_THRESHOLD,
    MAX_VEG_HEIGHT_M,
    MIN_VEG_HEIGHT_M,
    NN_LEAFSIZE,
    NN_MIN_DISTANCE_M,
    PARALLEL_CPU_THRESHOLD,
)
from scipy.spatial import cKDTree


# =============================================================================
# Static algorithm parameters
#
# Values live in `algo_config` so they can be overridden via ALGO_* env
# vars. This thin namespace is kept for backward-compat with external
# callers that read `run_pipeline._CONFIG.X`; every attribute reads
# through to algo_config at access time.
# =============================================================================
class _CONFIG:
    CSF_CLOTH_RESOLUTION = CSF_CLOTH_RESOLUTION
    CSF_CLASS_THRESHOLD  = CSF_CLASS_THRESHOLD
    CSF_ITERATIONS       = CSF_ITERATIONS
    CSF_SUBSAMPLE_RES    = CSF_SUBSAMPLE_RES
    DTM_GRID_RES         = DTM_GRID_RES
    MIN_VEG_HEIGHT_M     = MIN_VEG_HEIGHT_M
    MAX_VEG_HEIGHT_M     = MAX_VEG_HEIGHT_M
    EXG_THRESHOLD        = EXG_THRESHOLD
    NN_MIN_DISTANCE_M    = NN_MIN_DISTANCE_M
    DBSCAN_EPS_M         = DBSCAN_EPS_M
    DBSCAN_MIN_POINTS    = DBSCAN_MIN_POINTS
    DBSCAN_VOXEL_M       = DBSCAN_VOXEL_M


# -----------------------------------------------------------------------------
# Inter-stage scratchpad
# -----------------------------------------------------------------------------
@dataclass
class _Bag:
    """Per-stage scratchpad with explicit field-by-field release.

    Why a dataclass (and not the previous ``stage_results: dict[str, tuple]``)
    ------------------------------------------------------------------------
    The old code stored each stage's outputs in a dict, e.g.::

        stage_results["extract_leaf_vertices"] = (
            pts_a_aligned, colors_a, pts_b, colors_b, transform_b,
        )

    and unpicked them with ``_, _, pts_b, colors_b, _ = ...`` — the
    ``_`` lookalikes looked like they dropped a 1.6 GiB array but the
    tuple was still in the dict holding the reference alive, so the
    array was kept until *the next* stage's outputs replaced the dict
    entry. Peak RSS for a 50 M-point run was inflated by 1.6 GiB
    because of this retention.

    The new ``_Bag`` has all ``Optional[np.ndarray] = None`` fields
    that each stage writes and each downstream stage explicitly
    drops by setting ``bag.pts_b = None`` (CPython refcount drops
    the array immediately — no GC pause, no dict holding it alive).
    A test (``tests/algorithm/test_orchestrator_bag_free.py``) asserts
    the release points.

    Fields mirror the four stage signatures verbatim so the
    orchestrator can read them by attribute name and stay human-readable.
    """
    pts_a_aligned:  Optional[np.ndarray] = None  # Stage 1 → Stage 3
    colors_a:       Optional[np.ndarray] = None  # Stage 1 (kept for symmetry)
    pts_b:          Optional[np.ndarray] = None  # Stage 1 → Stage 2
    colors_b:       Optional[np.ndarray] = None  # Stage 1 → Stage 2
    pts_filt:       Optional[np.ndarray] = None  # Stage 2 → Stage 3
    rgb_filt:       Optional[np.ndarray] = None  # Stage 2 → Stage 3
    pts_diff:       Optional[np.ndarray] = None  # Stage 3 → Stage 4
    rgb_diff:       Optional[np.ndarray] = None  # Stage 3 → Stage 4
    transform_b:    Optional[list]        = None  # Stage 1 → Stage 4

    def reset(self) -> None:
        """Drop every field's reference. Used at the very end of main()
        so the orchestrator's own frame doesn't hold 10s of GiB of
        numpy arrays while uvicorn-style wrappers wait for the
        subprocess to exit."""
        for f in self.__dataclass_fields__:
            setattr(self, f, None)


# =============================================================================
# Stage helpers
# =============================================================================
def _write_xyzrgb_ply(path: Path, pts: np.ndarray, rgb: np.ndarray) -> None:
    """Write a minimal ASCII PLY with ``float x y z`` + ``uchar red green blue``.

    Replicates extract_leaf_vertices._write_xyzrgb_ply (kept private there)
    so the pipeline does not need to import underscored helpers.
    """
    pts = pts.astype(np.float32, copy=False)
    rgb = rgb.astype(np.uint8, copy=False)
    rows = np.column_stack([pts, rgb])
    with path.open("w") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write(f"element vertex {len(pts)}\n")
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("property uchar red\nproperty uchar green\nproperty uchar blue\n")
        f.write("end_header\n")
        np.savetxt(f, rows,
                   fmt=["%.4f", "%.4f", "%.4f", "%d", "%d", "%d"],
                   delimiter=" ")


def _maybe_dump_xyzrgb(inter_dir: Path | None, stem: str,
                       pts: np.ndarray, rgb: np.ndarray) -> None:
    if inter_dir is None:
        return
    _write_xyzrgb_ply(inter_dir / f"{stem}.ply", pts, rgb)


def _run_stage(label: str, fn: Callable[..., Any],
               *args: Any, **kwargs: Any) -> tuple[Any, float]:
    """Invoke ``fn`` and report per-stage start/done + elapsed seconds.

    Returns ``(fn_result, elapsed_seconds)``.
    """
    print(f"[{label}] starting…", flush=True)
    t0 = time.time()
    out = fn(*args, **kwargs)
    elapsed = time.time() - t0
    print(f"[{label}] done in {elapsed:6.1f}s", flush=True)
    return out, elapsed


# =============================================================================
# Stage 1: extract POSITION + RGB from both 3D Tiles roots (in memory)
# =============================================================================
def _extract_one_tileset_for_pool(tileset_root: Path):
    """Pool-friendly wrapper around ``extract_point_cloud``.

    Module-level (no closure state) so ``ProcessPoolExecutor`` can
    pickle the reference when fan-out runs in a ``spawn`` context.
    Pinned kwargs match what ``stage_extract`` historically passed
    when calling inline.
    """
    return extract_point_cloud(
        tileset_root, progress=False, with_color=True,
    )


def stage_extract(tileset_a: Path, tileset_b: Path, inter_dir: Path | None,
                  *, bag: _Bag, roi: ROIOpts | None = None,
                  ) -> None:
    """Extract both epochs and align A's local ENU into B's local ENU.

    The two tilesets each carry their own ``root.transform`` (the
    16-float column-major 4×4 mapping that tileset-local ENU -> ECEF).
    The model centroids on the WGS84 surface are *almost* identical
    but not bit-exact, so the two local ENU frames differ by a small
    rotation + translation. Reading both clouds with the bare
    ``extract_point_cloud`` and feeding them straight into a NN query
    means the KD-tree treats the two slightly-offset local origins as
    the same frame, which silently inflates every distance estimate.

    We fix this exactly the way :func:`extract_pair_aligned` does:
    compose ``T = inv(T_b) @ T_a`` (A's local ENU -> B's local ENU)
    and apply it to ``points_a`` so both clouds live in B's local ENU.
    RGB is invariant under rigid transforms so ``colors_a`` is reused
    as-is. Only ``transform_b`` is returned, since it is the canonical
    transform for stage 4's ENU -> ECEF matmul.

    When the host has at least ``algo_config.PARALLEL_CPU_THRESHOLD`` CPU
    cores the two extracts run in parallel via a 2-worker ``fork`` pool —
    each one is fully independent so fan-out gives an effective ~2× speedup
    over the serial path on 2-core hosts and lets Pass-2's intra-tileset
    parallelism (see ``extract_point_cloud(workers=...)``) actually
    overlap with the other tileset's Pass-2 work on bigger hosts.

    When ``roi`` is active, both clouds are masked to the ROI polygon
    *before* returning, so all downstream stages (vegetation filter,
    NN, DBSCAN) operate only on the ROI subset and the final output
    naturally contains only ROI changes.

    Returns
    -------
    pts_a_aligned, rgb_a, pts_b, rgb_b, transform_b
    """
    cpu = os.cpu_count() or 1
    use_parallel = cpu >= PARALLEL_CPU_THRESHOLD

    if use_parallel:
        # ProcessPoolExecutor (instead of multiprocessing.Pool) so its
        # workers are non-daemonic — that lets ``extract_point_cloud``'s
        # inner Pass-2 ProcessPoolExecutor spawn successfully from
        # within these workers (CPython forbids daemon processes from
        # spawning children).
        with ProcessPoolExecutor(max_workers=2) as pool:
            res_a, res_b = list(pool.map(
                _extract_one_tileset_for_pool,
                [tileset_a, tileset_b],
            ))
        points_a, colors_a, transform_a, _ = res_a
        points_b, colors_b, transform_b, _ = res_b
    else:
        points_a, colors_a, transform_a, _ = extract_point_cloud(
            tileset_a, progress=False, with_color=True,
        )
        points_b, colors_b, transform_b, _ = extract_point_cloud(
            tileset_b, progress=False, with_color=True,
        )

    T_a = np.asarray(transform_a, dtype=np.float64).reshape(4, 4, order="F")
    T_b = np.asarray(transform_b, dtype=np.float64).reshape(4, 4, order="F")
    T = np.linalg.inv(T_b) @ T_a          # A's ENU -> B's ENU

    pts_a_h = np.hstack([
        points_a.astype(np.float64, copy=False),
        np.ones((len(points_a), 1), dtype=np.float64),
    ])
    points_a_in_b = (pts_a_h @ T.T)[:, :3].astype(np.float32, copy=False)

    # Trace the alignment shift so the user can spot a mis-pair.
    shift = (points_a_in_b.mean(axis=0) - points_b.mean(axis=0))
    print(f"  A: {len(points_a_in_b):,} pts  (aligned to B's ENU)",
          flush=True)
    print(f"  B: {len(points_b):,} pts", flush=True)
    print(f"  centroid offset (A_in_b − B): "
          f"({shift[0]:+.3f}, {shift[1]:+.3f}, {shift[2]:+.3f}) m",
          flush=True)

    _maybe_dump_xyzrgb(inter_dir, "01_pts_a_in_b_rgb",
                       points_a_in_b, colors_a)
    _maybe_dump_xyzrgb(inter_dir, "01_pts_b_rgb", points_b, colors_b)

    # ----- ROI mask (Stage 1 exit) -----
    # Both A and B are masked to the polygon in B's local ENU so the
    # entire downstream pipeline (CSF vegetation filter → NN → DBSCAN)
    # only sees ROI points. This prevents the "stolen nearest neighbour"
    # effect (a B point near the ROI boundary finding a closer A point
    # outside the ROI and being mis-classified as "no change"), and
    # makes `instances.json` / `3DTiles` naturally ROI-only.
    if roi is not None and roi.active:
        keep_b = points_in_polygon(points_b[:, 0], points_b[:, 1],
                                   roi.polygon_enu)
        keep_a = points_in_polygon(points_a_in_b[:, 0], points_a_in_b[:, 1],
                                   roi.polygon_enu)
        points_b = points_b[keep_b]
        colors_b = colors_b[keep_b]
        # colors_a is paired with points_a in the same order, so the
        # mask indices apply 1:1.
        points_a_in_b = points_a_in_b[keep_a]
        colors_a = colors_a[keep_a]
        n_a_in = len(keep_a)
        n_b_in = len(keep_b)
        print(f"  [roi] stage_extract mask: "
              f"B kept {int(keep_b.sum()):,}/{n_b_in:,}  "
              f"A kept {int(keep_a.sum()):,}/{n_a_in:,}",
              flush=True)
        if len(points_b) == 0 or len(points_a_in_b) == 0:
            raise RuntimeError(
                "[roi] no points inside ROI after Stage 1 mask; "
                "check areaCoordinates vs tileset extent"
            )

    # Hand outputs to the bag for downstream stages; we keep a local
    # ref to drop the locals before returning.
    bag.pts_a_aligned = points_a_in_b
    bag.colors_a      = colors_a
    bag.pts_b         = points_b
    bag.colors_b      = colors_b
    bag.transform_b   = transform_b


# =============================================================================
# Stage 2: vegetation filter on B (CSF + height-above-DTM + ExG)
# =============================================================================
def stage_filter_vegetation(*, bag: _Bag, inter_dir: Path | None) -> None:
    pts_b  = bag.pts_b
    rgb_b  = bag.colors_b
    if pts_b is None or rgb_b is None:
        raise RuntimeError("stage_filter_vegetation: bag.pts_b / bag.colors_b are None")
    # Rescale uchar [0, 255] to uint16 [0, 65535] by *257 — same factor as
    # filter_vegetation._normalize_colors_to_uint16. Without it, ExG comes
    # out ~256× too small and the EXG gate silently never fires.
    r16 = (rgb_b[:, 0].astype(np.uint32) * 257).astype(np.uint16)
    g16 = (rgb_b[:, 1].astype(np.uint32) * 257).astype(np.uint16)
    b16 = (rgb_b[:, 2].astype(np.uint32) * 257).astype(np.uint16)

    # CSF subsampling: cloth simulation is invariant to point density
    # at fixed cloth_resolution, so feeding CSF a 1m voxel-grid
    # subsample (1 representative point per cubic metre) gives the
    # same ground seeds as the full cloud, in 1/30-1/45 of the time on
    # typical urban densities. The DTM is then interpolated from the
    # subset's ground points and re-evaluated at every original B
    # point via 2D nearest-neighbour (DTM grid is 2 m, so within a
    # 1 m voxel the DTM height is essentially uniform).
    sub_res = _CONFIG.CSF_SUBSAMPLE_RES
    if sub_res > 0:
        voxel_idx = np.floor(pts_b / sub_res).astype(np.int64)
        _, first_idx = np.unique(voxel_idx, axis=0, return_index=True)
        sub_pts = pts_b[np.sort(first_idx)]
    else:
        sub_pts = pts_b
    n_sub = len(sub_pts)
    print(f"  CSF input: {n_sub:,} pts (subsample {n_sub / len(pts_b) * 100:.1f}% "
          f"of {len(pts_b):,})", flush=True)

    ground_mask = classify_ground_csf(
        sub_pts[:, 0], sub_pts[:, 1], sub_pts[:, 2],
        cloth_resolution=_CONFIG.CSF_CLOTH_RESOLUTION,
        class_threshold=_CONFIG.CSF_CLASS_THRESHOLD,
        iterations=_CONFIG.CSF_ITERATIONS,
        bSloopSmooth=False,
        rigidness=2,
        time_step=0.65,
        verbose=False,
    )
    sub_h_above = compute_height_above_dtm(
        sub_pts[:, 0], sub_pts[:, 1], sub_pts[:, 2], ground_mask,
        grid_res=_CONFIG.DTM_GRID_RES,
    )
    # Map subset h_above back to every original B point (2D nearest-neighbour)
    tree = cKDTree(sub_pts[:, :2])
    _, nearest = tree.query(pts_b[:, :2], k=1, workers=-1)
    h_above = sub_h_above[nearest]
    del tree, nearest

    exg = compute_exg(r16, g16, b16)
    del r16, g16, b16  # uint16 colour scratch; not needed after ExG

    in_height = (
        np.isfinite(h_above)
        & (h_above >= _CONFIG.MIN_VEG_HEIGHT_M)
        & (h_above <= _CONFIG.MAX_VEG_HEIGHT_M)
    )
    is_veg_by_vi = exg >= _CONFIG.EXG_THRESHOLD
    veg_mask = in_height & is_veg_by_vi
    keep = ~veg_mask
    n_in = len(pts_b)
    n_out = int(keep.sum())
    print(f"  in={n_in:,}  dropped vegetation={n_in - n_out:,}  kept={n_out:,}",
          flush=True)

    pts_filt = pts_b[keep].astype(np.float32, copy=False)
    rgb_filt = rgb_b[keep]
    _maybe_dump_xyzrgb(inter_dir, "02_pts_b_no_veg", pts_filt, rgb_filt)

    # Publish outputs and drop the upstream B arrays — they're ~627 MiB
    # (pts_b) + 156 MiB (rgb_b) at typical B scale, and Stage 3 only
    # needs the filtered subset.
    bag.pts_filt  = pts_filt
    bag.rgb_filt  = rgb_filt
    bag.pts_b     = None
    bag.colors_b  = None


# =============================================================================
# Stage 3: B -> A nearest-neighbour, drop "near" pairs as background
# =============================================================================
def stage_nn(*, bag: _Bag, inter_dir: Path | None) -> None:
    pts_a     = bag.pts_a_aligned
    pts_b_filt = bag.pts_filt
    rgb_b_filt = bag.rgb_filt
    if pts_a is None or pts_b_filt is None or rgb_b_filt is None:
        raise RuntimeError("stage_nn: bag.pts_a_aligned / pts_filt / rgb_filt are None")
    # cKDTree accepts float32 — dropping the legacy ``astype(np.float64,
    # copy=False)`` promotion saves 1.4 GiB peak RSS at N_f ≈ 50 M
    # points (the B tileset, after the ROI mask and vegetation drop).
    tree = cKDTree(pts_a, leafsize=NN_LEAFSIZE)
    dist, _ = tree.query(pts_b_filt, k=1, workers=-1)
    keep = dist >= _CONFIG.NN_MIN_DISTANCE_M
    pts_diff = pts_b_filt[keep]
    rgb_diff = rgb_b_filt[keep]
    n_in = len(pts_b_filt)
    n_out = int(keep.sum())
    median_kept = float(np.median(dist[keep])) if n_out else 0.0
    print(f"  in={n_in:,}  kept diff={n_out:,}  median dist (kept)="
          f"{median_kept:.3f}m",
          flush=True)
    del tree, dist

    _maybe_dump_xyzrgb(inter_dir, "03_pts_diff", pts_diff, rgb_diff)

    # Publish + drop the upstream A and filtered B arrays — they're
    # ~114 MiB (A) + 600 MiB (pts_filt) + 150 MiB (rgb_filt) and Stage 4
    # only needs pts_diff / rgb_diff / transform_b.
    bag.pts_diff = pts_diff
    bag.rgb_diff = rgb_diff
    bag.pts_a_aligned = None
    bag.colors_a      = None
    bag.pts_filt      = None
    bag.rgb_filt      = None


# =============================================================================
# Stage 4: ENU -> ECEF + DBSCAN cluster + drop noise + write 3D Tiles
# =============================================================================
def _build_ecef_pl_props(pts: np.ndarray, rgb: np.ndarray | None,
                         ) -> tuple[dict, list]:
    """Build a ``(props_dict, prop_list)`` pair matching convert_point_ecef_…'s
    internal in-memory representation."""
    prop_list = [
        ("x", "float", 4, np.dtype("<f4")),
        ("y", "float", 4, np.dtype("<f4")),
        ("z", "float", 4, np.dtype("<f4")),
    ]
    props: dict[str, np.ndarray] = {
        "x": pts[:, 0].astype(np.float32),
        "y": pts[:, 1].astype(np.float32),
        "z": pts[:, 2].astype(np.float32),
    }
    if rgb is not None:
        prop_list += [
            ("red",   "uchar", 1, np.dtype("u1")),
            ("green", "uchar", 1, np.dtype("u1")),
            ("blue",  "uchar", 1, np.dtype("u1")),
        ]
        props["red"]   = rgb[:, 0].astype(np.uint8)
        props["green"] = rgb[:, 1].astype(np.uint8)
        props["blue"]  = rgb[:, 2].astype(np.uint8)
    return props, prop_list


def stage_convert(*, bag: _Bag, out_dir: Path, inter_dir: Path | None) -> None:
    pts_diff     = bag.pts_diff
    rgb_diff     = bag.rgb_diff
    transform_b  = bag.transform_b
    if pts_diff is None or transform_b is None:
        raise RuntimeError("stage_convert: bag.pts_diff / transform_b are None")

    # ---- Pre-flight: refuse if predicted peak would OOM ----
    # Empirical model (see convert_point_ecef_and_3dtiles._estimate_peak_gib);
    # we now have a much tighter bound thanks to the voxel-decimated
    # DBSCAN path, but the check is still the only thing that converts
    # the silent SIGKILL into a clean errorMessage the backend can act on.
    predicted = _estimate_peak_gib(
        len(pts_diff), n_clusters=0, dbscan_voxel_m=_CONFIG.DBSCAN_VOXEL_M,
    )
    cap_gib = _read_cgroup_memory_max_gib()
    if cap_gib is not None and predicted > 0.8 * cap_gib:
        raise RuntimeError(
            f"OOM: expected Stage 4 peak {predicted:.1f} GiB > 80% of cgroup "
            f"limit {cap_gib:.1f} GiB (pts_diff={len(pts_diff):,}, "
            f"voxel_m={_CONFIG.DBSCAN_VOXEL_M}). "
            f"Set ALGO_DBSCAN_VOXEL_M=0 to disable decimation, or run on a "
            f"host with more memory."
        )
    print(f"  [mem] predicted Stage 4 peak: {predicted:.1f} GiB "
          f"(cgroup cap: {cap_gib:.1f} GiB)" if cap_gib is not None
          else f"  [mem] predicted Stage 4 peak: {predicted:.1f} GiB "
               f"(cgroup cap: unmeasured)",
          flush=True)

    # 1. ENU -> ECEF — algebraic rewrite (saves 8 B/pt vs. the
    #    homogeneous hstack approach). The old code did
    #        homog = np.hstack([pts_diff, np.ones((N, 1))])
    #        ecef = (homog @ T.T)[:, :3]
    #    which materialised a (N, 4) intermediate that was dropped
    #    immediately, costing 8 B/pt of transient RSS. The
    #    affine-only split keeps the working set at (N, 3):
    T = np.asarray(transform_b, dtype=np.float64).reshape(4, 4, order="F")
    ecef = pts_diff @ T[:3, :3].T + T[3, :3]
    # (preserve pts_diff for cluster_instances — ecef needs the same N)

    # 2. DBSCAN cluster in ENU frame (now voxel-decimated internally)
    clusters_pre, labels = cluster_instances(
        pts_diff, ecef,
        eps=_CONFIG.DBSCAN_EPS_M,
        min_points=_CONFIG.DBSCAN_MIN_POINTS,
        voxel_m=_CONFIG.DBSCAN_VOXEL_M,
    )

    # 3. Drop DBSCAN noise (always required by this pipeline)
    keep = labels >= 0
    n_total = len(pts_diff)
    n_dropped = int((~keep).sum())
    n_kept = int(keep.sum())
    ecef_clean = ecef[keep]
    rgb_clean = rgb_diff[keep] if rgb_diff is not None else None
    # pts_diff and labels are no longer needed after the noise drop —
    # the LAS writer and the debug PLY only need the kept subset.
    del pts_diff, labels, ecef
    print(f"  DBSCAN: kept={n_kept:,}  dropped noise={n_dropped:,}  "
          f"clusters={len(clusters_pre)}",
          flush=True)

    # 4. Direct ECEF → LAS (skip the PLY round-trip — the open3d PLY
    #    reader is the slow part of save_ecef_ply_to_las).
    tmp_las = out_dir / (".ecef_temp.las" if inter_dir is None
                          else "intermediates/04_points_ecef.las")
    save_ecef_arrays_to_las(ecef_clean, rgb_clean, str(tmp_las))

    # 5. Optional debug PLY (only when keeping intermediates). Use the
    #    same ecef_clean we just wrote; drop it right after.
    if inter_dir is not None:
        props, prop_list = _build_ecef_pl_props(ecef_clean, rgb_clean)
        write_ply(inter_dir / "04_pts_ecef.ply", props, prop_list,
                  fmt="binary_little_endian")
    ecef_clean = None
    rgb_clean = None

    # 6. LAS → 3D Tiles via py3dtiles
    tiles_dir = out_dir / "3DTiles"
    pyres = convert_las_to_3dtiles(str(tmp_las), str(tiles_dir))
    if not pyres["ok"]:
        raise SystemExit(f"3D Tiles generation failed:\n{pyres['message']}")

    # 7. instances.json (write to out_dir/instances.json, not under
    #    tiles_dir/ — py3dtiles' --overwrite only wipes tiles_dir).
    write_instances_json(
        out_dir / "instances.json",
        clusters_pre,
        eps=_CONFIG.DBSCAN_EPS_M,
        min_points=_CONFIG.DBSCAN_MIN_POINTS,
        n_input_points=n_total,
    )
    # clusters_pre is a list of dicts holding bbox / hull data; release.
    del clusters_pre

    # 8. Cleanup: production mode deletes the scratch LAS; debug mode
    #    already has it at inter_dir/<intermediates>/04_points_ecef.las.
    if inter_dir is None and tmp_las.exists():
        tmp_las.unlink()

    # Drop the upstream scratch we no longer need.
    bag.pts_diff    = None
    bag.rgb_diff    = None
    bag.transform_b = None

    print(f"  3D Tiles: {tiles_dir}", flush=True)
    print(f"  instances.json: {out_dir / 'instances.json'}", flush=True)


# =============================================================================
# Driver
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_pipeline",
        description=(
            "End-to-end in-memory pipeline: two 3D Tiles roots -> "
            "colored 3D Tiles + DBSCAN instances.json. Algorithm "
            "parameters live in _CONFIG (this file); only "
            "input/output paths are configurable."
        ),
    )
    p.add_argument("tileset_a", type=Path,
                   help="Path to reference (epoch A) 3D Tiles root.")
    p.add_argument("tileset_b", type=Path,
                   help="Path to comparison (epoch B) 3D Tiles root.")
    p.add_argument("-o", "--out-dir", type=Path, required=True,
                   help="Output directory (3D Tiles, instances.json, "
                        "and intermediates/ land here).")
    p.add_argument("--keep-intermediates", dest="keep_intermediates",
                   action="store_true", default=True,
                   help="Persist stage-by-stage PLY + .npy intermediates "
                        "in <out_dir>/intermediates/. (default: ON)")
    p.add_argument("--no-keep-intermediates", dest="keep_intermediates",
                   action="store_false",
                   help="Discard intermediate PLY/LAS — only the final "
                        "3D Tiles and instances.json are kept.")
    # ----- 2026-07 新增: ROI 感兴趣区域 -----
    p.add_argument("--area-coordinates", default=None,
                   help="JSON-encoded ROI polygon: a list of "
                        "{latitude, longitude, altitude} dicts (WGS84, "
                        "≥3 vertices). When set, the pipeline only "
                        "inspects points inside this polygon.")
    p.add_argument("--position-mode", default=None,
                   help="Coordinate reference system identifier "
                        "(e.g. 'WGS-84'). Informational only.")
    p.add_argument("--radius", type=float, default=None,
                   help="Reserved for future use (e.g. outward buffer). "
                        "Currently logged and ignored.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    inter_dir: Path | None = (out_dir / "intermediates"
                               if args.keep_intermediates else None)
    if inter_dir is not None:
        inter_dir.mkdir(parents=True, exist_ok=True)

    # ----- ROI configuration -----
    # Parse + validate areaCoordinates up front so a malformed payload
    # fails the task at "starting" rather than mid-stage 1 (which would
    # leave a half-written intermediates/ on disk). parse_area_coordinates
    # returns None when --area-coordinates is empty; that disables ROI.
    try:
        roi_coords = parse_area_coordinates(args.area_coordinates)
    except ValueError as e:
        # surface as the run's error message — _run_stage prints
        # "[N/4 stage] starting…" then "done in Ns", so we let the
        # exception propagate and the subprocess wrapper writes it to
        # status.json / error.log.
        raise SystemExit(f"--area-coordinates invalid: {e}") from e

    roi = ROIOpts(
        position_mode=args.position_mode,
        area_coordinates=roi_coords,
        radius=args.radius,
    )
    if roi.radius is not None:
        print(f"[roi] radius={roi.radius} m received but currently ignored; "
              f"reserved for future use.", flush=True)
    if roi.area_coordinates is not None:
        print(f"[roi] {len(roi.area_coordinates)} vertices received "
              f"(positionMode={args.position_mode!r}); will project to "
              f"B's local ENU after Stage 1.", flush=True)

    t_pipeline = time.time()
    timings: list[tuple[str, float]] = []
    bag = _Bag()

    # ---- Start background RSS sampler ----
    # Records peak VmRSS once per second for the lifetime of the
    # subprocess. We use it to verify post-mortem that the algorithmic
    # memory fix actually worked (e.g. "B model peaked at 8.3 GiB,
    # well under the 64 GiB cap"). The sampler is a daemon thread
    # with no callbacks, so the worst-case impact is a single
    # 1-second-late read of /proc/self/status per second.
    try:
        from _rss_sampler import start as _rss_start
        _rss_start()
    except Exception as _rss_exc:
        # /proc not available (e.g. macOS dev box) — degrade silently.
        # Worst case we just don't get a peak-RSS line in the log.
        print(f"[rss] sampler disabled: {_rss_exc}", flush=True)

    # Single stage list — every entry carries (N/4 label, callable).
    # All stages now take their inputs from the bag (or kwargs) and
    # publish their outputs back to the bag, so the per-stage
    # signature is uniform: ``fn(bag=bag, ...)``.
    STAGES: list[tuple[str, Callable[..., Any], dict[str, Any]]] = [
        ("1/4 extract_leaf_vertices", stage_extract,
         {"tileset_a": args.tileset_a, "tileset_b": args.tileset_b,
          "inter_dir": inter_dir, "roi": roi}),
        ("2/4 filter_vegetation", stage_filter_vegetation,
         {"inter_dir": inter_dir}),
        ("3/4 nn_change_analysis", stage_nn,
         {"inter_dir": inter_dir}),
        ("4/4 convert_point_ecef_and_3dtiles", stage_convert,
         {"out_dir": out_dir, "inter_dir": inter_dir}),
    ]

    # Project the ROI polygon into B's ENU eagerly (needs T_b) so
    # stage_extract can mask synchronously.
    if roi.area_coordinates is not None and roi.polygon_enu is None:
        T_b_early = np.asarray(
            load_root_transform(args.tileset_b), dtype=np.float64,
        ).reshape(4, 4, order="F")
        roi.polygon_enu = polygon_to_b_enu(roi.area_coordinates, T_b_early)
        print(f"[roi] polygon projected to B's local ENU; "
              f"active={roi.active}", flush=True)

    for label, fn, stage_kwargs in STAGES:
        out, dt = _run_stage(label, fn, bag=bag, **stage_kwargs)
        timings.append((label, dt))

    # Final: release the bag's references so the orchestrator's own
    # frame doesn't hold 10s of GiB of numpy arrays while the subprocess
    # wrapper waits for it to exit.  Setting fields to None drops the
    # refcount to zero and the numpy arrays are freed immediately
    # (no GC pause, deterministic).
    bag.reset()
    del bag

    # Stop the RSS sampler and emit the final peak.
    try:
        from _rss_sampler import stop as _rss_stop
        _rss_stop()
    except Exception:
        pass

    total = time.time() - t_pipeline
    print()
    print("=" * 60)
    print("Pipeline summary")
    print("=" * 60)
    for name, dt in timings:
        print(f"  {name:<40s}  {dt:6.1f}s")
    print(f"  {'TOTAL':<40s}  {total:6.1f}s")
    print(f"\nOutputs:")
    print(f"  3D Tiles:    {out_dir / '3DTiles'}")
    print(f"  instances:   {out_dir / 'instances.json'}")
    if inter_dir is not None:
        print(f"  intermediates: {inter_dir}")
    if roi.active:
        print(f"\nROI:")
        print(f"  positionMode: {roi.position_mode}")
        print(f"  vertices:     {len(roi.polygon_enu)}")
        print(f"  radius:       ignored (reserved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
