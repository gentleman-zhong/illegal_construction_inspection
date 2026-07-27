"""Stage metadata — single source of truth for stage names + progress ranges.

Imported by both :mod:`task_manager` (parent process, parses subprocess
stdout) and :mod:`run_pipeline_subprocess` (writes sidecar
``status.json``).

Keep these two dicts in sync with :mod:`scripts.algorithm.run_pipeline`'s
stage labels: ``'extract_leaf_vertices'``, ``'filter_vegetation'``,
``'nn_change_analysis'``, ``'convert_point_ecef_and_3dtiles'``. The
labels are also referenced from docs (``BACKEND_API.md`` §3,
``OPERATIONS.md`` §8).
"""

# Stage internal name → human-readable step for backend polling responses.
STAGE_FRIENDLY: dict[str, str] = {
    "extract_leaf_vertices":          "point cloud extraction",
    "filter_vegetation":              "vegetation filtering",
    "nn_change_analysis":             "change detection",
    "convert_point_ecef_and_3dtiles": "3d tiles generation",
}

# Stage name → (start_progress, end_progress) for the public progress
# meter (returned in poll responses as the ``progress`` string).
# Finalize (95 → 100) lives in the reader thread, not here.
STAGE_PROGRESS: dict[str, tuple[int, int]] = {
    "extract_leaf_vertices":          (0,  30),
    "filter_vegetation":              (30, 55),
    "nn_change_analysis":             (55, 80),
    "convert_point_ecef_and_3dtiles": (80, 95),
}


__all__ = ["STAGE_FRIENDLY", "STAGE_PROGRESS"]