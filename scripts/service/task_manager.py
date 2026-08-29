"""In-memory task store + subprocess lifecycle for the algorithm service.

The HTTP layer (:mod:`api_server`) calls into a single :class:`TaskStore`
singleton. The store:

* holds an in-memory dict of :class:`TaskStatus` keyed by ``task_id``
* enforces a single-task concurrency limit (``threading.Lock``)
* spawns :mod:`run_pipeline_subprocess` as a child process
* runs a daemon thread per task that parses the child's stdout for
  ``[N/4 <stage>] starting…`` / ``done in …`` lines and updates the
  in-memory status
* after the subprocess exits 0, **synchronously uploads** the 3D Tiles
  chunks and ``instances.json`` to OSS via the injected
  :class:`OssUploader`; ``status.state`` stays ``RUNNING`` until every
  upload completes, then flips to ``SUCCESS``
* also exposes :meth:`append_3dtiles_chunk` (v2 hook) — for a chunked
  algorithm, calling this for each chunk makes that chunk's URL
  visible to the backend **before** the task is done, so the frontend
  can render progressively

URLs the backend sees in poll responses come from OSS, not the
algorithm service — the service no longer serves 3D Tiles / instances.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from oss_uploader import OssUploader

from stage_meta import STAGE_FRIENDLY, STAGE_PROGRESS  # noqa: E402


log = logging.getLogger("task_manager")


class BusyError(RuntimeError):
    """Raised by :meth:`TaskStore.submit` when another task is already running
    or the same ``task_id`` is already known."""


@dataclass
class TaskStatus:
    task_id: str
    state: str = "PENDING"           # PENDING / RUNNING / SUCCESS / FAILED
    step: str = "waiting"
    progress: int = 0
    error_message: Optional[str] = None
    output_dir: Optional[str] = None
    base_model_path: str = ""
    compare_model_path: str = ""
    xml_path: Optional[str] = None
    # ----- 2026-07 新增可选元数据 (与 xmlFile 同步在 request.json 存档) -----
    position_mode: Optional[str] = None
    area_coordinates: Optional[list] = None
    radius: Optional[float] = None
    # ----- 2026-08 新增: 三场景检测类型 -----
    # 默认 "twoIllegal" 兼容老任务(无 detection_type 字段的历史 request.json);
    # task_manager._spawn 会把这个值塞到子进程的 --detection-type argv;
    # run_pipeline.py 据此覆盖 _CONFIG.VIOLATION_MODE。
    detection_type: str = "twoIllegal"
    # Local path(s) of 3DTiles chunks. v1 algorithm produces one chunk
    # at <output_dir>/3DTiles; the list shape is the v2 hook for
    # chunked / progressive 3DTiles return.
    three_dtiles_paths: list[str] = field(default_factory=list)
    instances_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    # OSS upload results, populated by the reader thread:
    #   oss_chunk_urls[local_path] = public URL of that chunk's tileset.json
    #   oss_instance_url          = public URL of instances.json
    # Both stay None / missing while the upload is in flight; the API
    # layer returns them as None in the response until they are set.
    oss_chunk_urls: dict[str, str] = field(default_factory=dict)
    oss_instance_url: Optional[str] = None


class TaskStore:
    """Singleton-style in-memory store. One process owns one instance."""

    def __init__(self, output_base: Path, scripts_dir: Path,
                 uploader: "OssUploader", *,
                 callback_url: Optional[str] = None,
                 callback_timeout: int = 10,
                 callback_max_retries: int = 3,
                 max_concurrent: int = 4):
        self.output_base = output_base
        self.output_base.mkdir(parents=True, exist_ok=True)
        self.scripts_dir = scripts_dir
        self.uploader = uploader
        # Terminal-state callback to the backend. Empty URL = disabled
        # (escape hatch: drop the field from oss_config.json to silence).
        # Callback is best-effort: a failure here never blocks the
        # polling endpoint — the backend can always recover via
        # GET /two-violation/tasks/{taskId}.
        self.callback_url         = (callback_url or "").strip()
        self.callback_timeout     = max(1, int(callback_timeout))
        self.callback_max_retries = max(1, int(callback_max_retries))
        self._status: dict[str, TaskStatus] = {}
        self._lock = threading.Lock()
        # ----- 2026-07 多任务并行 -----
        # 单飞: _running_task_id + _running_proc
        # N-并行: set of in-flight task_id + dict of procs + FIFO 队列 + dispatcher
        # N=1 时退化为单飞路径 (在 submit() 里 fast-path)
        self._max_concurrent: int = max(1, int(max_concurrent))
        self._running_ids: set[str] = set()
        self._running_procs: dict[str, subprocess.Popen] = {}
        self._pending_queue: "queue.Queue[str]" = queue.Queue()
        self._dispatcher = threading.Thread(
            target=self._dispatcher_loop, daemon=True, name="task-dispatcher",
        )
        self._dispatcher.start()

    # ----- public API -----

    def submit(self, *, task_id: str, base_model_path: str,
               compare_model_path: str, xml_path: Optional[str],
               position_mode: Optional[str] = None,
               area_coordinates: Optional[list] = None,
               radius: Optional[float] = None,
               detection_type: str = "twoIllegal") -> TaskStatus:
        """Accept a new task. Raises :class:`BusyError` for concurrency /
        duplicate-id conflicts. Path existence is NOT validated here —
        ``run_pipeline_subprocess`` will mark the task FAILED with a clear
        ``errorMessage`` if a path is bad. (The HTTP layer does an early
        422 check too.)

        The three optional metadata fields (``position_mode``,
        ``area_coordinates``, ``radius``) are passed through to the
        subprocess via argv. ``area_coordinates`` is consumed by the
        ROI feature (Stage 1 / Stage 2 mask); ``position_mode`` and
        ``radius`` are informational / reserved respectively. All three
        are also archived to ``<out_dir>/request.json`` by the HTTP
        layer.

        ``detection_type`` (2026-08 新增,三场景检测类型)同样透传给子进程;
        algorithm 端据此覆盖 _CONFIG.VIOLATION_MODE。HTTP 层已经把缺失值
        兜底为 "twoIllegal",这里再 normalize 一次兜底防直接调 store.submit
        的代码路径。"""
        # 防御性兜底: 缺失值归一为 "twoIllegal",保证历史调用点 / dispatcher
        # 重启老任务时 TaskStatus.detection_type 一定有合法值。
        if not detection_type:
            detection_type = "twoIllegal"

        with self._lock:
            if task_id in self._status:
                raise BusyError(f"task_id already exists: {task_id}")

            out_dir = self.output_base / task_id
            out_dir.mkdir(parents=True, exist_ok=True)

            status = TaskStatus(
                task_id=task_id,
                state="PENDING",
                step="waiting",
                progress=0,
                output_dir=str(out_dir),
                base_model_path=base_model_path,
                compare_model_path=compare_model_path,
                xml_path=xml_path,
                position_mode=position_mode,
                area_coordinates=area_coordinates,
                radius=radius,
                detection_type=detection_type,
            )
            self._status[task_id] = status

            # Fast-path: N=1 走原同步 spawn 路径 (与旧版行为完全一致,
            # 保留向后兼容)。N>1 时,有 slot 就直接 spawn,满了就入队。
            if self._max_concurrent == 1 or len(self._running_ids) < self._max_concurrent:
                try:
                    proc = self._spawn(
                        task_id, base_model_path, compare_model_path,
                        xml_path, position_mode, area_coordinates, radius,
                        detection_type, out_dir,
                    )
                except Exception as e:
                    status.state = "FAILED"
                    status.error_message = f"failed to start subprocess: {e}"
                    status.finished_at = time.time()
                    log.exception("[%s] failed to spawn subprocess", task_id)
                    raise

                self._running_ids.add(task_id)
                self._running_procs[task_id] = proc
                status.state = "RUNNING"

                # Start reader thread OUTSIDE the lock so submit returns quickly.
                # 先把 start 放到锁内,避免与 dispatcher 抢同一 task_id。
                t = threading.Thread(
                    target=self._reader_loop,
                    args=(task_id, proc, out_dir),
                    daemon=True,
                    name=f"reader-{task_id}",
                )
                t.start()
            else:
                # 满了 → 入队 FIFO,等 dispatcher 拉起。状态保持 PENDING/step=waiting。
                self._pending_queue.put(task_id)
                log.info("[%s] queued (running=%d, max=%d)",
                         task_id, len(self._running_ids), self._max_concurrent)
        return status

    def get(self, task_id: str) -> Optional[TaskStatus]:
        return self._status.get(task_id)

    # ----- progressive-result hook (v2 algorithm support) -----

    def append_3dtiles_chunk(self, task_id: str, local_path: str) -> None:
        """Append a chunk's local 3DTiles path to the in-memory list and
        immediately upload it to OSS so the URL is visible to the
        backend **before** the algorithm finishes (progressive v2
        hook). v1 algorithm does not call this; the v1 single-chunk
        path is handled by :meth:`_finalize_success`.

        On upload failure the entire task is marked FAILED with an
        OSS-related ``errorMessage``."""
        status = self._status.get(task_id)
        if status is None:
            return
        if local_path not in status.three_dtiles_paths:
            status.three_dtiles_paths.append(local_path)
        if local_path in status.oss_chunk_urls:
            return  # already uploaded

        try:
            self._upload_chunk(status, Path(local_path))
        except Exception as e:
            status.state = "FAILED"
            status.error_message = f"OSS upload failed for chunk {local_path}: {e}"
            status.finished_at = time.time()
            log.exception("[%s] OSS chunk upload failed", task_id)

    # ----- internals -----

    def _release_slot(self, task_id: str) -> None:
        """Release one concurrency slot, no matter who owned it.

        Idempotent (set.discard / dict.pop are both no-ops on missing keys)
        so it's safe to call unconditionally from reader_loop. After
        releasing, the next queued task will be picked up by the
        dispatcher (which is itself woken implicitly by the slot
        appearing as free on its next pass)."""
        with self._lock:
            self._running_ids.discard(task_id)
            self._running_procs.pop(task_id, None)
        log.info("[%s] slot released; running=%d, queued=%d",
                 task_id, len(self._running_ids), self._pending_queue.qsize())

    def _dispatcher_loop(self) -> None:
        """Single daemon thread that drains ``_pending_queue`` FIFO.

        When a task in the queue has a free slot, this loop spawns it
        the same way :meth:`submit`'s fast-path does (in the daemon's
        thread, not on the submit caller's request thread).

        The loop is intentionally tolerant: tasks can be cancelled /
        forgotten before they ever reach the front of the queue; in
        that case ``task_id not in self._status`` and we silently drop
        the entry. If the queue is somehow longer than the configured
        concurrency (it shouldn't be — submit() only enqueues when
        full), we re-enqueue and sleep briefly to avoid a tight spin."""
        while True:
            task_id = self._pending_queue.get()  # blocking
            with self._lock:
                if task_id not in self._status:
                    log.info("[dispatcher] drop unknown task %s", task_id)
                    continue
                if task_id in self._running_ids:
                    # Already running (shouldn't happen — defense only)
                    log.warning("[dispatcher] drop already-running %s", task_id)
                    continue
                if len(self._running_ids) >= self._max_concurrent:
                    # 队列里还有更早的 task 没消费完 → 放回去再等
                    self._pending_queue.put(task_id)
                    time.sleep(0.1)
                    continue
                status = self._status[task_id]
                out_dir = Path(status.output_dir)
                try:
                    proc = self._spawn(
                        status.task_id, status.base_model_path,
                        status.compare_model_path, status.xml_path,
                        status.position_mode, status.area_coordinates,
                        status.radius, status.detection_type, out_dir,
                    )
                except Exception as e:
                    status.state = "FAILED"
                    status.error_message = f"failed to start subprocess: {e}"
                    status.finished_at = time.time()
                    log.exception("[%s] failed to spawn (dispatcher)",
                                  task_id)
                    # 不占 slot,直接下一个
                    continue
                self._running_ids.add(task_id)
                self._running_procs[task_id] = proc
                status.state = "RUNNING"
                log.info("[%s] dispatcher started; running=%d, queued=%d",
                         task_id, len(self._running_ids),
                         self._pending_queue.qsize())
                # 锁内起 reader (与 submit fast-path 一致;避免和并发 reader 抢)
                threading.Thread(
                    target=self._reader_loop,
                    args=(task_id, proc, out_dir),
                    daemon=True, name=f"reader-{task_id}",
                ).start()

    def _spawn(self, task_id: str, base_path: str, compare_path: str,
               xml_path: Optional[str],
               position_mode: Optional[str],
               area_coordinates: Optional[list],
               radius: Optional[float],
               detection_type: str,
               out_dir: Path) -> subprocess.Popen:
        wrapper = self.scripts_dir / "run_pipeline_subprocess.py"
        cmd = [
            sys.executable, "-u", str(wrapper),
            "--task-id",      task_id,
            "--out-dir",      str(out_dir),
            "--base-path",    base_path,
            "--compare-path", compare_path,
            "--no-keep-intermediates",
        ]
        if xml_path:
            cmd += ["--xml-path", xml_path]
        # ----- 2026-07 新增: 透传可选元数据 (与 api_server.py 顺序一致) -----
        if position_mode is not None:
            cmd += ["--position-mode", position_mode]
        if area_coordinates is not None:
            # 复杂结构以 JSON 字符串透传;子进程不消费,但为未来 hook 保留
            cmd += ["--area-coordinates",
                    json.dumps(area_coordinates, ensure_ascii=False)]
        if radius is not None:
            cmd += ["--radius", str(radius)]
        # ----- 2026-08 新增: 透传 detectionType (总是传,显式优于隐式) -----
        # 缺省 fallback 在 submit() 里归一为 "twoIllegal",这里假定一定有值;
        # 防御性兜底避免任何遗留代码路径让 detection_type 是 None。
        cmd += ["--detection-type", detection_type or "twoIllegal"]
        log.info("[%s] spawning: %s", task_id, " ".join(cmd))

        # Make sure the subprocess can find CLI tools shipped in the same
        # conda env as sys.executable (e.g. ``py3dtiles``). Without this,
        # uvicorn launched outside an activated env will inherit a PATH
        # that doesn't include the env's bin/, and stage 4 fails with
        # "RuntimeError: 未找到 py3dtiles".
        env_bin = str(Path(sys.executable).resolve().parent)
        sub_env = os.environ.copy()
        sub_env["PATH"] = env_bin + os.pathsep + sub_env.get("PATH", "")

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(self.scripts_dir),
            env=sub_env,
        )

    # Match "[N/4 <stage_name>] starting…" and "[N/4 <stage_name>] done in …".
    _STAGE_RE = re.compile(r"\[(\d)/4 ([a-z_]+)\] (starting|done)")

    def _reader_loop(self, task_id: str, proc: subprocess.Popen,
                     out_dir: Path) -> None:
        """Background thread: parse child stdout, then wait for exit."""
        last_error_line: Optional[str] = None
        try:
            if proc.stdout is None:
                raise RuntimeError(
                    f"[{task_id}] subprocess stdout is None "
                    f"(Popen must be created with stdout=PIPE)")
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                # Promoted from log.debug → log.info (2026-07): operators
                # reported that without this they couldn't see stage
                # progress in the service.log during long runs. INFO is
                # a per-stage line (4 starts + 4 done + 1 finalising per
                # task); cost is acceptable.
                log.info("[%s] %s", task_id, line)

                # Look for an "error-ish" line as a backup for errorMessage.
                lower = line.lower()
                if (("error" in lower or "traceback" in lower
                     or "exception" in lower)):
                    if last_error_line is None or len(line) > len(last_error_line):
                        last_error_line = line

                m = self._STAGE_RE.search(line)
                if m:
                    stage, kind = m.group(2), m.group(3)
                    if kind == "starting":
                        self._on_stage_start(task_id, stage)
                    else:
                        self._on_stage_done(task_id, stage)
                    continue

                if "Pipeline summary" in line:
                    self._update(task_id, step="finalizing", progress=95)
        except Exception:
            log.exception("[%s] reader thread error", task_id)

        rc = proc.wait()
        log.info("[%s] subprocess exited with rc=%s", task_id, rc)

        status = self._status.get(task_id)
        if status is None:
            return

        if rc == 0:
            self._finalize_success(task_id)
        else:
            status.state = "FAILED"
            sidecar_err = self._read_sidecar_error(out_dir)
            # Channel A (sidecar) > channel B (stdout last error line).
            base_err = (sidecar_err or last_error_line
                        or f"subprocess exit code {rc}")

            # ---- rc < 0 classification (2026-07) ----
            # Popen returns negative rc for signals (POSIX: rc = -signum).
            # SIGKILL (rc=-9) is what the cgroup OOM-killer sends when
            # the process blows past the memory.max limit. SIGABRT
            # (rc=-6) is what Python + glibc sometimes raise on heap
            # corruption. Both are now annotated explicitly so the
            # backend doesn't have to guess what "exit code -9" means.
            if rc < 0:
                signum = -rc
                if signum == 9:  # SIGKILL
                    oom_hint = (
                        f"subprocess killed by SIGKILL (rc=-9). Most likely "
                        f"the cgroup OOM-killer terminated it. Check "
                        f"`/sys/fs/cgroup/memory.events` for `oom_kill` "
                        f"and review ALGO_DBSCAN_VOXEL_M and the cgroup "
                        f"memory limit. See OPERATIONS.md §OOM for the "
                        f"debugging checklist."
                    )
                    base_err = f"{oom_hint} | {base_err}".rstrip(" |")
                elif signum == 6:  # SIGABRT
                    base_err = f"subprocess aborted (SIGABRT): {base_err}"
                else:
                    base_err = f"subprocess killed by signal {-rc}: {base_err}"

            status.error_message = base_err
            status.finished_at = time.time()
            # Try to upload any partial outputs that the algorithm left
            # on disk before failing — the callback can then carry
            # partial URLs (progressive display).
            self._scan_partial_outputs(status)
            # Fire-and-forget callback to the backend (best-effort).
            self._fire_callback(status)

        # Release the slot so the next queued task can start.
        # _release_slot is idempotent (set.discard + dict.pop default None).
        self._release_slot(task_id)

    @staticmethod
    def _read_sidecar_error(out_dir: Path) -> Optional[str]:
        sidecar = out_dir / "status.json"
        if not sidecar.is_file():
            return None
        try:
            data = json.loads(sidecar.read_text())
            return data.get("error")
        except Exception:
            return None

    def _on_stage_start(self, task_id: str, stage: str) -> None:
        start_p, _ = STAGE_PROGRESS.get(stage, (0, 0))
        self._update(task_id, step=STAGE_FRIENDLY.get(stage, stage),
                     progress=start_p)

    def _on_stage_done(self, task_id: str, stage: str) -> None:
        _, end_p = STAGE_PROGRESS.get(stage, (0, 0))
        self._update(task_id, step=STAGE_FRIENDLY.get(stage, stage),
                     progress=end_p)

    def _update(self, task_id: str, *, step: str, progress: int) -> None:
        status = self._status.get(task_id)
        if status is None:
            return
        status.step = step
        status.progress = progress

    def _finalize_success(self, task_id: str) -> None:
        status = self._status.get(task_id)
        if status is None:
            return

        out_dir = Path(status.output_dir or "")

        # Idempotent on the chunk list — v1 algorithm does NOT call
        # append_3dtiles_chunk, so we fall back to the conventional
        # <out>/3DTiles location here; v2 (chunked) algorithm will
        # have populated status.three_dtiles_paths progressively via
        # the hook, and we MUST NOT overwrite those (or we'd lose the
        # progressive state).
        if not status.three_dtiles_paths:
            tiles_dir = out_dir / "3DTiles"
            if tiles_dir.is_dir():
                status.three_dtiles_paths = [str(tiles_dir)]
            else:
                log.warning("[%s] 3DTiles dir missing at %s", task_id, tiles_dir)

        if status.instances_path is None:
            instances = out_dir / "instances.json"
            if instances.is_file():
                status.instances_path = str(instances)
            else:
                log.warning("[%s] instances.json missing at %s",
                            task_id, instances)

        # --- Sync upload: chunks first, then instances.json. We keep
        # state=RUNNING while this is in flight; a failure marks the
        # whole task FAILED with an OSS-related errorMessage. ---
        try:
            for local_path in status.three_dtiles_paths:
                if local_path in status.oss_chunk_urls:
                    continue  # uploaded already (e.g. by append_3dtiles_chunk)
                self._upload_chunk(status, Path(local_path))
        except Exception as e:
            status.state = "FAILED"
            status.error_message = f"OSS upload failed (chunks): {e}"
            status.finished_at = time.time()
            log.exception("[%s] OSS chunk upload failed", task_id)
            return

        if status.instances_path and not status.oss_instance_url:
            try:
                remote_key = self.uploader.make_key(task_id, "instance.json")
                status.oss_instance_url = self.uploader.upload_file(
                    Path(status.instances_path), remote_key)
                log.info("[%s] OSS instances.json uploaded: %s",
                         task_id, status.oss_instance_url)
            except Exception as e:
                status.state = "FAILED"
                status.error_message = f"OSS upload failed (instances.json): {e}"
                status.finished_at = time.time()
                log.exception("[%s] OSS instances upload failed", task_id)
                return

        status.state = "SUCCESS"
        status.progress = 100
        status.step = "completed"
        status.finished_at = time.time()
        log.info("[%s] task SUCCESS, %d chunk URL(s), instances=%s",
                 task_id,
                 len(status.oss_chunk_urls),
                 status.oss_instance_url)
        # Fire-and-forget callback to the backend (best-effort; the
        # polling endpoint is the source of truth regardless).
        self._fire_callback(status)

        # OSS has authoritative copies of the 3DTiles chunk(s) and
        # instances.json. Release the local copies now that the task
        # is SUCCESS: the FAILED path still needs them on disk so
        # `_scan_partial_outputs` can rescue whatever is salvageable
        # for progressive rendering, but SUCCESS tasks have no future
        # use of the bulky tileset tree on this host. Failures here
        # are non-fatal — OSS is the single source of truth; local
        # cleanup is just disk hygiene.
        try:
            for local_path in status.three_dtiles_paths:
                tp = Path(local_path)
                if tp.is_dir():
                    shutil.rmtree(tp, ignore_errors=True)
            if status.instances_path:
                ip = Path(status.instances_path)
                if ip.is_file():
                    ip.unlink()
        except Exception as cleanup_exc:
            log.warning(
                "[%s] cleanup of <out_dir>/3DTiles or instances.json "
                "failed (non-fatal, OSS copy is authoritative): %s",
                task_id, cleanup_exc)

    def _upload_chunk(self, status: TaskStatus, tiles_dir: Path) -> None:
        """Upload a single chunk's 3DTiles tree to OSS. The remote
        layout mirrors the local one: ``<prefix>/<taskId>/<chunk_subdir>``.

        For v1: ``local_path = <out>/3DTiles`` → OSS:
        ``<prefix>/<taskId>/3DTiles/...``.

        For v2 chunked: ``local_path = <out>/3DTiles/<chunk_subdir>`` →
        OSS: ``<prefix>/<taskId>/3DTiles/<chunk_subdir>/...``.

        On success, ``status.oss_chunk_urls[local_path]`` is set to the
        public tileset.json URL."""
        out_dir = Path(status.output_dir or "")
        # The local layout is <output_base>/<taskId>/3DTiles[/<chunk_sub>];
        # we want OSS key to start with <prefix>/<taskId>/3DTiles[/<chunk_sub>],
        # so we compute the path relative to <output_base>'s parent (i.e.
        # the prefix of <out_dir>). Fall back to a "3DTiles" leaf if the
        # path doesn't sit under the expected output base (defensive).
        try:
            rel = tiles_dir.relative_to(out_dir.parent)
        except ValueError:
            log.warning("[%s] chunk path %s is not under output_base %s; "
                        "falling back to v1 layout",
                        status.task_id, tiles_dir, out_dir.parent)
            rel = Path(status.task_id) / tiles_dir.name
        remote_prefix = self.uploader.make_key(str(rel).replace("\\", "/"))
        urls = self.uploader.upload_directory(
            tiles_dir, remote_prefix, exclude_globs=("tmp/*",))
        tileset_url = next(
            (u for u in urls if u.endswith("/tileset.json")), None)
        if tileset_url is None:
            raise RuntimeError(
                f"no tileset.json URL after upload: {tiles_dir}")
        status.oss_chunk_urls[str(tiles_dir)] = tileset_url
        log.info("[%s] OSS chunk uploaded: %s -> %s",
                 status.task_id, tiles_dir, tileset_url)

    # ----- terminal-state callback to backend (best-effort) -----

    def _scan_partial_outputs(self, status: TaskStatus) -> None:
        """FAILED 时尽力把已落盘的 3DTiles chunk + instances.json 上传 OSS，
        让 callback 能带上 partial URL（渐进显示设计）。失败只 warning，
        不影响 FAILED 判定。"""
        out_dir = Path(status.output_dir or "")
        if not out_dir.is_dir():
            return

        # 找 chunk 根目录
        three_dtiles_root = out_dir / "3DTiles"
        if three_dtiles_root.is_dir():
            # v1: <out>/3DTiles/tileset.json 直接在根
            if (three_dtiles_root / "tileset.json").is_file():
                candidates = [three_dtiles_root]
            else:
                # v2: <out>/3DTiles/<sub>/tileset.json
                candidates = [d for d in sorted(three_dtiles_root.iterdir())
                              if d.is_dir()
                              and (d / "tileset.json").is_file()]
            for c in candidates:
                try:
                    if str(c) not in status.oss_chunk_urls:
                        self._upload_chunk(status, c)
                except Exception as e:
                    log.warning("[%s] partial chunk upload failed for %s: %s",
                                status.task_id, c, e)

        # instances.json
        if status.instances_path is None:
            instances = out_dir / "instances.json"
            if instances.is_file():
                status.instances_path = str(instances)
        if status.instances_path and not status.oss_instance_url:
            try:
                remote_key = self.uploader.make_key(
                    status.task_id, "instance.json")
                status.oss_instance_url = self.uploader.upload_file(
                    Path(status.instances_path), remote_key)
                log.info("[%s] partial instances.json uploaded: %s",
                         status.task_id, status.oss_instance_url)
            except Exception as e:
                log.warning("[%s] partial instances upload failed: %s",
                            status.task_id, e)

    def _fire_callback(self, status: TaskStatus) -> None:
        """包装 _send_callback 到 daemon 线程。daemon=True 保证服务退出时
        不会等回调——但也意味着回调正在跑时服务被 kill 会丢当次回调，
        backend 必须靠 GET 轮询兜底（这是接受的）。"""
        if not self.callback_url:
            return
        t = threading.Thread(
            target=self._send_callback, args=(status,),
            daemon=True, name=f"callback-{status.task_id}",
        )
        t.start()

    def _send_callback(self, status: TaskStatus) -> None:
        """POST 终态到 backend。3 次指数退避；都失败 → CRITICAL 日志，
        状态不变（polling 仍是 source of truth）。"""
        if not self.callback_url:
            return  # 回调被禁用（配置为空）

        tiles_urls = [status.oss_chunk_urls[p]
                      for p in status.three_dtiles_paths
                      if p in status.oss_chunk_urls]
        payload = {
            "taskId":          status.task_id,
            "detectionType":   status.detection_type,    # 2026-08 新增
            "status":          status.state,        # SUCCESS 或 FAILED
            "progress":        str(status.progress),
            "3dtilesUrl":      tiles_urls or None,
            "instanceJsonUrl": status.oss_instance_url,
            "errorMessage":    status.error_message,
        }

        last_err: Optional[Exception] = None
        for attempt in range(1, self.callback_max_retries + 1):
            try:
                r = requests.post(
                    self.callback_url, json=payload,
                    timeout=self.callback_timeout,
                )
                r.raise_for_status()
                log.info("[%s] callback delivered (attempt %d, status=%d)",
                         status.task_id, attempt, r.status_code)
                return
            except Exception as e:
                last_err = e
                log.warning("[%s] callback attempt %d/%d failed: %s",
                            status.task_id, attempt,
                            self.callback_max_retries, e)
                if attempt < self.callback_max_retries:
                    time.sleep(min(2 ** attempt, 30))   # 2s, 4s, 8s

        log.error("[%s] callback failed after %d attempts; backend should "
                  "recover via /two-violation/tasks/{id}. Last error: %s",
                  status.task_id, self.callback_max_retries, last_err)