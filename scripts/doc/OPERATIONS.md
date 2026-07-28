# 服务运维文档（OPERATIONS）

> 适用版本：`scripts.service.api_server` 0.5.x
> 读者：算法同事、容器 / 服务器运维。

本文档说清楚"怎么把服务跑起来、跑通、改环境变量、排错"。HTTP 契约请读 `BACKEND_API.md`。

---

## 1. 目录结构

```
illegal_construction_inspection/
├── dataset/                         # 3D Tiles 时相数据（A / B / ...）
├── dataset_output/                  # 离线测试时的产出（run_pipeline 直跑）
└── scripts/
    ├── algorithm/                   # 算法本体（不要从 service 改这里，反过来也不要）
    │   ├── point_cloud_extraction.py
    │   ├── filter_vegetation.py
    │   ├── convert_point_ecef_and_3dtiles.py
    │   └── run_pipeline.py
    ├── service/                     # HTTP 服务层
    │   ├── api_server.py            # FastAPI 入口（uvicorn target）
    │   ├── task_manager.py          # 进程内任务表 + 进程生命周期 + OSS 上传编排
    │   ├── oss_uploader.py          # boto3 封装的 OSS / S3 客户端
    │   ├── oss_config.json          # OSS 凭证 / endpoint / bucket / 前缀配置（**含密钥，勿提交**）
    │   └── run_pipeline_subprocess.py  # 子进程包装，把 out_dir / xml 转给算法
    ├── visualization/               # Cesium 可视化
    │   ├── cesium_test.html
    │   └── switch_scene.py
    └── doc/
        ├── BACKEND_API.md           # 后端对接契约
        ├── OPERATIONS.md            # 本文件
        └── OSS存储方案.md            # 后端既有的 OSS 文档（**与本项目无关，仅作参考**）
```

`algorithm/` 和 `service/` 是分层关系：`service/api_server` → `service/run_pipeline_subprocess` → `algorithm/run_pipeline`（走 subprocess，跨目录）。修改时按这个方向：算法改 `algorithm/`，服务改 `service/`，可视化改 `visualization/`。

---

## 2. 环境准备

### 2.1 Conda 环境

算法服务运行在 conda env：

```
/root/miniconda3/envs/illegal_construction_inspection/
```

激活与核对：

```bash
conda activate illegal_construction_inspection
which python          # 应指向 /root/miniconda3/envs/.../bin/python
python -V             # 3.x 即可
```

### 2.2 关键依赖

服务本身：
- `fastapi` ≥ 0.110
- `uvicorn[standard]` ≥ 0.27
- `pydantic` ≥ 2.6

算法本体（不需要改）：
- `numpy` / `scipy` / `plyfile` / `laspy` / `open3d`
- `Pillow`（b3dm 贴图采样）
- `py3dtiles`（LAS → 3D Tiles 转换）

### 2.3 输出卷

默认服务把任务输出写到 `/home/zhangzhong/illegal_construction_inspection/dataset_output/tmp/`：

```
<OUTPUT_BASE_DIR>/<taskId>/
├── 3DTiles/                  # 算法产出的 3D Tiles 树
├── instances.json            # DBSCAN 簇信息
├── input.xml                 # 上传的 XML 归档（仅上传时）
├── request.json              # 提交快照（含原始+解析后路径；路径解析失败时不写出），见 §5.4
├── error.log                 # **仅 FAILED 时**: 完整 Python traceback（给运维查问题用）
└── status.json               # 子进程写给父进程的 sidecar
```

可用 `OUTPUT_BASE_DIR` 环境变量覆盖。**当前默认路径已在仓库内（`dataset_output/tmp/`），不需要挂额外卷；如要换到 `/data/output/` 之类的容器外位置，请用 `OUTPUT_BASE_DIR` 覆盖并确保该目录对 uvicorn 进程可写。**

### 2.4 模型路径解析（`baseModelPath` / `compareModelPath`）

后端传入的 `baseModelPath` / `compareModelPath` 可以是下面两种形式：

| 形式 | 例子 | 处理 |
|---|---|---|
| 原始绝对路径（向后兼容） | `/root/illegal_construction_inspection/dataset/wuxi_251022` | 当成文件系统路径直接用 |
| 后端虚拟路径（OSS key） | `Q28qessxDPimUsaRnThrT6uDPKAvJubJxdmCqjrcAEk=/tileset.json`<br>`%252FD%23%23%24.../tileset.json` | 单次 `urllib.parse.unquote` + 剥尾部 `/tileset.json`，再拼成 `<MODEL_ROOT>/<decoded>/` |

规则集中实现在 `api_server.py:resolve_model_path()`。`%2F` 故意保留为字面量——它是 `/model` 下文件夹名的一部分，不是路径分隔符。

`MODEL_ROOT` 默认 `/model`，可用环境变量覆盖：

```bash
export MODEL_ROOT=/data/models
```

如果两条规则都不命中（例如传了一个根本不存在的文件夹），子进程 `run_pipeline_subprocess.py` 会立刻报 `no tileset.json at <path>`，任务被标 `FAILED`，不会浪费资源启动算法。

---

## 3. 启动服务

### 3.1 启动命令

```bash
/root/miniconda3/envs/illegal_construction_inspection/bin/python -m uvicorn \
    scripts.service.api_server:app \
    --host 0.0.0.0 \
    --port 8901 \
    --workers 1
```

或者激活 conda 后：

```bash
cd /home/zhangzhong/illegal_construction_inspection
python -m uvicorn scripts.service.api_server:app --host 0.0.0.0 --port 8901 --workers 1
```

