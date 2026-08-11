"""Single source of truth for algorithm tuning parameters.

All values can be overridden via environment variables (e.g. ``export
ALGO_DBSCAN_EPS_M=2.5``). Defaults match the values previously embedded
in :mod:`run_pipeline._CONFIG` and the per-file CLI defaults; behavior
is unchanged unless you set an env var.

Adding a new tunable? Add a ``_env(...)`` line below + (optionally)
document an env var, and import it where used. Do NOT sprinkle magic
numbers across the algorithm files — extend this module instead.
"""
from __future__ import annotations

import os
from typing import Any


def _env(name: str, default: Any) -> Any:
    """Read ``$name`` and coerce to ``type(default)``. Falls back to
    ``default`` if unset or unparseable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        if isinstance(default, bool):
            return raw.strip().lower() in ("1", "true", "yes", "y", "on")
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


# ── Stage 1: extract_leaf_vertices ───────────────────────────────
EXTRACT_DETECT_SAMPLES  = _env("ALGO_EXTRACT_DETECT_SAMPLES", 8)
# v0.8-introduced regression (R2): on a 128-core box the prior ``8`` capped
# inner-pool concurrency to ~6% of the machine, making Pass 2 wall-time scale
# with worker count instead of CPU count. Cap at 64 (the empirical "NFS RPC
# safe" upper bound — keeps the kernel from being asked to dispatch more
# in-flight syscalls than it can serve against the model mount). On dev
# machines (4-8 cores) this still bottoms out at the auto-detected count.
EXTRACT_MAX_WORKERS     = _env("ALGO_EXTRACT_MAX_WORKERS",
                               min(os.cpu_count() or 8, 64))
EXTRACT_MIN_LEAVES      = _env("ALGO_EXTRACT_MIN_LEAVES",     30)
EXTRACT_CHUNK_DIVISOR   = _env("ALGO_EXTRACT_CHUNK_DIVISOR",  8)

# ── Stage 2: filter_vegetation ───────────────────────────────────
CSF_CLOTH_RESOLUTION    = _env("ALGO_CSF_CLOTH_RESOLUTION",    2.0)
CSF_CLASS_THRESHOLD     = _env("ALGO_CSF_CLASS_THRESHOLD",     0.5)
CSF_ITERATIONS          = _env("ALGO_CSF_ITERATIONS",          500)
CSF_SUBSAMPLE_RES       = _env("ALGO_CSF_SUBSAMPLE_RES",       1.0)
CSF_BSLOOP_SMOOTH       = _env("ALGO_CSF_BSLOOP_SMOOTH",       "false").lower() == "true"
CSF_RIGIDNESS           = _env("ALGO_CSF_RIGIDNESS",           2)
CSF_TIME_STEP           = _env("ALGO_CSF_TIME_STEP",           0.65)
DTM_GRID_RES            = _env("ALGO_DTM_GRID_RES",            2.0)
MIN_VEG_HEIGHT_M        = _env("ALGO_MIN_VEG_HEIGHT_M",        0.5)
MAX_VEG_HEIGHT_M        = _env("ALGO_MAX_VEG_HEIGHT_M",        20.0)
EXG_THRESHOLD           = _env("ALGO_EXG_THRESHOLD",           0.05)

# ── Stage 3: nn_change_analysis ──────────────────────────────────
NN_MIN_DISTANCE_M       = _env("ALGO_NN_MIN_DISTANCE_M",       1.5)
NN_LEAFSIZE             = _env("ALGO_NN_LEAFSIZE",             32)
NN_CPU_FALLBACK         = _env("ALGO_NN_CPU_FALLBACK",         4)

# ── Stage 4: convert_point_ecef_and_3dtiles ──────────────────────
DBSCAN_EPS_M            = _env("ALGO_DBSCAN_EPS_M",            3.0)
DBSCAN_MIN_POINTS       = _env("ALGO_DBSCAN_MIN_POINTS",       120)
# Voxel size (metres) used to decimate the ENU point cloud before
# running open3d's cluster_dbscan. open3d's C++ implementation
# materialises the entire ε-neighbour graph (~4 B/pt + 4 B/edge) which
# on dense tilesets (NN 0.05–0.1 m) ballooned Stage 4 RSS to 40+ GiB
# and got the cgroup OOM-killer invoked. Voxel-decimating to one
# representative per ``DBSCAN_VOXEL_M`` cube drops the cluster input
# ~100× (e.g. B's 10M points → ~50–80k voxels at 0.5 m, ~500k voxels at
# 0.1 m) and the resulting cluster labels are back-projected to every
# original point via cKDTree.query(k=1). Centroid error is bounded by
# 1×voxel, cluster count typically matches the undecimated result to
# within ±5% on urban SfM (B's 0.05–0.1 m NN), but the back-projection
# step "tiles" each cluster to its NN-neighbour representative so
# hull/bbox shape diverges slightly more as voxel grows. **2026-07-28
# default changed 0.5 → 0.1** for finer cluster boundaries; at 0.1 m
# Stage 4 peak on B-class tilesets (~10 M pts_diff) is empirically
# ~25–35 GiB, still under the 64 GiB cgroup cap but tighter than 0.5.
# Set ``ALGO_DBSCAN_VOXEL_M=0`` to disable decimation entirely
# (fallback path: feeds the full N to cluster_dbscan; original OOM
# behaviour, useful only for very sparse point clouds).
DBSCAN_VOXEL_M          = _env("ALGO_DBSCAN_VOXEL_M",          0.1)
HULL_PARALLEL_MIN_N     = _env("ALGO_HULL_PARALLEL_MIN_N",     8)
LAS_SCALE_M             = _env("ALGO_LAS_SCALE_M",             0.001)

# ── Driver (run_pipeline.py) ─────────────────────────────────────
PARALLEL_CPU_THRESHOLD  = _env("ALGO_PARALLEL_CPU_THRESHOLD",  4)


__all__ = [
    # extract_leaf_vertices
    "EXTRACT_DETECT_SAMPLES", "EXTRACT_MAX_WORKERS",
    "EXTRACT_MIN_LEAVES", "EXTRACT_CHUNK_DIVISOR",
    # filter_vegetation
    "CSF_CLOTH_RESOLUTION", "CSF_CLASS_THRESHOLD", "CSF_ITERATIONS",
    "CSF_SUBSAMPLE_RES", "CSF_BSLOOP_SMOOTH", "CSF_RIGIDNESS",
    "CSF_TIME_STEP", "DTM_GRID_RES",
    "MIN_VEG_HEIGHT_M", "MAX_VEG_HEIGHT_M", "EXG_THRESHOLD",
    # nn_change_analysis
    "NN_MIN_DISTANCE_M", "NN_LEAFSIZE", "NN_CPU_FALLBACK",
    # convert_point_ecef_and_3dtiles
    "DBSCAN_EPS_M", "DBSCAN_MIN_POINTS", "DBSCAN_VOXEL_M",
    "HULL_PARALLEL_MIN_N", "LAS_SCALE_M",
    # driver
    "PARALLEL_CPU_THRESHOLD",
]