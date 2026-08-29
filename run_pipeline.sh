#!/usr/bin/env bash
# End-to-end manual pipeline runner for a single 3D Tiles change-detection
# task. Reads a `request.json` (the same payload the service ingests via
# `task_manager.py`), runs all four algorithm stages in-process, and keeps
# every intermediate PLY/npy on disk for post-mortem debugging.
#
# Stages (driven by `scripts/algorithm/run_pipeline.py`):
#   1. extract_leaf_vertices      — leaf b3dm -> A/B point cloud
#   2. filter_vegetation          — drop vegetation from B in ENU
#   3. nn_change_analysis         — B -> A nearest-neighbour (B points
#                                   whose distance >= threshold survive)
#   4. convert_point_ecef_and_3dtiles — ENU -> ECEF, DBSCAN cluster,
#                                       drop noise, LAS, py3dtiles
#
# Usage:
#   ./run_pipeline.sh output/<taskId>/request.json
#   ./run_pipeline.sh output/20260811170311EF8936/request.json
#
# Output (default: <taskId>/):
#   <taskId>/intermediates/    — every stage's PLY + .npy
#   <taskId>/final.3dtiles/    — final 3D Tiles
#   <taskId>/instances.json    — DBSCAN cluster instances
#
# Env vars (all optional):
#   PYTHON         — Python interpreter to use (default: the project's
#                    conda env python, /root/miniconda3/.../bin/python)
#   KEEP_INTERMEDIATES — "0" to drop intermediates (default: keep them)
#   STAGE_TIMEOUT_S — kill the pipeline if it runs longer than this many
#                    seconds (default: 1800 = 30 min). Set to 0 to disable.
#   DEBUG_PORT     — if set, wrap the python invocation with
#                    ``python -m debugpy --listen $DEBUG_PORT
#                    --wait-for-client <run_pipeline.py> ...``
#                    so a remote debugger can attach BEFORE the pipeline
#                    starts. Usage::
#
#                      DEBUG_PORT=8092 \
#                        ./run_pipeline.sh output/<taskId>/request.json
#
#                    Then in your IDE / vscode launch.json add::
#
#                      "type": "python", "request": "attach",
#                      "connect": { "host": "localhost", "port": 8092 }
#
#                    ``--wait-for-client`` blocks until the IDE attaches,
#                    so set a breakpoint before launching the script.
#                    STAGE_TIMEOUT_S still applies (default 30 min).
#                    Set STAGE_TIMEOUT_S=0 to disable the wait timeout
#                    if you'll be stepping through for a long time.
#
# Exit codes:
#   0 — pipeline finished, instances.json + final.3dtiles produced
#   1 — request.json missing or malformed
#   2 — required input tileset paths don't exist
#   3 — pipeline crashed mid-stage (see tail of <taskId>/pipeline.log)
#   124 — STAGE_TIMEOUT_S hit

set -euo pipefail

# ───────────────────────────────────────────────────────────────────────
# Resolve repo root and Python interpreter
# ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Force the right conda env so subprocesses spawned by the algorithm
# (e.g. ``py3dtiles`` invoked from convert_point_ecef_and_3dtiles.py)
# resolve against the env that has them installed. Without this, a
# caller running ``bash run_pipeline.sh`` from the ``base`` conda env
# would land here with PATH still pointing at base's bin/, and Stage 4
# fails with ``未找到 py3dtiles``. We try, in order:
#   1. Honour $PYTHON env var if set + executable
#   2. /root/miniconda3/envs/illegal_construction_inspection/bin/python
#   3. PATH lookup for python (and verify it can import py3dtiles).
CONDA_ENV_NAME="${CONDA_ENV:-illegal_construction_inspection}"
PYTHON_BIN="${PYTHON:-}"

if [[ -n "$PYTHON_BIN" && ! -x "$PYTHON_BIN" ]]; then
    echo "FATAL: PYTHON env var points to a non-executable: $PYTHON_BIN" >&2
    exit 1
fi

if [[ -z "$PYTHON_BIN" ]]; then
    # Try the conda env interpreter that has py3dtiles installed.
    CONDA_PY="/root/miniconda3/envs/${CONDA_ENV_NAME}/bin/python"
    if [[ -x "$CONDA_PY" ]]; then
        PYTHON_BIN="$CONDA_PY"
    else
        # Fall back to whatever `python` is on PATH, but warn loudly.
        PYTHON_BIN="$(command -v python3 || command -v python || true)"
        if [[ -z "$PYTHON_BIN" ]]; then
            echo "FATAL: no python found. Set PYTHON env var or activate" >&2
            echo "       the '$CONDA_ENV_NAME' conda env first." >&2
            exit 1
        fi
        echo "WARN: conda env '$CONDA_ENV_NAME' not found; falling back to $PYTHON_BIN" >&2
        echo "      Stage 4 may fail if py3dtiles is not installed there." >&2
    fi