**必须 `--workers 1`**：任务表是进程内字典（`task_manager._status`），多 worker 不会共享。

### 3.2 验证

```bash
$ curl -s http://localhost:8901/healthz
{"status":"ok"}
```

或浏览器打开 `http://localhost:8901/docs` 看 FastAPI 自动生成的 Swagger UI。

### 3.3 日志

uvicorn 默认输出到 stdout，建议重定向到文件：

```bash
nohup /root/miniconda3/envs/illegal_construction_inspection/bin/python -m uvicorn \
    scripts.service.api_server:app --host 0.0.0.0 --port 8901 --workers 1 \
    > /tmp/uvicorn.log 2>&1 &
disown
tail -f /tmp/uvicorn.log
```

容器化部署请把 stdout 交给你们的日志收集。

---

## 4. 配置

### 4.1 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `PORT` | `8901` | uvicorn 监听端口 |
| `OUTPUT_BASE_DIR` | `/home/zhangzhong/illegal_construction_inspection/dataset_output/tmp` | 任务输出目录（含子进程写入） |
| `LOG_LEVEL` | `INFO` | Python logging 级别（`DEBUG` / `INFO` / `WARNING`） |
| `OSS_CONFIG` | `scripts/service/oss_config.json` | OSS 配置文件路径（绝对 / 相对均可） |
| `MAX_CONCURRENT_TASKS` | `4`（或在 `oss_config.json` 里设 `max_concurrent_tasks`） | 同时跑的 task 数；env 覆盖 config。`1` = 单飞（向后兼容旧版） |
| `ALGO_DBSCAN_VOXEL_M` | `0.1` | Stage 4 DBSCAN 输入下采样体素边长（米）。`0.1` 在 64 GiB cgroup 下 Stage 4 峰值 RSS 约 ~25–35 GiB（取决于 B 模型规模），把代表点从 ~10 M 裁到 ~500 k 同时保留较精细的 cluster 形状；老默认 `0.5` 走更激进的下采样（峰值 ~6–8 GiB，但 hull/bbox 形状被 NN-tiled 锯齿化）。设 `0` 关闭下采样（旧行为，可能 OOM）。详见 §7.5。 |

`PUBLIC_BASE_URL` **已废弃**（v0.5 起）：响应里的 `3dtilesUrl` / `instanceJsonUrl` 来自 OSS 配置（`oss_config.json` 的 `public_base`），不再由环境变量控制。

Docker / systemd 启动时直接注入即可。

设置示例（在外层 sh）：
```bash
export OUTPUT_BASE_DIR=/mnt/data/algo-output
export PORT=9000
export OSS_CONFIG=/etc/algo/oss_config.json   # 可选；默认就用仓库内的
python -m uvicorn scripts.service.api_server:app --host 0.0.0.0 --port 9000 --workers 1
```

### 4.2 OSS 配置文件

`scripts/service/oss_config.json`（`chmod 600`，**含明文密钥**）：

```json
{
  "endpoint":      "http://10.230.0.5:8009",
  "access_key":    "123",
  "secret_key":    "123",
  "bucket":        "hushi-test",
  "public_base":   "https://oss.ikingtec.com/hushi-test",
  "key_prefix":    "illegal-compare",
  "region":        "us-east-1",
  "use_presigned": false,
  "max_workers":   4,

  "backend_callback_url":     "http://192.168.4.20:8088/api/two-illegal-compare/tasks/callback",
  "callback_timeout_seconds": 10,
  "callback_max_retries":     3
}
```

| 字段 | 含义 |
|---|---|
| `endpoint` | boto3 SDK 用的端点（`S3` 客户端 `endpoint_url`）。当前是内部网关 `http://10.230.0.5:8009`（容器内能解析；`https://oss.ikingtec.com` 在容器内 DNS 不通） |
| `access_key` / `secret_key` | OSS 凭证 |
| `bucket` | 桶名（这里固定 `hushi-test`） |
| `public_base` | 拼返回 URL 的公共前缀（**不带尾部斜杠**）；公共读 bucket 时后端直接 `GET` 这个 URL |
| `key_prefix` | 桶内路径前缀（不带首尾斜杠），`illegal-compare/<taskId>/...` 里的 `illegal-compare` |
| `region` | boto3 签名用 region（浪潮 OSS 写 `us-east-1` 即可，签名不被校验） |
| `use_presigned` | `true` 时返回带签名的 URL（5 天有效）；`false` 时返回公共读 URL（要求 bucket 开了公共读） |
| `max_workers` | 上传并发 worker 数（4 worker × 4 线程 ≈ 16 个小文件并发），默认 4 |
| `backend_callback_url` | 算法终态（SUCCESS/FAILED）时主动 POST 的后端回调地址；**空字符串 = 禁用回调**，见 §4.3 |
| `callback_timeout_seconds` | 单次回调 HTTP 超时（秒），默认 10 |
| `callback_max_retries` | 回调失败重试次数（指数退避 2s/4s/8s，封顶 30s），默认 3 |
| `max_concurrent_tasks` | 同时跑的 task 数（默认 4），见 §9 性能；env `MAX_CONCURRENT_TASKS` 优先级更高 |

`.gitignore` 建议加 `oss_config.local.json`（如果想放本地覆盖版本不提交）。

### 4.3 后端回调（保底通知）

`backend_callback_url` 配的是后端提供的"对比进度回调接口"（v0.6 新增）。算法服务在子进程退出（无论成功失败）后，会**额外**主动 POST 一次到该地址，作为**保底通知**——主路径仍然是后端 GET `/two-violation/tasks/{taskId}` 轮询。

