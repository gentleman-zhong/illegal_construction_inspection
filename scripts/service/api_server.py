#!/usr/bin/env python3
"""FastAPI entry point for the illegal-construction inspection algorithm.

Implements the v4 contract (unified with the backend):

* ``POST /two-violation/compare`` (application/json) — submit a task
* ``GET  /two-violation/tasks/{taskId}`` — poll progress / get final URLs
* ``GET  /healthz`` — liveness

Response envelope is always ``{code, message, data}`` with two terminal
states: ``code: 0 / message: "success"`` and ``code: 500 / message:
"failed"``. errorMessage is always present in ``data`` (``null`` on
success, populated on failure).

Once an algorithm task finishes, its 3D Tiles + ``instances.json`` are
uploaded to OSS (S3-compatible, configured in ``oss_config.json``).
The poll response's ``3dtilesUrl`` / ``instanceJsonUrl`` point at the
**cloud URLs** the backend can ``GET`` directly. The service no longer
serves 3D Tiles or instances.json.

Inputs are absolute filesystem paths (the algorithm reads directly from
the same mount the backend uses); no URL fetching, no /tmp cache. The
optional ``xmlFile`` is a base64-encoded string in the JSON body,
decoded and archived under ``<out_dir>/input.xml`` (the algorithm does
not currently consume it).

Run::

    python -m uvicorn scripts.service.api_server:app --host 0.0.0.0 --port 8901 --workers 1
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import unquote

# Allow `python scripts/service/api_server.py` as well as
# `uvicorn scripts.service.api_server:app` to find the sibling
# `task_manager.py` module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# algo/ subdir sits next to service/; we add it so we can import the
# memory estimator + the b3dm counter used by the submit-time pre-flight.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "algorithm"))

from fastapi import FastAPI                                                                # noqa: E402
from pydantic import BaseModel, Field                                                     # noqa: E402

from oss_uploader import OssUploader, load_config                                         # noqa: E402
from task_manager import BusyError, TaskStore                                             # noqa: E402
try:
    # Pre-flight estimator — see Priority 4.  Imports are wrapped so a
    # missing scipy (developer box without the prod conda env) does
    # not 500 the entire service; the pre-flight simply degrades to
    # "no early rejection" and the subprocess still runs (and still
    # has its own in-pipeline check in run_pipeline.stage_convert).
    from convert_point_ecef_and_3dtiles import (                                          # noqa: E402
        _estimate_peak_gib,
        _read_cgroup_memory_max_gib,
    )
    from point_cloud_extraction import (                                                    # noqa: E402
        find_leaf_b3dms_with_bbox,
        b3dm_position_count,
    )
    from algo_config import DBSCAN_VOXEL_M as _ALGO_DBSCAN_VOXEL_M                         # noqa: E402
    _PREFLIGHT_OK = True
except Exception as _preflight_exc:  # pragma: no cover
    _PREFLIGHT_OK = False


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("api_server")


# Opt-out: set ALGO_DISABLE_OOM_PREFLIGHT=1 to skip the submit-time
# memory early-reject entirely. Use when the b3dm scan itself is slow
# (e.g., 30s+ NFS walks of city-scale tilesets) and you'd rather get a
# clean RuntimeError from stage_convert than wait on every submit for a
# warning that degrades to fail-open anyway. Read here (after
# ``logging.basicConfig``) so the warning lands in the same handler
# config as the rest of api_server logs.
if os.getenv("ALGO_DISABLE_OOM_PREFLIGHT", "").lower() in ("1", "true", "yes"):
    log.warning("ALGO_DISABLE_OOM_PREFLIGHT set — submit-time OOM "
                "early-reject disabled (stage_convert still checks "
                "cgroup limit in-pipeline)")
    _PREFLIGHT_OK = False


SCRIPTS_DIR = Path(__file__).resolve().parent

# Default output dir is intentionally NOT a user-specific home path; override
# via OUTPUT_BASE_DIR. If neither is set, fall back to a generic Linux path
# (and log a warning so it's obvious in dev).
_DEFAULT_OUTPUT_BASE = "/var/lib/illegal-inspection/output"
_env_output_base = os.getenv("OUTPUT_BASE_DIR")
if _env_output_base:
    OUTPUT_BASE = Path(_env_output_base)
else:
    OUTPUT_BASE = Path(_DEFAULT_OUTPUT_BASE)
    log.warning(
        "OUTPUT_BASE_DIR not set; falling back to %s. "
        "Set OUTPUT_BASE_DIR to silence this and pin your deploy path.",
        _DEFAULT_OUTPUT_BASE,
    )


app = FastAPI(
    title="Illegal-Construction Inspection Algorithm Service",
    version="0.5.0",
)


# OSS uploader is built once at import time. If the config is missing
# or malformed we fail fast — better than a confusing 500 mid-task.
# The same config file also carries the (optional) terminal-state
# callback URL + retry knobs (see BACKEND_API §4.6).
_cfg = load_config()
uploader = OssUploader(_cfg)

# ----- 2026-07 多任务并行 -----
# max_concurrent_tasks 同时支持 oss_config.json 键 与 MAX_CONCURRENT_TASKS
# 环境变量(env 优先级更高,方便容器化部署时调整)。默认 4。
_max_concurrent_cfg = _cfg.get("max_concurrent_tasks", 4)
_max_concurrent_env = os.getenv("MAX_CONCURRENT_TASKS")
try:
    _max_concurrent = int(_max_concurrent_env or _max_concurrent_cfg)
except (TypeError, ValueError):
    log.warning("invalid max_concurrent_tasks setting (cfg=%r, env=%r); "
                "falling back to 4", _max_concurrent_cfg, _max_concurrent_env)
    _max_concurrent = 4
_max_concurrent = max(1, _max_concurrent)
log.info("max concurrent tasks = %d (cfg=%s, env=%s)",
         _max_concurrent, _max_concurrent_cfg, _max_concurrent_env)

# Single store per process — the HTTP layer must run with --workers 1 so
# this in-memory state is visible to every request.
store = TaskStore(
    output_base=OUTPUT_BASE, scripts_dir=SCRIPTS_DIR, uploader=uploader,
    callback_url=_cfg.get("backend_callback_url"),
    callback_timeout=_cfg.get("callback_timeout_seconds", 10),
    callback_max_retries=_cfg.get("callback_max_retries", 3),
    max_concurrent=_max_concurrent,
)


# --------- request model ---------

class SubmitRequest(BaseModel):
    taskId:           str = Field(..., min_length=1, max_length=128,
                                  pattern=r"^[A-Za-z0-9_\-\.]+$",
                                  description="Backend-supplied task identifier.")
    baseModelPath:    str = Field(..., min_length=1,
                                  description="Absolute filesystem path to "
                                              "the reference (epoch A) 3D Tiles root.")
    compareModelPath: str = Field(..., min_length=1,
                                  description="Absolute filesystem path to "
                                              "the comparison (epoch B) 3D Tiles root.")
    xmlFile:          Optional[str] = Field(None,
                                            description="Optional XML file content, "
                                                        "base64-encoded.")
    # ---- 2026-07 新增 (与 xmlFile 同一风格: Optional,无额外验证) ----
    positionMode:     Optional[str] = Field(None,
                                            description="Coordinate reference system "
                                                        "(e.g. 'WGS-84'). Informational "
                                                        "only; archived to "
                                                        "<out_dir>/request.json.")
    areaCoordinates:  Optional[List[dict]] = Field(None,
                                            description="Optional list of "
                                                        "{latitude, longitude, altitude} "
                                                        "dicts defining an ROI polygon "
                                                        "(≥3 vertices). Consumed by the "
                                                        "algorithm: only points inside "
                                                        "the ROI participate in change "
                                                        "detection. Archived to "
                                                        "<out_dir>/request.json.")
    radius:           Optional[float] = Field(None,
                                            description="Optional radius (meters). "
                                                        "Reserved for future use; "
                                                        "currently ignored by the "
                                                        "algorithm but archived to "
                                                        "<out_dir>/request.json.")
    # ----- 2026-08 新增: 多场景检测类型 -----
    # 三种场景共用同一套三维差分对比管线 (Stage 1-3),只在 Stage 4 后处理
    # 路径上分叉:
    #   - "twoIllegal"           : 现有逻辑 (HAG 过滤 + Gaussian 置信度排序)
    #   - "constructionProgress" : 建筑工地施工进度监控 (legacy num_points 排序)
    #   - "landSlide"            : 滑坡预警 (legacy num_points 排序)
    # 缺失/None 一律按 "twoIllegal" 处理 (向后兼容,老客户端无需立即升级)。
    # Pydantic Literal[...] 在收到未知值时返 HTTP 422。
    detectionType:    Optional[Literal["twoIllegal", "constructionProgress", "landSlide"]] \
                      = Field(None,
                              description="Three-scenario enum. None / missing => "
                                          "'twoIllegal' (backward-compat). "
                                          "'constructionProgress' and 'landSlide' "
                                          "switch Stage 4 post-processing to the "
                                          "legacy num_points-desc sort.")


# --------- model path resolution ---------
# Backend sends "virtual" paths that look like either:
#   - a plain absolute filesystem path (legacy contract), or
#   - an OSS-style key ending in "/tileset.json", possibly URL-encoded,
#     whose decoded form is the literal name of a folder under MODEL_ROOT.
# We translate the second form to "<MODEL_ROOT>/<decoded>/tileset.json"
# before any downstream layer touches the filesystem.

_MODEL_ROOT = Path(os.getenv("MODEL_ROOT", "/model")).resolve()


def resolve_model_path(raw: str) -> Path:
    """Translate a backend-supplied model path to a local filesystem Path.

    Rules (applied in order):
      1. Single URL-decode (``urllib.parse.unquote``). A value with no '%'
         passes through unchanged.
      2. Strip a trailing ``/tileset.json`` (if present) to get the folder token.
      3. If ``<MODEL_ROOT>/<token>/tileset.json`` exists, return that path.
      4. Else, fall back to treating ``raw`` as a literal absolute filesystem
         path (legacy contract); return it if it has a ``tileset.json`` underneath.
      5. Else, return the candidate from step 3 anyway — the subprocess probe
         in ``run_pipeline_subprocess.py`` will then report a clean
         "no tileset.json at <path>" error.
    """
    if not raw:
        return (_MODEL_ROOT / "").resolve()

    decoded = unquote(raw)
    if decoded.endswith("/tileset.json"):
        decoded = decoded[: -len("/tileset.json")]
    candidate_model = (_MODEL_ROOT / decoded).resolve()

    if (candidate_model / "tileset.json").is_file():
        return candidate_model

    # Backward compatibility: plain absolute paths (e.g. smoke-001).
    raw_path = Path(raw)
    if (raw_path / "tileset.json").is_file():
        return raw_path.resolve()

    return candidate_model


# --------- response builders ---------

# 兜底:任何 errorMessage 超过 500 字符就截断,防止后端 DB 列写爆。
# 正常错误已经在 run_pipeline_subprocess.py 源头写成短摘要(最后一行异常),
# 这层是为了防御未来别的代码路径意外写出长字符串。
_MAX_ERROR_LEN = 500


def _truncate_error(s: Optional[str]) -> Optional[str]:
    """Cap errorMessage at _MAX_ERROR_LEN chars; return None unchanged.

    Strategy when truncating: keep the first 400 chars + an explicit
    truncation marker + the last 80 chars, so the most actionable parts
    (exception type + key path at the head; the final exception message
    at the tail) both survive."""
    if s is None:
        return None
    if len(s) <= _MAX_ERROR_LEN:
        return s
    return f"{s[:400]}...<truncated {len(s) - 480} chars>...{s[-80:]}"


def _submit_ok(task_id: str) -> dict:
    return {"code": 0, "message": "success",
            "data": {"taskId": task_id, "status": "PENDING", "errorMessage": None}}


def _submit_fail(task_id: str, reason: str) -> dict:
    return {"code": 500, "message": "failed",
            "data": {"taskId": task_id, "status": "FAILED", "errorMessage": _truncate_error(reason)}}


def _poll_ok(status) -> dict:
    # OSS URL is set in the task_manager's reader thread right after the
    # upload finishes. Until then, the chunk URL is missing — for a
    # RUNNING task that's normal, for a SUCCESS task it should never
    # be None. We preserve the order of three_dtiles_paths so callers
    # can match chunks by index.
    tiles_urls: list[str] = []
    for p in status.three_dtiles_paths:
        url = status.oss_chunk_urls.get(p)
        if url is not None:
            tiles_urls.append(url)
    return {"code": 0, "message": "success", "data": {
        "taskId":          status.task_id,
        "progress":        str(status.progress),
        "status":          status.state,
        "step":            status.step,
        "3dtilesUrl":      tiles_urls or None,
        "instanceJsonUrl": status.oss_instance_url,
        "detectionType":   status.detection_type,
        "errorMessage":    None,
    }}


def _poll_fail(task_id: str, reason: str, status=None) -> dict:
    # FAILED 时若 _scan_partial_outputs 已经成功上了一些 chunk，
    # 3dtilesUrl / instanceJsonUrl 也回填给前端（与 callback 行为一致；
    # 用于渐进显示——前端先看已上传的 chunk、再看 errorMessage）。
    if status is not None:
        progress, step = str(status.progress), status.step
        tiles_urls = [status.oss_chunk_urls[p]
                      for p in status.three_dtiles_paths
                      if p in status.oss_chunk_urls]
        instance_url = status.oss_instance_url
    else:
        progress, step = "0", "waiting"
        tiles_urls = []
        instance_url = None
    return {"code": 500, "message": "failed", "data": {
        "taskId":          task_id,
        "progress":        progress,
        "status":          "FAILED",
        "step":            step,
        "3dtilesUrl":      tiles_urls or None,
        "instanceJsonUrl": instance_url,
        "detectionType":   (status.detection_type if status is not None else "twoIllegal"),
        "errorMessage":    _truncate_error(reason),
    }}


# --------- helpers ---------

def _archive_xml(out_dir: Path, xml_b64: str) -> None:
    """Decode base64 XML and persist to <out_dir>/input.xml.

    Best-effort: any failure is logged but does not raise — the algorithm
    does not currently consume this file."""
    try:
        raw = base64.b64decode(xml_b64, validate=True)
    except Exception as e:
        log.warning("xmlFile base64 decode failed: %s", e)
        return
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "input.xml").write_bytes(raw)
    except OSError as e:
        log.warning("failed to write input.xml: %s", e)


def _archive_metadata(out_dir: Path, task_id: str,
                      position_mode: Optional[str],
                      area_coordinates: Optional[List[dict]],
                      radius: Optional[float],
                      xml_path: Optional[str],
                      base_model_path: Optional[str] = None,
                      compare_model_path: Optional[str] = None,
                      base_model_path_resolved: Optional[str] = None,
                      compare_model_path_resolved: Optional[str] = None,
                      detection_type: Optional[str] = "twoIllegal",
                      submitted_at: Optional[str] = None) -> None:
    """Snapshot the full request payload to <out_dir>/request.json.

    Records both the original virtual paths (as submitted by the backend)
    and the resolved local filesystem paths (after ``resolve_model_path``),
    plus the optional ROI / XML / detectionType fields. Best-effort: write
    failure is logged at WARNING but does not raise — a missing metadata
    file must not block task submission."""
    payload = {
        "taskId":                  task_id,
        "submittedAt":             submitted_at,
        # 原始虚拟路径（后端 SubmitRequest 原样）
        "baseModelPath":           base_model_path,
        "compareModelPath":        compare_model_path,
        # 解析到本地的 /model/<dir> 路径；解析失败时为 None
        "baseModelPathResolved":   base_model_path_resolved,
        "compareModelPathResolved": compare_model_path_resolved,
        # ROI 参数
        "positionMode":            position_mode,
        "areaCoordinates":         area_coordinates,
        "radius":                  radius,
        # 三场景检测类型 (None ⇒ "twoIllegal" 默认;在 submit() 里统一过 or "twoIllegal")
        "detectionType":           detection_type,
        # 仅在 xmlFile 提供时有值;否则 None
        "xmlPath":                 xml_path,
    }
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "request.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("could not write request metadata %s: %s",
                    out_dir / "request.json", e)
# --------- pre-flight (timeout-bounded) ---------

# Pre-flight scans the leaf b3dm files of the base model to estimate
# B's point count; the algorithm rejects submissions where the
# predicted Stage 4 peak would blow past 80% of the cgroup memory cap.
#
# Filesystem hazard: the underlying ``open()`` / ``read()`` on a b3dm
# can block in kernel space if the mount is slow or the NFS/RPC
# server stops responding. A Python-level ``try/except timeout`` is
# useless here — the thread is uninterruptibly in state ``D`` until
# the kernel gets the RPC reply.
#
# Fix: run the pre-flight in a daemon thread and ``thread.join(timeout)``.
# On timeout we log a WARNING and *fail-open* — skip the OOM early-reject
# and let the task reach ``store.submit()``. The subprocess itself
# re-checks the memory estimate at Stage 4 entry (see
# ``run_pipeline.stage_convert`` / ``_estimate_peak_gib``), so even if
# we let a too-big task through, it still surfaces a clean
# ``RuntimeError("OOM: ...")` ` errorMessage instead of hanging the API.
#
# Override the budget via env: ``PREFLIGHT_TIMEOUT_S=60 uvicorn ...``.
_DEFAULT_PREFLIGHT_TIMEOUT_S = 30.0


def _count_b3dm_vertices(base_path: Path) -> int:
    """Header-only b3dm scan that returns total vertex count.

    Raises whatever ``find_leaf_b3dms_with_bbox`` /
    ``b3dm_position_count`` raises; the caller wraps in try/except.
    """
    leaves = find_leaf_b3dms_with_bbox(base_path)
    n_b = 0
    for leaf_path, _ in leaves:
        n_b += b3dm_position_count(leaf_path)
    return n_b


def _preflight_with_timeout(base_path: Path, timeout_s: float
                            ) -> dict:
    """Run the b3dm vertex scan with a wall-clock budget.

    Returns one of:
      ``{"status": "ok",       "n_b": int}``
      ``{"status": "timeout"}``
      ``{"status": "error",    "exc": BaseException}``

    The thread is daemon=True: if it is still running when timeout fires
    (e.g., because the kernel I/O is in state D), it won't block process
    exit, and won't block subsequent submissions — it's a bounded leak
    on shutdown that goes away when uvicorn restarts. In practice the
    leaked thread is harmless (no shared state, no FDs).
    """
    out: dict = {"status": "ok", "n_b": 0}
    def worker() -> None:
        try:
            out["n_b"] = _count_b3dm_vertices(base_path)
        except BaseException as exc:  # noqa: BLE001 — best-effort scan
            out["status"] = "error"
            out["exc"] = exc

    t = threading.Thread(target=worker, name=f"preflight-{base_path.name}",
                         daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        out["status"] = "timeout"
    return out


# --------- routes ---------

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/two-violation/compare")
def submit(req: SubmitRequest) -> dict:
    """Accept a task. Inputs are filesystem paths (not URLs).

    The service does no path-validation or XML-size checks at submit
    time. Bad paths surface as FAILED + errorMessage on the poll
    endpoint after the algorithm subprocess reads them.
    """
    out_dir = OUTPUT_BASE / req.taskId
    out_dir.mkdir(parents=True, exist_ok=True)

    if req.xmlFile:
        _archive_xml(out_dir, req.xmlFile)

    # Map backend "virtual" paths to local /model/<dir> filesystem paths.
    # Plain absolute paths still work (backward compatibility).
    try:
        base_path    = resolve_model_path(req.baseModelPath)
        compare_path = resolve_model_path(req.compareModelPath)
        log.info(
            "[%s] resolved paths: base=%s compare=%s",
            req.taskId, base_path, compare_path,
        )
    except Exception as e:
        log.exception("path resolve failed")
        return _submit_fail(req.taskId, f"path resolve failed: {e}")

    # 快照请求的全部内容到 <out_dir>/request.json，包含原始路径与解析后路径。
    # 算法本体可能消费 ROI 字段；其他字段为后续 hook 预留。和 _archive_xml
    # 一样 best-effort，失败不抛。
    # detectionType: Pydantic Literal 已校验枚举;None 兜底为 "twoIllegal"。
    _detection_type = req.detectionType or "twoIllegal"
    _archive_metadata(
        out_dir, req.taskId,
        req.positionMode, req.areaCoordinates, req.radius,
        xml_path=str(out_dir / "input.xml") if req.xmlFile else None,
        base_model_path=req.baseModelPath,
        compare_model_path=req.compareModelPath,
        base_model_path_resolved=str(base_path),
        compare_model_path_resolved=str(compare_path),
        detection_type=_detection_type,
        submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # ---- Pre-flight: refuse early if the b3dm head-count + diff-ratio
    # estimate predicts an OOM. We do a cheap header-only scan of B's
    # leaf b3dms (no full b3dm parse) to count vertices, then estimate
    # Stage 4 peak with the same empirical model the subprocess uses.
    # The estimate is conservative (upper bound); a refusal means
    # "almost certainly OOMs given the current cgroup limit". A pass
    # does NOT guarantee success (other stages can still spike), but
    # it means the silent rc=-9 SIGKILL path becomes a clean 500 with
    # a readable errorMessage.
    #
    # Wall-clock budget: ``PREFLIGHT_TIMEOUT_S`` (default 30s) bounds
    # the scan via ``_preflight_with_timeout``. If the b3dm scan blocks
    # on the underlying filesystem (e.g., NFS/RPC wedged — kernel-side
    # state D, where Python signals cannot interrupt), we degrade
    # **fail-open** (skip the early-reject and let the task reach
    # ``store.submit()``). The subprocess re-checks the same memory
    # estimate at Stage 4 entry, so even on a too-big submission the
    # outcome is a clean ``RuntimeError("OOM: ...")`` errorMessage —
    # **never** a hung API request that strands the backend caller.
    try:
        _preflight_timeout_s = float(
            os.getenv("PREFLIGHT_TIMEOUT_S", _DEFAULT_PREFLIGHT_TIMEOUT_S)
        )
    except (TypeError, ValueError):
        _preflight_timeout_s = _DEFAULT_PREFLIGHT_TIMEOUT_S
    if _PREFLIGHT_OK and _preflight_timeout_s > 0:
        pf = _preflight_with_timeout(base_path, _preflight_timeout_s)
        if pf["status"] == "timeout":
            log.warning(
                "[%s] pre-flight timed out after %.1fs; "
                "proceeding WITHOUT OOM early-reject "
                "(in-pipeline check still runs in stage_convert)",
                req.taskId, _preflight_timeout_s,
            )
        elif pf["status"] == "error":
            # Best-effort: a malformed tileset must NOT 500 the submission.
            log.warning("[%s] pre-flight failed (proceeding): %s",
                        req.taskId, pf["exc"])
        else:
            n_b = pf["n_b"]
            # 5% empirical diff ratio on urban SfM (B - A; the B tileset
            # has both new buildings + dropped trees after Stage 2 mask).
            n_diff_est = max(1, int(n_b * 0.05))
            predicted = _estimate_peak_gib(
                n_diff_est, n_clusters=0, dbscan_voxel_m=_ALGO_DBSCAN_VOXEL_M,
            )
            cap_gib = _read_cgroup_memory_max_gib()
            log.info(
                "[%s] pre-flight: n_b=%d, est n_diff=%d, predicted peak=%.1f GiB, "
                "cgroup cap=%s GiB",
                req.taskId, n_b, n_diff_est, predicted,
                f"{cap_gib:.1f}" if cap_gib is not None else "unknown",
            )
            if cap_gib is not None and predicted > 0.8 * cap_gib:
                return _submit_fail(
                    req.taskId,
                    f"OOM: predicted peak {predicted:.1f} GiB > 80% of "
                    f"cgroup limit {cap_gib:.1f} GiB (B has {n_b:,} points; "
                    f"~5% change-ratio). Set ALGO_DBSCAN_VOXEL_M=0 to "
                    f"disable decimation, or run on a host with more memory.",
                )

    try:
        store.submit(
            task_id=req.taskId,
            base_model_path=str(base_path),
            compare_model_path=str(compare_path),
            xml_path=str(out_dir / "input.xml") if req.xmlFile else None,
            position_mode=req.positionMode,
            area_coordinates=req.areaCoordinates,
            radius=req.radius,
            detection_type=_detection_type,
        )
    except BusyError as e:
        return _submit_fail(req.taskId, str(e))
    except Exception as e:
        log.exception("submit failed")
        return _submit_fail(req.taskId, f"submit failed: {e}")

    return _submit_ok(req.taskId)


@app.get("/two-violation/tasks/{taskId}")
def get_task(taskId: str):
    status = store.get(taskId)
    if status is None:
        return _poll_fail(taskId, f"taskId not found: {taskId}")
    if status.state == "FAILED":
        reason = status.error_message or "task failed"
        return _poll_fail(taskId, reason, status)
    return _poll_ok(status)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8901"))
    uvicorn.run(app, host="0.0.0.0", port=port)
