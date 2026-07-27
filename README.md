# illegal_construction_inspection

A 3D Tiles-based two-epoch change-detection pipeline that compares two
3D Tiles tilesets (epoch A "base" vs epoch B "compare") and produces
per-instance geometry of the differences, suitable for illegal
construction / vegetation encroachment inspection.

## Architecture

```
HTTP service (FastAPI)                Subprocess wrapper
        │                                     │
        ▼                                     ▼
  api_server.py ─── spawns ───► run_pipeline_subprocess.py
        │                                     │
        │                            (writes status.json)
        ▼                                     │
   task_manager.py                            ▼
        │                              run_pipeline.py
        │                              ┌─────────────┬──────────────┬─────────────┬──────────────────┐
        │                              │  Stage 1    │   Stage 2    │   Stage 3   │     Stage 4     │
        │                              │  extract    │   filter     │   NN        │   cluster +     │
        │                              │  3D Tiles   │   vegetation │   change    │   ECEF + 3D     │
        │                              │  → ENU pts  │   (ExG/CSF)  │   analysis  │   Tiles output  │
        │                              └─────────────┴──────────────┴─────────────┴──────────────────┘
        ▼
  callback (status: OK / FAILED) + GET /two-violation/tasks/{id}
```

- **Algorithm core** lives in `scripts/algorithm/` and is fully
  importable standalone (the FastAPI service is a thin HTTP wrapper
  around `run_pipeline.main`).
- **Service** lives in `scripts/service/` and handles HTTP dispatch,
  per-task subprocess lifecycle, OSS upload, and callbacks.
- **Tests** live in `tests/algorithm/`.

## Quick start

### 1. Install

The pipeline was developed against a conda env:

```bash
conda create -n illegal_construction_inspection python=3.12 -y
conda activate illegal_construction_inspection
# Key packages (full list to be added as requirements.txt — tracked
# in OPERATIONS.md until the env manifest is committed):
pip install numpy scipy open3d==0.19 laspy py3dtiles plyfile tqdm
pip install fastapi uvicorn
```

### 2. Run the service

```bash
./run_service.sh start     # uses PORT=6601 by default
./run_service.sh status    # → healthz
./run_service.sh stop
```

Override paths via env vars (see `run_service.sh`):

```bash
PORT=6601 OUTPUT_BASE_DIR=/var/lib/illegal-inspection/output \
  MODEL_ROOT=/model LOG_LEVEL=INFO ./run_service.sh start
```

### 3. Submit a task

```bash
curl -X POST http://localhost:6601/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d @/tmp/request.json
# → returns { taskId, state: PENDING }

# Poll:
curl -s http://localhost:6601/two-violation/tasks/<taskId> | python -m json.tool
```

## Documentation

- [`scripts/doc/OPERATIONS.md`](scripts/doc/OPERATIONS.md) — deploy,
  OOM troubleshooting, env vars, runtime artifacts.
- [`scripts/doc/BACKEND_API.md`](scripts/doc/BACKEND_API.md) — HTTP API
  contract (request/response shapes, error codes).

## Tests

```bash
source /root/miniconda3/etc/profile.d/conda.sh
conda activate illegal_construction_inspection
python -m pytest tests/algorithm/ -v
```

## Key algorithm knobs

| env var                      | default | meaning                                          |
| ---------------------------- | ------- | ------------------------------------------------ |
| `ALGO_DBSCAN_VOXEL_M`        | `0.5`   | DBSCAN input voxel decimation (m). `0` disables. |
| `ALGO_DBSCAN_EPS_M`          | `3.0`   | DBSCAN `eps` (m).                                |
| `ALGO_DBSCAN_MIN_POINTS`     | `120`   | DBSCAN `min_points`.                             |
| `ALGO_CSF_CLOTH_RESOLUTION`  | `2.0`   | Cloth Simulation Filter cloth spacing (m).       |
| `MAX_CONCURRENT_TASKS`       | `4`     | Per-process concurrency cap.                     |

See `scripts/algorithm/algo_config.py` for the full list.

## License

TBD.