fi

# Verify py3dtiles is importable. convert_las_to_3dtiles shells out to
# ``py3dtiles`` CLI; if it's missing the algorithm crashes deep in
# Stage 4 with the cryptic "未找到 py3dtiles" message — better to fail
# loudly here with an actionable hint.
if ! "$PYTHON_BIN" -c "import py3dtiles" >/dev/null 2>&1; then
    echo "FATAL: py3dtiles is NOT importable in $PYTHON_BIN" >&2
    echo "       Run: conda activate $CONDA_ENV_NAME && pip install py3dtiles" >&2
    echo "       (or set PYTHON to a python that has py3dtiles installed)" >&2
    exit 1
fi

# Prepend the python's bin/ to PATH so subprocess calls inside the
# python process (e.g. ``shutil.which("py3dtiles")``) can resolve the
# CLI entry-point. Without this, Stage 4 crashes with "未找到 py3dtiles"
# even when the import works, because the python process inherits the
# shell's PATH which may not include the conda env's bin/ directory.
_CONDA_BIN_DIR="$(dirname "$PYTHON_BIN")"
if [[ -x "$_CONDA_BIN_DIR/py3dtiles" ]]; then
    export PATH="$_CONDA_BIN_DIR:$PATH"
fi
unset _CONDA_BIN_DIR

# ───────────────────────────────────────────────────────────────────────
# Parse CLI
# ───────────────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    cat <<EOF
Usage: $0 <request.json> [extra args passed to run_pipeline.py]

Example:
  $0 output/20260811170311EF8936/request.json
EOF
    exit 1
fi
REQ_JSON="$1"
shift || true

if [[ ! -f "$REQ_JSON" ]]; then
    echo "FATAL: request.json not found: $REQ_JSON" >&2
    exit 1
fi

# ───────────────────────────────────────────────────────────────────────
# Parse request.json (using Python — keeps JSON parsing robust to quoting)
# ───────────────────────────────────────────────────────────────────────
read -r TASK_ID BASE_MODEL COMPARE_MODEL POSITION_MODE RADIUS AREA_JSON < <(
    "$PYTHON_BIN" - "$REQ_JSON" <<'PY'
import json, sys
req = json.load(open(sys.argv[1]))
def coalesce(*vals):
    for v in vals:
        if v is not None and v != "":
            return v
    return ""
print(
    req.get("taskId", ""),
    coalesce(req.get("baseModelPathResolved"), req.get("baseModelPath")),
    coalesce(req.get("compareModelPathResolved"), req.get("compareModelPath")),
    req.get("positionMode") or "",
    str(req.get("radius") or ""),
    json.dumps(req.get("areaCoordinates") or []),
)
PY
)

if [[ -z "$TASK_ID" ]]; then
    echo "FATAL: request.json has no 'taskId' field" >&2
    exit 1
fi
if [[ -z "$BASE_MODEL" || -z "$COMPARE_MODEL" ]]; then
    echo "FATAL: request.json missing baseModelPath/compareModelPath" >&2
    exit 1
fi
if [[ ! -d "$BASE_MODEL" ]]; then
    echo "FATAL: base tileset dir does not exist: $BASE_MODEL" >&2
    exit 2
fi
if [[ ! -d "$COMPARE_MODEL" ]]; then
    echo "FATAL: compare tileset dir does not exist: $COMPARE_MODEL" >&2
    exit 2
fi

OUT_DIR="$REPO_ROOT/output/$TASK_ID"
mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/pipeline.log"

echo "================================================================"
echo "taskId            : $TASK_ID"
echo "baseModelPath     : $BASE_MODEL"
echo "compareModelPath  : $COMPARE_MODEL"
echo "positionMode      : ${POSITION_MODE:-<unset>}"
echo "radius            : ${RADIUS:-<unset>}"
echo "areaCoordinates   : $AREA_JSON"
echo "out_dir           : $OUT_DIR"
echo "log_file          : $LOG_FILE"
echo "python            : $PYTHON_BIN"
echo "debug_port        : ${DEBUG_PORT:-<off>}"
echo "stage_timeout_s   : ${STAGE_TIMEOUT_S:-1800}"
echo "================================================================"