行为细节：

- **触发**：仅在终态（`SUCCESS` / `FAILED`）；RUNNING 中不发
- **线程**：daemon 线程，与主服务 / reader 线程解耦；回调失败/超时绝不阻塞新任务提交
- **超时 / 重试**：见上表字段；都失败 → `ERROR` 日志 `callback failed after N attempts`，状态保持终态不变
- **payload schema 与示例**：[BACKEND_API.md §4.6](BACKEND_API.md#46-终态回调保底通知)
- **关闭回调**：把 `backend_callback_url` 设为 `""`，重启服务即可。轮询端点行为完全不变

后端建议：按 `taskId` 做幂等去重；handler 响应快（≤10s）；容忍回调丢失——下次 GET 仍能拉到完整状态。

---

## 5. 测试（端到端）

### 5.1 准备数据

算法读 3D Tiles，路径下要有 `tileset.json`：

```bash
ls /data/dataset/dongshan_2509/tileset.json   # 必须存在
ls /data/dataset/dongshan_2604/tileset.json
```

### 5.2 提交任务（不带 XML）

```bash
curl -s -X POST http://localhost:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "taskId":       "TW-DEMO",
    "baseModelPath":    "/data/dataset/dongshan_2509",
    "compareModelPath": "/data/dataset/dongshan_2604"
  }'
```

预期：`{"code":0,"message":"success","data":{"taskId":"TW-DEMO","status":"PENDING","errorMessage":null}}`

### 5.3 带 base64 XML 提交

```bash
B64=$(base64 -w0 /path/to/test.xml)
curl -s -X POST http://localhost:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d "{
    \"taskId\":\"TW-DEMO-XML\",
    \"baseModelPath\":\"/data/dataset/dongshan_2509\",
    \"compareModelPath\":\"/data/dataset/dongshan_2604\",
    \"xmlFile\":\"$B64\"
  }"
```

提交后可在容器内核对：`cat /data/output/TW-DEMO-XML/input.xml` 应能看到原始 XML。

### 5.4 带可选元数据提交（positionMode / areaCoordinates / radius）

后端除了 3D Tiles 路径外，还可携带三个可选元数据字段（v0.6 起，仅为后续 hook 预留；算法本体目前不消费）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `positionMode` | string | 坐标系标识（e.g. `"WGS-84"`） |
| `areaCoordinates` | list[dict] | 感兴趣区多边形顶点，每个 dict 含 `{latitude, longitude, altitude}` |
| `radius` | float | 半径（米） |

```bash
curl -s -X POST http://localhost:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "taskId":      "TW-META",
    "baseModelPath":    "/data/dataset/dongshan_2509",
    "compareModelPath": "/data/dataset/dongshan_2604",
    "positionMode":"WGS-84",
    "areaCoordinates":[
      {"altitude":13.51,"latitude":31.4912779,"longitude":121.0935673},
      {"altitude":13.64,"latitude":31.4911645,"longitude":121.0936537}
    ],
    "radius":500
  }'
```

提交后可在容器内核对 `request.json`：

```bash
cat /root/illegal_construction_inspection/output/TW-META/request.json
# 期望:
# {
#   "taskId": "TW-META",
#   "submittedAt": "2026-07-21T08:35:16+00:00",
#   "baseModelPath": "/abc/def_v1",
#   "compareModelPath": "/abc/def_v2",
#   "baseModelPathResolved": "/model/def_v1/tileset.json",
#   "compareModelPathResolved": "/model/def_v2/tileset.json",
#   "positionMode": "WGS-84",
#   "areaCoordinates": [
#     {"altitude": 13.51, "latitude": 31.4912779, "longitude": 121.0935673},
#     ...
#   ],
#   "radius": 500.0,
#   "xmlPath": null
# }
```

`request.json` 是**纯快照**——算法本体和上传流程都不会读它（`areaCoordinates` 由 argv 透传，不经过此文件），仅供人工审计 / 路径解析排错用。三个 Optional 字段不传时对应值为 `null`。**仅在路径解析成功后写出**；`resolve_model_path` 抛错时此文件不生成——这种场景直接看 HTTP 响应的 `errorMessage` 即可。

### 5.5 轮询直到终态

```bash
while true; do
  resp=$(curl -s http://localhost:8901/two-violation/tasks/TW-DEMO)
  s=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['status'])")
  echo "status=$s"
  case "$s" in SUCCESS|FAILED) break;; esac
  sleep 15
done
```

SUCCESS 后，`data.3dtilesUrl[0]` 与 `data.instanceJsonUrl` 就是云端 URL（受 `PUBLIC_BASE_URL` 控制）。

### 5.6 错误路径验证

服务**不**在提交时校验路径 / XML 大小，所有业务错误都走 `code: 500` + `errorMessage`：

```bash
# 1) 重复 taskId → code:500, errorMessage 含 "task_id already exists"
curl -s -X POST http://localhost:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "taskId":"TW-DEMO",
    "baseModelPath":"/data/dataset/dongshan_2509",
    "compareModelPath":"/data/dataset/dongshan_2604"
  }'

# 2) 路径无效 → 提交仍 code:0；轮询会变 FAILED
curl -s -X POST http://localhost:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "taskId":"TW-BAD",
    "baseModelPath":"/no/such/path",
    "compareModelPath":"/data/dataset/dongshan_2604"
  }'
# 轮询
curl -s http://localhost:8901/two-violation/tasks/TW-BAD
# 期望: code:500, status:FAILED, errorMessage 含 "no tileset.json"

# 3) taskId 不存在 → 直接 code:500
curl -s http://localhost:8901/two-violation/tasks/TW-NOT-EXIST
# 期望: {"code":500,"message":"failed","data":{"taskId":"TW-NOT-EXIST",
#         "status":"FAILED","errorMessage":"taskId not found: TW-NOT-EXIST",...}}

# 4) Pydantic 422：taskId 缺字段 / 格式错 → HTTP 422（非业务码）
curl -s -X POST http://localhost:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d '{"baseModelPath":"/a","compareModelPath":"/b"}'
# 期望: HTTP 422 + {"detail":[...]}
```

### 5.7 多任务并行（默认 N=4）

```bash
# 0) 确认服务起来了 + 端口对
curl -s http://localhost:8901/healthz   # {"status":"ok"}

# 1) 1 秒内连发 5 个 task。期望：1-4 号 → code:0 + 1-2s 内变 RUNNING；
#    5 号 → code:0 立刻返，但 status=PENDING/step=waiting（排在队尾）。
TS=$(date +%s)
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8901/two-violation/compare \
    -H 'Content-Type: application/json' \
    -d "{\"taskId\":\"par-$i-$TS\",
         \"baseModelPath\":\"/data/dataset/dongshan_2509\",
         \"compareModelPath\":\"/data/dataset/dongshan_2604\"}" | \
    python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(f\"submit par-$i-$TS → {d['status']}\")"
done

# 2) 3 秒后查 5 个 task 的 status
sleep 3
for i in 1 2 3 4 5; do
  curl -s http://localhost:8901/two-violation/tasks/par-$i-$TS | \
    python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(f\"par-$i-$TS: {d['status']} {d['progress']}%\")"
done
# 期望: 1-4 号 RUNNING (或 SUCCESS/FAILED 已完工);5 号 PENDING/waiting

# 3) ps 应能看到 4 个 run_pipeline_subprocess 子进程
ps -ef | grep run_pipeline_subprocess | grep -v grep | wc -l
# 期望: 4 (前提：1-4 号还没跑完)

# 4) 1 号跑完后，5 号应自动变 RUNNING（dispatcher 拉起）
#    等 1 号完工（可能几分钟），再查 5 号
sleep 30
curl -s http://localhost:8901/two-violation/tasks/par-5-$TS | \
  python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(f\"par-5-$TS: {d['status']} {d['progress']}%\")"
# 期望: RUNNING 或 SUCCESS

# 5) 重复 taskId 仍 500（与并发无关的不变量）
curl -s -X POST http://localhost:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d "{\"taskId\":\"par-1-$TS\",
       \"baseModelPath\":\"/data/dataset/dongshan_2509\",
       \"compareModelPath\":\"/data/dataset/dongshan_2604\"}"
# 期望: code:500, errorMessage 含 "task_id already exists"

# 6) N=1 回退：把 oss_config.json 改成 1，重启，行为完全等同旧版（永远只跑 1 个）
sed -i 's/"max_concurrent_tasks": *4/"max_concurrent_tasks": 1/' \
  /root/illegal_construction_inspection/scripts/service/oss_config.json
/root/illegal_construction_inspection/run_service.sh restart
# 提交 2 个：第 1 个 RUNNING，第 2 个 PENDING/waiting
```

---

## 6. 输出位置

### 6.1 本地（算法服务写）

```
<OUTPUT_BASE_DIR>/<taskId>/
├── 3DTiles/                # 算法产出 3D Tiles（tileset.json + 各 .pnts；v1 单 chunk 整棵在这里）
│   └── tmp/                # 算法中间产物（**不上传**）
├── instances.json          # DBSCAN 簇列表（JSON）
├── input.xml               # 上传 XML 归档（仅当上传）
├── request.json            # 提交快照（含原始+解析后路径；路径解析失败时不写出，可能全 null）
├── error.log               # **仅 FAILED 时**: 完整 Python traceback,给运维查问题用
└── status.json             # 子进程 → 父进程 sidecar（task 结束后保留）
```

默认 `<OUTPUT_BASE_DIR> = /home/zhangzhong/illegal_construction_inspection/dataset_output/tmp`，即实际落地为：

```
/home/zhangzhong/illegal_construction_inspection/dataset_output/tmp/<taskId>/...
```

### 6.2 云端（算法服务同步上传）

任务跑完后，**算法服务**在 reader 线程里把 3D Tiles + `instances.json` 同步上传到 OSS：

```
<public_base>/<key_prefix>/<taskId>/
├── 3DTiles/
│   ├── tileset.json
│   ├── r.pnts
│   ├── r0.pnts
│   ├── r1.pnts
│   └── ...
└── instance.json
```

按当前默认配置，落地为：

```
https://oss.ikingtec.com/hushi-test/illegal-compare/<taskId>/3DTiles/tileset.json
https://oss.ikingtec.com/hushi-test/illegal-compare/<taskId>/3DTiles/r.pnts
...
https://oss.ikingtec.com/hushi-test/illegal-compare/<taskId>/instance.json
```

上传规则：

- 时机：算法子进程退出 0 → 同步上传所有 chunks → 同步上传 `instances.json` → 状态置 `SUCCESS`
- 范围：`<out>/<taskId>/3DTiles/` 整棵子树，**排除 `3DTiles/tmp/`**
- 并发：4 worker（约 260 个文件 / 20 MB 的 task 实测 5–15 秒传完）
- 失败：整 task 标 `FAILED`，`errorMessage` 含 OSS 异常

清理某个旧任务：
```bash
# 本地
rm -rf /home/zhangzhong/illegal_construction_inspection/dataset_output/tmp/<taskId>
# 云端
# 删 <public_base>/<key_prefix>/<taskId>/ 整棵（用 boto3 / OSS 控制台）
```

> 注：`task_manager` 是**进程内**字典，重启 uvicorn 后历史的 `taskId` 全部失效（GET 返 `code:500 + "taskId not found"`），但本地 `<OUTPUT_BASE_DIR>/<taskId>/` 与 OSS 上的 `illegal-compare/<taskId>/` **不会**被自动清掉。

---

## 7. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| uvicorn 启动报 `Address already in use` | 8901 端口被占 | `lsof -i :8901` 找 PID，杀掉；或换 `PORT` |
| uvicorn 启动报 `ModuleNotFoundError: No module named 'scripts.service...'` | 不是从仓库根目录启动 | `cd /home/zhangzhong/illegal_construction_inspection` 再起 |
| uvicorn 启动报 `FileNotFoundError: OSS config not found: ...` | `OSS_CONFIG` 指向了不存在的文件，或 `oss_config.json` 不在 `scripts/service/` 下 | 把 `oss_config.json` 放回 `scripts/service/`，或 `export OSS_CONFIG=/path/to/your.json` |
| `/healthz` 200 但 `POST /two-violation/compare` 一直 500 | conda env 错了，或 `sys.executable` 找不到子进程依赖 | 看 `/tmp/uvicorn.log` 找到 trace，确认环境 |
| 提交返 `code:500 + "task_id already exists"` | 同 taskId 已注册 | 用新 taskId |
| 任务卡在 `PENDING`（step=waiting）很久不动 | 已在 queue 里等 slot（`max_concurrent_tasks` 满了）。N=4 默认下，队尾第 5 个要等前 4 个完工 | 看 `service.log` 找 `[<task_id>] queued (...)`；前几个任务完工后 dispatcher 自动拉起；不必取消 |
| 任务永远 `RUNNING` 不出 | 子进程挂了或卡死 | 看 sidecar `/data/output/<taskId>/status.json` 看 `error`；看 `/tmp/uvicorn.log` 找 trace |
| 任务 `FAILED` 看错误 | 看 `GET /two-violation/tasks/{id}` 的 `errorMessage` 字段，并读 `status.json` |
| `errorMessage` 不够详细 / 被截断 | 读 `<out_dir>/error.log` —— 那是完整的 Python traceback(`_truncate_error` 在 500 字符以上才截，源码侧一般只有最后一行异常 + 关键路径) |
| `instances.json` 没产出 | 算法中途报错 | 同上，再看 sidecar + uvicorn log |
| 任务 `FAILED + "OSS upload failed ..."` | 算法跑完了但 OSS 上传挂了 | 看 uvicorn log 找根因（endpoint 不通 / 凭证错 / bucket ACL 错 / 网络）；`errorMessage` 含详细错误 |
| 任务 `SUCCESS` 但 `3dtilesUrl` 拉不到 | 后端访问 `https://oss.ikingtec.com/...` 时 DNS 不通 | 后端改用内部网关端点 `http://10.230.0.5:8009/hushi-test/...`；或 DNS 配好 `oss.ikingtec.com` |
| 任务 `SUCCESS` 但 `3dtilesUrl` 返 403 | bucket 公共读没开 | 把 `oss_config.json` 的 `use_presigned` 改成 `true`，URL 会带 5 天 `?X-Amz-...` 签名 |
| 日志 `callback delivered (attempt N, status=200)` 看不到，但任务已 SUCCESS | 回调跑了 → 成功送达 200 | 正常，无需处理 |
| 日志 `callback attempt N/3 failed: ...` 一连 3 条，最后 `callback failed after 3 attempts` | 后端回调 URL 不可达 / 后端 handler 报错 / 网络抖动 | 检查后端 `192.168.4.20:8088` 是否可达；后端 handler 日志有无 4xx/5xx；任务状态仍 `SUCCESS`（polling 是 source of truth），后端通过 GET 自愈 |
| 后端完全没收到回调，但状态对 | 回调丢失（服务重启 / 进程被杀 / 网络断） | 正常设计——主路径是 GET 轮询；下次 GET 即恢复 |
| OOM / Python 进程被杀 | tileset 太大，算法 Pass 2 吃满 CPU + 内存 | 见 §7.5 详细排查；v0.8+ 默认开启 DBSCAN 体素下采样（`ALGO_DBSCAN_VOXEL_M=0.1`），B 模型峰值 RSS 已从 ~50 GiB 降到 ~25–35 GiB |

### 7.5 OOM 排查（`exit code -9` / cgroup OOM-killer）

2026-07 之前的 OOM 是 "幽灵"：cgroup OOM-killer 发送 `SIGKILL`，Python 拿不到任何异常，`<out>/error.log` 空白，`status.json` 卡在 `progress=80`。v0.8+ 做了三层防护：

1. **算法层（根治）**：`scripts/algorithm/convert_point_ecef_and_3dtiles.py:cluster_instances` 在 `open3d.cluster_dbscan` 之前用体素下采样（默认 `ALGO_DBSCAN_VOXEL_M=0.1`）把输入从 ~10 M 点降到 ~500 k 代表点，cKDTree 回投标签。Stage 4 峰值 RSS 从 ~50 GiB 降到 ~25–35 GiB，64 GiB cgroup 限制下 2–3 路并发安全；老默认 `0.5` 更激进（峰值 ~6–8 GiB，4 路并发），但 hull/bbox 形状被 NN-tiled 锯齿化。
2. **算法层（清晰报错）**：`stage_convert` 入口跑 `_estimate_peak_gib(...)`，超 cgroup 80% 时 `RuntimeError("OOM: expected peak X GiB > 80% of cgroup limit Y GiB ...")`，被 `errorMessage` 透传给后端。
3. **服务层（兜底识别）**：`task_manager._reader_loop` 在 `rc < 0` 时显式标注 `SIGKILL` (rc=-9) / `SIGABRT` (rc=-6)，给 `errorMessage` 拼上前缀：
   ```
   subprocess killed by SIGKILL (rc=-9). Most likely the cgroup OOM-killer
   terminated it. Check `/sys/fs/cgroup/memory.events` for `oom_kill` and
   review ALGO_DBSCAN_VOXEL_M and the cgroup memory limit. See
   OPERATIONS.md §7.5 for the debugging checklist. | <原 sidecar error>
   ```
4. **服务层（提交时预检）**：`api_server.submit` 在 `store.submit` 之前对 B tileset 做 header-only b3dm 扫描（不读 BIN chunk），算 `n_diff_est = N_b × 0.05`，预测 Stage 4 峰值；超 cgroup 80% 直接 `_submit_fail` 拒绝入队，**响应里立刻**拿到清晰的 OOM 原因。

**诊断步骤（任务已 FAILED 之后）：**

1. 看 `GET /two-violation/tasks/{id}` 的 `errorMessage`：包含上面那段 `SIGKILL (rc=-9)` 字样 → 确认是 cgroup OOM-killer。
2. 进容器，看 `cat /sys/fs/cgroup/memory.events`：`oom_kill` 计数器 > 0 就是它干的。
3. 看子进程输出里有没有 `[rss] peak RSS = X.X GiB`（每次任务结束都打一条）。`X > 0.8 × cgroup_max_GiB` 就是预料之中的 OOM。
4. 处置：
   - **首选**：不动 `ALGO_DBSCAN_VOXEL_M`（默认 `0.1` 已经能跑 B 同时保留较精细的 cluster 形状），先看是不是 cgroup 限额太紧（`docker inspect` 看 `Memory`）。
   - **需要更低内存峰值**（超大规模 tileset / cgroup < 64 GiB）：设 `ALGO_DBSCAN_VOXEL_M=0.3` 或 `0.5`（cluster 形状更锯齿化，但峰值 ~6–8 GiB）。
   - **极稀疏点云**（NN > 0.5 m）或对精度极度敏感：设 `ALGO_DBSCAN_VOXEL_M=0`，跳过下采样（旧行为，~50 GiB 峰值）。

**已知 cgroup 限额参考：**

| 部署 | `--memory=` | `--memory-swap=` | B 模型峰值（默认 `VOXEL_M=0.1`） | 老默认 `0.5` |
|---|---|---|---|---|
| 64 GiB 容器（当前默认） | `64g` | `64g` | ~25–35 GiB，2–3 路并发 | ~6–8 GiB，4 路并发 |
| 32 GiB 容器 | `32g` | `32g` | ~25–35 GiB，1 路并发 | ~6–8 GiB，2–3 路并发 |
| 16 GiB 容器 | `16g` | `16g` | 不够；需 `ALGO_DBSCAN_VOXEL_M≥0.5` | ~6–8 GiB，1 路并发 |

**NumPy ≥ 2.0 `copy` 关键字陷阱（f32 输入下必 fail）：**

`numpy 2.0` 收紧了 `np.asarray(arr, dtype, copy=False)` 的语义：dtype 不匹配时直接抛 `ValueError: Unable to avoid copy while creating an array as requested.`（旧版静默 copy）。这跟 `ndarray.astype(dtype, copy=False)` 不一样 —— 后者**仍然**静默 copy（"软" copy=False）。

```python
# BAD — f32 输入必 fail：
sub_pts = np.asarray(xyz_enu[sub_idx], dtype=np.float64, copy=False)
# → ValueError: Unable to avoid copy while creating an array as requested.

# GOOD — 让 dtype 透传输入：
sub_pts = np.asarray(xyz_enu[sub_idx])

# ALSO BAD — 静默 copy（不抛错，但浪费内存）：
source_xy = np.ascontiguousarray(source_xy, dtype=np.float64)
# → 静默分配 f64 副本，N=50M 时 ~400 MB extra peak

# GOOD — 让 dtype 透传：
source_xy = np.ascontiguousarray(source_xy)
```

在生产路径上踩到过两次：
- `cluster_instances` 的 f32 `pts_diff` 输入 → `ValueError`（fatal，stage 4 直接挂）。见 `tests/algorithm/test_dbscan_decimate.py::test_cluster_instances_accepts_f32_input` 回归测试。
- `_idw` 的 f32 `source_xy` / `query_xy` 输入 → 静默 copy ~1.2 GiB peak（fatal 程度低，但雪上加霜）。

写新代码或 review 时一条规则：**永远不要在 `np.asarray(..., dtype=X, copy=False)` 上同时改 dtype**。要不就不要 `copy=False`，要不就让 dtype 透传，要么用 `ndarray.astype(X, copy=False)`（"软" copy=False 不会抛错，但会 copy）。

**已知 v0.8 regression（已修复）：凸包 / 3D Tiles ECEF 偏移 4,651 km**

v0.8 内存优化在 `scripts/algorithm/run_pipeline.py:stage_convert` 把 ECEF 公式从齐次形式

```python
homog = np.hstack([pts_diff, np.ones((N, 1))])
ecef  = (homog @ T.T)[:, :3]      # 旧：占用 (N,4) 中间值
```

改写成代数形式以省掉 `(N,4)` 中间数组：

```python
T = np.asarray(transform_b, dtype=np.float64).reshape(4, 4, order="F")
ecef = pts_diff @ T[:3, :3].T + T[3, :3]   # ← 这里索引错了
```

但 `T` 是按 3D Tiles 规范 **column-major** `reshape(order="F")`，平移列在 `T[:3, 3]`（不是 `T[3, :3]` —— 那是齐次行 `[0, 0, 0]`）。结果整个 `ecef` 丢了 EC EF 平移，所有凸包 / 3D Tiles 输出整体偏移局部原点的 ECEF 坐标，**上海区域实测 ~4,651 km**。

**修复**（`scripts/algorithm/run_pipeline.py:535`）：

```python
ecef = pts_diff @ T[:3, :3].T + T[:3, 3]   # T[:3, 3] 是平移列
```

修复后与旧齐次形式 **bit-exact**（50 个随机 rigid transform × 100 随机点全部相等）。回归测试：`tests/algorithm/test_ecef_algebraic.py`，4 个 case（identity / Shanghai 真实 transform / 50 个随机 rigid / buggy form 控制），同时断言 ECEF 输出在百万米量级（ECEF 范围），不是局部米。

**判定这次 bug 的方法**：跑完任务后 `jq '.clusters[0].bbox_center_ecef' output/<id>/instance.json`，值应当在 `[-3e6, 5e6, 4e6]` 量级；若只有几十到几百米 → bug 复现。

**已知 bug（已修复）：pre-flight 卡死 → API 提交挂起，任务永远进入不到 `_status`**

`submit()` 入口跑 `find_leaf_b3dms_with_bbox(base_path)` 扫 base 模型的 b3dm header，预估 Stage 4 内存。这个调用 `open()` / `read()` b3dm 文件，**当 mount（NFS / FUSE / 任何走 RPC 的文件系统）响应慢的时候会在内核态进入 `D` 状态（uninterruptible sleep, wchan `rpc_wa`）**——Python 信号、try/except timeout、asyncio timeout 都不起作用，因为是 syscall 在阻塞。

**症状**（2026-07 多起实测）：
- POST `/two-violation/compare` 写出了 `request.json` 但**永远不返回响应**
- service.log 在 `resolved paths` 之后没有任何 pre-flight / spawning 日志
- GET `/tasks/{id}` 返回 HTTP 200 + body `code:500 errorMessage:"taskId not found"`（**HTTP 200 误导**：FastAPI 默认 status 是 200，错误状态码在 JSON body 里）
- 任务**永远**进不到 TaskStore：`store.submit()` 调用前就被卡住了

**修复**（`scripts/service/api_server.py`）：

新增 `_preflight_with_timeout(base_path, timeout_s)`：把 b3dm scan 放到一个 **daemon 线程**里跑，**主线程 `thread.join(timeout_s)`** 卡到超时为止。超时即返回 `{"status": "timeout"}`，submit 走 **fail-open**（跳过 OOM early-reject 直接进 `store.submit()`）。子类进程入口的 `run_pipeline.stage_convert` 有同一个 `_estimate_peak_gib` 兜底，太大的任务会在那里干净地抛 `RuntimeError("OOM: ...")` 而不是 hang 整个 API。

可调超时（默认 30 s）：

```bash
PREFLIGHT_TIMEOUT_S=60 uvicorn ...     # 把超时改成 60s
PREFLIGHT_TIMEOUT_S=0  uvicorn ...     # 关掉超时（不推荐，会回到老 bug 行为）
```

**daemon thread 善后**：超时后线程被扔在后头继续等 RPC 响应（内核 D 状态没法杀）。**这没事**——它是 daemon（不阻塞 uvicorn 退出），不持有共享状态、文件描述符、socket；最坏的情况：uvicorn 重启时一起消失。NFS wedged 恢复后该线程也会自然结束。

**回归测试**：`tests/service/test_preflight_timeout.py`，4 个 case（fast scan / hanging scan / raising scan / daemon check），都用 `monkeypatch` 把 inner scan 替换成 stub，不碰真实 b3dm。

**判定这次 bug 的方法**：

```bash
# 1. 看 uvicorn 是否有线程卡在 D 状态
ps -L -p $(pgrep -f uvicorn) -o pid,tid,stat,wchan,comm | awk '$3=="D"'
#   若有 wchan=rpc_wa / nfs_wait / fuse_await → 命中此 bug

# 2. 看 service.log 该任务在 "resolved paths" 之后是否还有后续行
grep -A20 "your-task-id.*resolved paths" service.log
#   应当紧跟 pre-flight: n_b=...;若 30 秒内没有 → 命中此 bug（旧版本）
```

**避免**：升级到新版 `api_server.py` 后，pre-flight 30 s 还没回就直接进队，submit handler 不再挂起。

---

## 8. 日志与监控

| 来源 | 位置 / 形式 |
|---|---|
| uvicorn 启动 + 主请求 | stdout（按部署，重定向到 `/tmp/uvicorn.log` 等） |
| 子进程 stdout | 通过 subprocess.PIPE 回到主进程，并入 uvicorn 日志；筛选 `[1/4 ...] [2/4 ...]` 等 stage marker |
| 任务最终结果 | sidecar `/data/output/<taskId>/status.json`（含 step/progress/status/error） |
| XML 落地 | `/data/output/<taskId>/input.xml`（仅上传时） |
| 可选元数据落地 | `/data/output/<taskId>/request.json`（**路径解析成功才写出**，含 taskId / submittedAt(UTC ISO8601) / baseModelPath / compareModelPath / baseModelPathResolved / compareModelPathResolved / positionMode / areaCoordinates / radius / xmlPath 共 10 个字段） |
| 完整 traceback 落地 | `/data/output/<taskId>/error.log`（**仅 FAILED 时写出**；HTTP 响应里的 `errorMessage` 只是短摘要，需要完整栈帧来这里查） |
| 回调成功 | `[<taskId>] callback delivered (attempt N, status=200)` |
| 回调失败单次 | `[<taskId>] callback attempt N/M failed: <reason>`（WARNING） |
| 回调彻底失败 | `[<taskId>] callback failed after N attempts; backend should recover via /two-violation/tasks/{id}`（ERROR，状态仍为终态） |

**阶段 marker 抓取示例**（如果想用 tail 实时观察阶段）：

```bash
tail -f /tmp/uvicorn.log | grep -E "\[[0-9]/4"
# 输出形如：
# [1/4 extract_leaf_vertices] starting…
# [1/4 extract_leaf_vertices] done in    12.3s
# ...
# Pipeline summary
```

---

## 9. 性能与并发

| 项 | 数值 |
|---|---|
| 并发任务数 | **默认 4**（`task_manager._max_concurrent`）；env `MAX_CONCURRENT_TASKS` 或 `oss_config.json:max_concurrent_tasks` 可覆盖。`N=1` 走 fast-path 退化为单飞 |
| 排队行为 | 超出 N 的提交立刻 `code:0` + `state=PENDING/step=waiting` 入队 FIFO；前 N 个完工后 dispatcher 拉起 |
| Pass 2 并行度 | CPU 核数（自动）/ 单 tileset 串行两 epoch |
| 内存峰值 | 视 tileset 大小；典型单 epoch 1-2 GB。N 个并发 → ~`(2-4 GB) × N` 合计 host 占用。N=4 → ~8-16 GB。**生产部署按 host 内存调小** |
| 单 tileset wall-clock | 主要由 Pass 2 的 b3dm texture 采样决定（CPU-bound），典型 5-30 分钟 |
| OSS 上传开销 | 算法跑完后同步上传；4 worker 并发，~260 个文件 / 20 MB 约 5-15 秒（容器→10.230.0.5:8009 内部链路） |
| 磁盘占用 | 输出 `instances.json` 5 MB 量级；`3DTiles/` 与算法输入成正比 |

**多任务并行下的已知 caveat**：
- `nn_change_analysis` 阶段用 `workers=-1`（`multiprocessing.Pool` 抓满所有 CPU 核）。N=4 时 4 个 task 互相抢核会 thrash——单 task 实测 wall-clock 变长。**目前 plan 不做自动 cap**，需要手动设 `MAX_CONCURRENT_TASKS=2` 或在算法里改 `workers=cpu_count()//N`。
- 排队只防超量启动，**不防 OOM**：如果 N 个 task 合计内存超过 host 物理内存，kernel 杀进程由 OOM killer 兜底。

**算法本体怎么跑得更快：**
- 关掉 texture：`stage_extract(..., with_color=False)`（instances.json 不带 RGB）
- 减少 worker：`extract_point_cloud(workers=1)`（`run_pipeline.py` 内）
- 关注 `point_cloud_extraction.py` 的 stage metadata

---

## 10. 关闭 / 重启

```bash
# 关
pkill -f uvicorn

# 启（与 §3 同）
nohup /root/miniconda3/envs/illegal_construction_inspection/bin/python -m uvicorn \
    scripts.service.api_server:app --host 0.0.0.0 --port 8901 --workers 1 \
    > /tmp/uvicorn.log 2>&1 &
disown
```

**注意**：重启会丢失进程内任务表 —— 已完成的任务输出文件还在 `/data/output/` 里，但如果调用方继续用旧 `taskId` 轮询，会收到 `code:500 + "taskId not found"`，需要重新提交。

---

## 11. 算法同事修改流程

1. 改 `algorithm/*.py`
2. 在 `dataset/` 找两个时相直接 `python run_pipeline.py <a> <b> -o ./dataset_output/test/<group>` 跑一遍，确认产物对齐预期
3. 跑通后再通过 HTTP 提交一次（`scripts.doc.BACKEND_API.md` §6），端到端验证

> 直跑 vs 通过服务两种方式的差异：
> - 直跑：算法自己写到 `dataset_output/`，可以逐 stage 看 print / PLY 调试
> - 服务跑：算法由 `run_pipeline_subprocess` 启起来，进度走 `status.json` + stdout 解析

---

## 12. 不做的事（这条很重要）

| 项 | 暂未实现 |
|---|---|
| 任务取消 | 没有 cancel endpoint，要停就 `pkill` |
| 鉴权 / CORS | 没有，反代层做 |
| XML 大小校验 | 没有；上传 100MB XML 服务端会全量 base64 decode + 写盘——客户端控制大小即可 |
| K-means 等替代聚类 | `convert_point_ecef_and_3dtiles.py` 当前用 DBSCAN |
| 进度回调到外部 | 进度通过 GET 拉，不主动推 |
| 多 chunk 渐进式 | v2 算法尚未实现，接口已预留 `task_manager.append_3dtiles_chunk`（v1 算法不会调；详见 BACKEND_API.md §10） |
| 算法服务代理子文件下载 | 没有；`3dtilesUrl` / `instanceJsonUrl` 是 OSS 公共读 URL，下载由后端负责 |
| OSS 失败自动重试 | 没有；上传失败整个 task 转 FAILED；运维查 `errorMessage` 后重新提交新 taskId |
| OSS 跨 region 复制 / CDN | 没有；`oss_config.json` 写哪个就用哪个 |