#!/usr/bin/env python3
"""Subprocess wrapper around :mod:`run_pipeline`.

Inputs are absolute filesystem paths (``--base-path``, ``--compare-path``,
``--xml-path``). The HTTP service (``service/api_server.py``) is expected
to have validated those paths before spawning us; we sanity-check
``tileset.json`` exists and otherwise just dispatch to
:func:`run_pipeline.main`.

Writes a sidecar ``<out_dir>/status.json`` mirroring the backend-facing
``{step, progress, status, errorMessage}`` dict.

CLI::

    python run_pipeline_subprocess.py \\
        --task-id <id> \\
        --out-dir <dir> \\
        --base-path <abs path> \\
        --compare-path <abs path> \\
        [--xml-path <abs path>] \\
        [--no-keep-intermediates]
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import traceback
from pathlib import Path

# run_pipeline lives in scripts/algorithm/; this script lives in
# scripts/service/. Spawned as a subprocess from the FastAPI service —
# run_pipeline is imported into this process directly via
# scripts/algorithm/ on sys.path below.

# Allow ``python scripts/service/run_pipeline_subprocess.py`` as well as
# ``uvicorn scripts.service.api_server:app`` to find run_pipeline in the
# sibling algorithm/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "algorithm"))
sys.path.insert(0, str(Path(__file__).resolve().parent))    # for stage_meta

import run_pipeline      # noqa: E402
from stage_meta import STAGE_FRIENDLY, STAGE_PROGRESS  # noqa: E402


log = logging.getLogger(__name__)


class StatusWriter:
    """Atomically writes ``<out_dir>/status.json`` on every update."""

    def __init__(self, out_dir: Path):
        self.path = out_dir / "status.json"

    def write(self, *, step: str, progress: int, status: str,
              error: str | None = None) -> None:
        payload: dict = {
            "step":     step,
            "progress": str(progress),
            "status":   status,
        }
        if error is not None:
            payload["error"] = error
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.replace(self.path)


def _install_stage_hook(writer: StatusWriter) -> None:
    """Wrap :func:`run_pipeline._run_stage` so each stage transition writes
    ``status.json`` with the friendly step + progress endpoint."""
    original = run_pipeline._run_stage

    def hooked(label: str, fn, *args, **kwargs):
        parts = label.split(maxsplit=1)
        stage_name = parts[1] if len(parts) > 1 else label
        friendly = STAGE_FRIENDLY.get(stage_name, stage_name)
        start_p, end_p = STAGE_PROGRESS.get(stage_name, (0, 100))

        writer.write(step=friendly, progress=start_p, status="OK")
        result, elapsed = original(label, fn, *args, **kwargs)
        writer.write(step=friendly, progress=end_p, status="OK")
        return result, elapsed

    run_pipeline._run_stage = hooked


def _parse_sys_exit(e: SystemExit) -> tuple[int, str]:
    code = e.code
    if isinstance(code, int):
        return code, ""
    if code is None:
        return 0, ""
    return 1, str(code)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--task-id", required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--base-path", required=True,
                   help="Absolute filesystem path to the base (epoch A) "
                        "3D Tiles root directory.")
    p.add_argument("--compare-path", required=True,
                   help="Absolute filesystem path to the comparison (epoch B) "
                        "3D Tiles root directory.")
    p.add_argument("--xml-path", default=None,
                   help="Optional absolute path of the input XML file. "
                        "The algorithm does not currently consume it; we "
                        "snapshot a copy under <out_dir>/input.xml for "
                        "auditability.")
    # ---- 2026-07 新增: 与 api_server.py 透传的 3 个可选元数据 ----
    # areaCoordinates 现在被 run_pipeline 实际消费（ROI 过滤）；
    # radius 仍是预留。HTTP 层依旧把三者都存档到 <out_dir>/request.json。
    p.add_argument("--position-mode", default=None,
                   help="Coordinate reference system (e.g. 'WGS-84'). "
                        "Informational only.")
    p.add_argument("--area-coordinates", default=None,
                   help="JSON-encoded list of {latitude, longitude, "
                        "altitude} dicts. When supplied (≥3 vertices), "
                        "the algorithm only inspects points inside this "
                        "ROI polygon.")
    p.add_argument("--radius", type=float, default=None,
                   help="Optional radius (meters). Reserved for future "
                        "use; currently logged and ignored by the "
                        "algorithm.")
    p.add_argument("--no-keep-intermediates", dest="keep_intermediates",
                   action="store_false", default=True)
    return p.parse_args(argv)


def main() -> int:
    args = _parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    writer = StatusWriter(out_dir)

    base_path    = Path(args.base_path)
    compare_path = Path(args.compare_path)
    if not (base_path / "tileset.json").is_file():
        msg = f"base path has no tileset.json: {base_path}"
        writer.write(step="failed", progress=0, status="FAILED", error=msg)
        return 1
    if not (compare_path / "tileset.json").is_file():
        msg = f"compare path has no tileset.json: {compare_path}"
        writer.write(step="failed", progress=0, status="FAILED", error=msg)
        return 1

    # Snapshot XML (algorithm currently does not consume it; future hook).
    if args.xml_path:
        try:
            shutil.copy2(args.xml_path, out_dir / "input.xml")
        except OSError as e:
            log.warning(
                "could not archive xml %s -> %s: %s",
                args.xml_path, out_dir / "input.xml", e)

    # ---- run algorithm (uses local paths directly) ----
    _install_stage_hook(writer)

    argv = [
        str(base_path), str(compare_path),
        "-o", str(out_dir),
    ]
    if not args.keep_intermediates:
        argv.append("--no-keep-intermediates")
    # ---- 2026-07 新增: 把 ROI 元数据透传给 run_pipeline.main(argv) ----
    # task_manager._spawn 已经把 positionMode / areaCoordinates / radius
    # 拼成 JSON 字符串塞到 argv 了；这里只是把它们原样追加下去，让
    # run_pipeline.parse_args 能拿到。
    if args.position_mode is not None:
        argv += ["--position-mode", args.position_mode]
    if args.area_coordinates is not None:
        argv += ["--area-coordinates", args.area_coordinates]
    if args.radius is not None:
        argv += ["--radius", str(args.radius)]

    rc, err_msg = 0, ""
    try:
        rc = run_pipeline.main(argv)
    except SystemExit as e:
        rc, err_msg = _parse_sys_exit(e)
    except KeyboardInterrupt:
        writer.write(step="interrupted", progress=0, status="FAILED",
                     error="interrupted by signal")
        raise
    except Exception as e:
        tb = traceback.format_exc()
        # Short summary for sidecar / HTTP response / callback (DB-friendly).
        # 完整 traceback 落 <out_dir>/error.log 给运维查问题。
        short = f"{type(e).__name__}: {e}".strip() or "unknown error"
        writer.write(step="failed", progress=0, status="FAILED", error=short)
        try:
            (out_dir / "error.log").write_text(tb, encoding="utf-8")
        except OSError as log_exc:
            log.warning("could not write error.log: %s", log_exc)
        return 1

    if rc == 0:
        writer.write(step="completed", progress=100, status="SUCCESS")
        return 0

    writer.write(step="failed", progress=0, status="FAILED",
                 error=err_msg or f"subprocess exit code {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