# ───────────────────────────────────────────────────────────────────────
# Compose run_pipeline.py args
# ───────────────────────────────────────────────────────────────────────
PIPELINE_SCRIPT="$REPO_ROOT/scripts/algorithm/run_pipeline.py"
if [[ ! -f "$PIPELINE_SCRIPT" ]]; then
    echo "FATAL: run_pipeline.py not found at $PIPELINE_SCRIPT" >&2
    exit 1
fi

# Keep-intermediates flag (default ON; honour env override)
if [[ "${KEEP_INTERMEDIATES:-1}" == "0" ]]; then
    KEEP_FLAG="--no-keep-intermediates"
else
    KEEP_FLAG="--keep-intermediates"
fi

PIPELINE_ARGS=(
    "$PIPELINE_SCRIPT"
    "$BASE_MODEL"
    "$COMPARE_MODEL"
    -o "$OUT_DIR"
    "$KEEP_FLAG"
    --area-coordinates "$AREA_JSON"
)
if [[ -n "$POSITION_MODE" ]]; then
    PIPELINE_ARGS+=(--position-mode "$POSITION_MODE")
fi
if [[ -n "$RADIUS" ]]; then
    PIPELINE_ARGS+=(--radius "$RADIUS")
fi
# Pass through any extra args (advanced overrides)
PIPELINE_ARGS+=("$@")

# ───────────────────────────────────────────────────────────────────────
# Run pipeline (with optional wall-clock timeout + debugpy attach)
# ───────────────────────────────────────────────────────────────────────
STAGE_TIMEOUT_S="${STAGE_TIMEOUT_S:-1800}"

# Compose the actual python command line. If DEBUG_PORT is set, prefix
# the launch with ``python -m debugpy --listen $DEBUG_PORT
# --wait-for-client`` so a remote debugger can attach BEFORE the
# algorithm starts. ``--wait-for-client`` blocks the python process
# on a socket accept() until the IDE connects, which is what makes
# "set breakpoint, then run the script" work; without it the script
# would race past the breakpoint during the IDE's TCP connect.
DEBUG_ARGS=()
if [[ -n "${DEBUG_PORT:-}" ]]; then
    if ! "$PYTHON_BIN" -c "import debugpy" >/dev/null 2>&1; then
        echo "FATAL: DEBUG_PORT=$DEBUG_PORT set but debugpy is not importable" >&2
        echo "       in $PYTHON_BIN. Run: pip install debugpy" >&2
        exit 1
    fi
    DEBUG_ARGS=(-m debugpy --listen "$DEBUG_PORT" --wait-for-client)
    echo "[debug] debugpy enabled — listening on 0.0.0.0:$DEBUG_PORT,"
    echo "        blocking until a debugger client attaches"
fi

echo "[$(date -Iseconds)] pipeline starting" | tee -a "$LOG_FILE"
echo "cmd: $PYTHON_BIN ${DEBUG_ARGS[*]:-} ${PIPELINE_ARGS[*]}" | tee -a "$LOG_FILE"

set +e
if [[ "$STAGE_TIMEOUT_S" == "0" ]]; then
    "$PYTHON_BIN" "${DEBUG_ARGS[@]}" "${PIPELINE_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
    rc=${PIPESTATUS[0]}
else
    # `timeout` returns 124 on kill; we'll remap to 3 below.
    timeout "$STAGE_TIMEOUT_S" \
        "$PYTHON_BIN" "${DEBUG_ARGS[@]}" "${PIPELINE_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
    rc=${PIPESTATUS[0]}
fi
set -e

echo "[$(date -Iseconds)] pipeline exited rc=$rc" | tee -a "$LOG_FILE"

# ───────────────────────────────────────────────────────────────────────
# Summarise outcome
# ───────────────────────────────────────────────────────────────────────
if [[ $rc -eq 0 ]]; then
    echo "================================================================"
    echo "Pipeline OK"
    echo "  instances.json: $OUT_DIR/instances.json"
    echo "  final tiles  : $OUT_DIR/final.3dtiles/"
    echo "  intermediates: $OUT_DIR/intermediates/"
    echo "================================================================"
    exit 0
elif [[ $rc -eq 124 ]]; then
    echo "FATAL: pipeline killed by STAGE_TIMEOUT_S=$STAGE_TIMEOUT_S" >&2
    echo "       tail of $LOG_FILE:" >&2
    tail -n 50 "$LOG_FILE" >&2 || true
    exit 124
else
    echo "FATAL: pipeline crashed (rc=$rc); tail of $LOG_FILE:" >&2
    tail -n 80 "$LOG_FILE" >&2 || true
    exit 3
fi