# 后端对接文档（BACKEND_API）

> 适用版本：`scripts.service.api_server` 0.5.x
> 接口基址：`http://<host>:8901`（**当前部署：`http://192.168.2.195:8901`**；可用 `PORT` 环境变量覆盖）
> 描述：违建检测算法服务的 HTTP 契约。所有响应均为 JSON。

---

## 1. 服务总览

违建检测算法服务，监听 `8901` 端口（默认；可用 `PORT` 环境变量覆盖）。架构：

```
Backend / Cesium 前端
        │   HTTP（application/json）
        ▼
FastAPI（uvicorn，--workers 1）
        │   TaskStore.submit(...)
        ▼
   ┌─────────────────────────────────────────────────────┐
   │ TaskStore（进程内，N-并行 + FIFO 队列）              │
   │   max_concurrent_tasks = 4（默认；env / oss_config   │
   │   可覆盖；N=1 退化为单飞 fast-path）                 │
   │                                                     │
   │  submit() ─┬─ 有 slot ──► Popen #1  ──► reader-1    │
   │            ├─ 有 slot ──► Popen #2  ──► reader-2    │
   │            ├─ 有 slot ──► Popen #3  ──► reader-3    │
   │            ├─ 有 slot ──► Popen #4  ──► reader-4    │
   │            └─ 全占  ────► _pending_queue (FIFO)      │
   │                                  │                   │
   │                          dispatcher (daemon)         │
   │                                  │ 读完一个就拉下一个│
   └──────────────────────────────────┼──────────────────┘
                                      ▼
run_pipeline_subprocess.py  ──► <OUTPUT_BASE_DIR>/<taskId>/
                                        ├── 3DTiles/
                                        ├── instances.json
                                        ├── input.xml         （如上传了）
                                        ├── request.json      （提交快照，含原始+解析后路径；路径解析失败时不写出）
                                        ├── error.log         （仅 FAILED 时；完整 traceback）
                                        └── status.json       （sidecar）
                          默认 OUTPUT_BASE_DIR=
                          /home/zhangzhong/illegal_construction_inspection/dataset_output/tmp/
                                                    │
                                                    ▼  （同步上传）
                                              OSS / S3 客户端
                                              （scripts/service/oss_uploader.py）
                                                    │
                                                    ▼
                              oss.ikingtec.com/hushi-test/illegal-compare/<taskId>/
                                                  ├── 3DTiles/tileset.json + *.pnts
                                                  └── instance.json

算法终态时（SUCCESS/FAILED）额外主动 POST 一次后端回调 URL（保底通知，主路径仍是后端轮询）
        │
        ▼
POST http://192.168.4.20:8088/api/two-illegal-compare/tasks/callback
body: { taskId, status, progress, 3dtilesUrl, instanceJsonUrl, errorMessage }
```

健康检查：`GET /healthz` → `200 {"status":"ok"}`

OpenAPI 文档：由 FastAPI 自动生成在 `/docs`（Swagger UI）和 `/openapi.json`。

**统一响应 envelope：**

所有业务端点（`POST /two-violation/compare`、`GET /two-violation/tasks/{taskId}`）的响应均为：

```json
{ "code": 0 | 500,
  "message": "success" | "failed",
  "data": { ... } }
```

- `code: 0 / message: "success"` — 业务成功（任务已接受 / 任务状态可读）
- `code: 500 / message: "failed"` — 业务失败（提交被拒 / 任务未注册 / 任务 FAILED）

`errorMessage` 字段在 `data` 里始终存在：`null`（成功）或失败原因字符串（失败）。

> **当前部署：** 主机 `192.168.2.195`，服务通过 docker `-p 8901:8901` 暴露到 host 同一端口。响应里的 `3dtilesUrl` / `instanceJsonUrl` 是算法服务上传到 OSS 后回填的云端公共读 URL，后端可直接 `GET` 拉取，**算法服务不代理子文件下载**。OSS 配置在 `scripts/service/oss_config.json`（详见 §4.5 / OPERATIONS.md §4）。
>
> **主动回调（保底）：** 算法进入终态（SUCCESS / FAILED）后，算法服务会额外 POST 一次到后端的回调地址 `http://192.168.4.20:8088/api/two-illegal-compare/tasks/callback` —— **这是保底通知，主路径仍然是后端 GET 轮询**（见 §3）。回调丢失不影响轮询响应、不影响任务状态机；详见 §4.6。

---

## 2. 提交任务

### `POST /two-violation/compare`

请求：`Content-Type: application/json`

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `taskId` | string | ✅ | `^[A-Za-z0-9_\-\.]+$`，长度 1-128 | 任务唯一标识，必须全局唯一 |
| `baseModelPath` | string | ✅ | 绝对磁盘路径（容器内视角） | A 时相 3D Tiles 根目录 |
| `compareModelPath` | string | ✅ | 同上 | B 时相 3D Tiles 根目录 |
| `xmlFile` | string \| null | ❌ | base64 编码字符串 | 可选 XML，落到 `<out>/input.xml`；算法目前不消费 |
| `positionMode` | string \| null | ❌ | 无验证 | 坐标系标识（如 `"WGS-84"`），落到 `<out>/request.json`；算法作为信息字段使用，不参与几何换算 |
| `areaCoordinates` | list[dict] \| null | ❌ | 无验证，每个 dict 含 `{latitude, longitude, altitude}` 字段名；≥3 个顶点 | 感兴趣区 ROI 多边形顶点（WGS84）。**算法实际消费**：点云仅对 ROI 内的点做两违巡查；ROI 外的点与 cluster 全部丢弃。`radius` 当前忽略，仅存档 |
| `radius` | float \| null | ❌ | 无验证 | 半径（米），落到 `<out>/request.json`；**预留**，算法当前忽略 |

**服务层不做路径存在性 / XML 大小校验。** 路径无效时提交仍返 `code: 0`，任务在子进程中报错后轮询端点会读出 `FAILED` + `errorMessage`。

提交响应只有两种业务码：

**成功（任务已接受）：**
```json
{"code":0,"message":"success",
 "data":{"taskId":"TW20260714000001","status":"PENDING","errorMessage":null}}
```

**失败（提交被拒）：**
```json
{"code":500,"message":"failed",
 "data":{"taskId":"TW20260714000001","status":"FAILED",
         "errorMessage":"task_id already exists: TW20260714000001"}}
```

常见的 `errorMessage`：
- `task_id already exists: <id>` — 同名 `taskId` 已注册（成功跑完的也算）
- `submit failed: <reason>` — 启动子进程失败等内部异常
- `OOM: predicted peak X.X GiB > 80% of cgroup limit Y.Y GiB (B has N pts; ~5% change-ratio). Set ALGO_DBSCAN_VOXEL_M=0 to disable decimation, or run on a host with more memory.` — **v0.8+ 提交时预检拒绝**：B tileset header-only 扫描预测的 Stage 4 峰值 RSS 超过 cgroup 限额的 80%。修改 `ALGO_DBSCAN_VOXEL_M`（默认 `0.5`，设 `0.3` 更精细 / 设 `0` 关闭下采样）或扩容 cgroup 限额后重试。详见 OPERATIONS.md §7.5。
- `subprocess killed by SIGKILL (rc=-9). Most likely the cgroup OOM-killer terminated it. ... | <sidecar err>` — **v0.8+ 算法层防护失败后的清晰兜底**：子进程被 cgroup OOM-killer 杀掉，service 端识别 `rc < 0` 后给 `errorMessage` 拼上前缀。检查 `/sys/fs/cgroup/memory.events` 的 `oom_kill` 计数 + 调 `ALGO_DBSCAN_VOXEL_M`。

> **v0.7+ 变更**：服务现在同时跑 `max_concurrent_tasks`（默认 4）个任务；超过 N 的提交**不会**被拒，而是以 `code:0 / status:PENDING / step:waiting` 入队 FIFO 等 slot。前 N 个完工后 dispatcher 自动拉起。详见 §3 状态机。

> 注：若请求体字段缺失 / `taskId` 格式不合法，FastAPI 仍会返 HTTP 422（body 为 `{"detail": [...]}`）。这是 Pydantic 模型校验，不是业务码；调用方在解析前应先检查 HTTP 状态。

### curl 提交示例

**只传路径（不带 XML）：**
```bash
curl -X POST http://192.168.2.195:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "taskId":"TW20260714000001",
    "baseModelPath":"/home/zhangzhong/illegal_construction_inspection/dataset/wuxi_251022",
    "compareModelPath":"/home/zhangzhong/illegal_construction_inspection/dataset/wuxi_260205"
  }'
```

**带 base64 XML：**
```bash
B64=$(base64 -w0 /path/to/input.xml)
curl -X POST http://192.168.2.195:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d "{\"taskId\":\"TW20260714000001\",
       \"baseModelPath\":\"/.../dataset/wuxi_251022\",
       \"compareModelPath\":\"/.../dataset/wuxi_260205\",
       \"xmlFile\":\"$B64\"}"
```

**带可选元数据（positionMode / areaCoordinates / radius）：**

```bash
curl -X POST http://192.168.2.195:8901/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "taskId":"TW20260722000001",
    "baseModelPath":"/.../dataset/wuxi_251022",
    "compareModelPath":"/.../dataset/wuxi_260205",
    "positionMode":"WGS-84",
    "areaCoordinates":[
      {"altitude":13.51,"latitude":31.4912779,"longitude":121.0935673},
      {"altitude":13.64,"latitude":31.4911645,"longitude":121.0936537}
    ],
    "radius":500
  }'
```

提交后 `<OUTPUT_BASE_DIR>/TW20260722000001/request.json` 内容为：

```json
{
  "taskId": "TW20260722000001",
  "submittedAt": "2026-07-21T08:35:16+00:00",
  "baseModelPath": "/abc/def_v1",
  "compareModelPath": "/abc/def_v2",
  "baseModelPathResolved": "/model/def_v1/tileset.json",
  "compareModelPathResolved": "/model/def_v2/tileset.json",
  "positionMode": "WGS-84",
  "areaCoordinates": [
    {"altitude": 13.51, "latitude": 31.4912779, "longitude": 121.0935673},
    {"altitude": 13.64, "latitude": 31.4911645, "longitude": 121.0936537}
  ],
  "radius": 500.0,
  "xmlPath": null
}
```

字段说明：

- `submittedAt` — UTC ISO8601 时间戳（带 `+00:00` 后缀）
- `baseModelPath` / `compareModelPath` — 后端提交时的原始路径（可能是含前缀的虚拟路径，如 `/abc/def_v1`）
- `baseModelPathResolved` / `compareModelPathResolved` — 经 `resolve_model_path` 映射后的本地 `/model/<dir>` 路径
- `areaCoordinates` 被算法实际消费（ROI 过滤）；`positionMode` 仅信息字段；`radius` 是预留参数
- `request.json` **仅在路径解析成功后写出**；如果 `resolve_model_path` 抛错则跳过——这种场景下 `errorMessage` 已经透传给调用方

三个 Optional 字段不传时 `request.json` 里对应值为 `null`。

---

## 3. 轮询任务

### `GET /two-violation/tasks/{taskId}`

任意状态都返 `200`，业务码在 body `code` 字段。taskId 不存在时**不**返 HTTP 404，而是返 `code: 500` + `status: "FAILED"` + `errorMessage`。

### 响应字段（`data` 里固定 7 项）

| 字段 | 类型 | 说明 |
|---|---|---|
| `taskId` | string | 同提交 |
| `progress` | string | `"0"`～`"100"` 的字符串 |
| `status` | enum | `PENDING` / `RUNNING` / `SUCCESS` / `FAILED`（详见下表） |
| `step` | string | 当前阶段名（见下表） |
| `3dtilesUrl` | list \| null | SUCCESS 时为 OSS 上 3D Tiles 根 URL 列表（v1 单元素，v2 多元素；RUNNING 时也可能部分填入已上传的 chunk）；**FAILED 时也可能非空**（部分 chunk 已上传时回填，给前端渐进显示用）；否则 `null` |
| `instanceJsonUrl` | string \| null | SUCCESS 时为 OSS 上 `instances.json` 的公共读 URL；FAILED 时若已部分上传也会填；否则 `null` |
| `errorMessage` | string \| null | FAILED 时填失败原因；其余为 `null`。**最长 500 字符**（超长会截断为 `前 400 字 + 截断标记 + 后 80 字`）。完整 Python traceback 见服务容器内 `<OUTPUT_BASE_DIR>/<taskId>/error.log`（本服务内部,需要 SSH 进容器查看；后端拿不到) |

### 状态机（对外口径）

| `status` | `step` | 含义 | 何时出现 | 业务码 |
|---|---|---|---|---|
| `PENDING` | `waiting` | 提交已受理；v0.7+ 还可能表示"在 FIFO 队尾等 slot"（`max_concurrent_tasks` 占满时） | 提交响应 + 轮询响应（队尾时） | `code: 0` |
| `RUNNING` | `point cloud extraction` / `vegetation filtering` / `change detection` / `3d tiles generation` / `finalizing` | 子进程在跑 | 轮询响应 | `code: 0` |
| `SUCCESS` | `completed` | 子进程退出 0，产物可用 | 轮询响应 | `code: 0` |
| `FAILED` | `failed` / 上一个 step | 子进程退出非 0 / 抛异常 / taskId 不存在 | 轮询响应 | `code: 500` |

> **PENDING 含义（v0.7+）**：可能是 (a) 提交瞬间子进程尚未启动（极短，仅提交响应里看到），或 (b) 已入 FIFO 队尾等空闲 slot（轮询里也能看到，秒到分钟级——取决于队前任务剩余时间）。后端可以照常轮询；不要把长期 PENDING 视为"丢了"。

```
submit (code:0, status:PENDING)
        │
        ▼ (有 slot → 立刻 spawn;无 slot → 入队)
RUNNING (code:0) ──exit 0──▶ SUCCESS (code:0, 3dtilesUrl / instanceJsonUrl 可用)
       │
       └── exit≠0 / 异常 ──▶ FAILED (code:500, errorMessage 含原因)
                │
                ▼
   (slot 释放) ──► dispatcher 拉起下一个 PENDING ──► RUNNING
```

### `step` 与 `progress` 对应

| 阶段（内部 stage 名）| 对外 `step` | `progress` 区间 |
|---|---|---|
| `extract_leaf_vertices` | `point cloud extraction` | 0 → 30 |
| `filter_vegetation` | `vegetation filtering` | 30 → 55 |
| `nn_change_analysis` | `change detection` | 55 → 80 |
| `convert_point_ecef_and_3dtiles` | `3d tiles generation` | 80 → 95 |
| （终态 summary） | `finalizing` | 95 → 100 |
| 终态 | `completed` | 100 |

### 轮询示例

**RUNNING：**
```bash
$ curl -s http://192.168.2.195:8901/two-violation/tasks/TW20260714000001
{"code":0,"message":"success",
 "data":{"taskId":"TW20260714000001","progress":"30","status":"RUNNING",
         "step":"vegetation filtering",
         "3dtilesUrl":null,"instanceJsonUrl":null,"errorMessage":null}}
```

**SUCCESS：**
```bash
$ curl -s http://192.168.2.195:8901/two-violation/tasks/TW20260714000001
{"code":0,"message":"success",
 "data":{"taskId":"TW20260714000001","progress":"100","status":"SUCCESS","step":"completed",
         "3dtilesUrl":["https://oss.ikingtec.com/hushi-test/illegal-compare/TW20260714000001/3DTiles/tileset.json"],
         "instanceJsonUrl":"https://oss.ikingtec.com/hushi-test/illegal-compare/TW20260714000001/instance.json",
         "errorMessage":null}}
```

**FAILED（子进程报错）：**
```bash
$ curl -s http://192.168.2.195:8901/two-violation/tasks/TW-BAD-PATH
{"code":500,"message":"failed",
 "data":{"taskId":"TW-BAD-PATH","progress":"0","status":"FAILED","step":"waiting",
         "3dtilesUrl":null,"instanceJsonUrl":null,
         "errorMessage":"taskId not found: TW-BAD-PATH"}}
```

**FAILED（taskId 未注册）：**
```bash
$ curl -s http://192.168.2.195:8901/two-violation/tasks/TW-NOT-EXIST
{"code":500,"message":"failed",
 "data":{"taskId":"TW-NOT-EXIST","progress":"0","status":"FAILED","step":"waiting",
         "3dtilesUrl":null,"instanceJsonUrl":null,
         "errorMessage":"taskId not found: TW-NOT-EXIST"}}
```

**推荐轮询频率**：每 5-15 秒一次。`progress` 字段在每个 stage 切换时会跳变，不需要紧密轮询。

> 注：进程内任务表，重启后已结束的 `taskId` 也算"不存在"，需要重新提交。

---

## 4. 下载产物

算法服务**不再代理** 3D Tiles 子文件与 `instances.json` 的下载。轮询响应里的 `3dtilesUrl` / `instanceJsonUrl` 是**OSS 公共读 URL**（不是算法服务的 `/two-violation/...` 路径），后端拿到后自行从云端拉取，下载逻辑（如 404 重试 / 鉴权 / 增量）由后端负责。

算法服务的 `/healthz` 之外只有两个业务端点：

- `POST /two-violation/compare`
- `GET  /two-violation/tasks/{taskId}`

旧路由 `GET /two-violation/tasks/{taskId}/3dtiles/{file_path:path}` 和 `GET /two-violation/tasks/{taskId}/instance` 已删除，调用会返 404。

### 输出 URL 形态

`3dtilesUrl` 与 `instanceJsonUrl` 是算法服务**同步上传到 OSS 后回填的字符串**。OSS 配置在 `scripts/service/oss_config.json`：

- `public_base` = `https://oss.ikingtec.com/hushi-test`（公共读 URL 前缀）
- `key_prefix`  = `illegal-compare`（桶内路径前缀）
- `endpoint`    = `http://10.230.0.5:8009`（boto3 SDK 用的内部端点；容器内能解析）

**URL 路径约定**（v1 单 chunk）：

```
3dtilesUrl[0]      = https://oss.ikingtec.com/hushi-test/illegal-compare/<taskId>/3DTiles/tileset.json
instanceJsonUrl    = https://oss.ikingtec.com/hushi-test/illegal-compare/<taskId>/instance.json
```

**v2 多 chunk 时**（见 §10）：

```
3dtilesUrl[i]      = https://oss.ikingtec.com/hushi-test/illegal-compare/<taskId>/3DTiles/<chunk_subdir>/tileset.json
```

URL 一旦出现在响应里就被认为可下载——文件是否真的可用、是 404 还是 401，由后端去云端拉取时自行判断。

如果后端走的是**私有网络**（容器内 / VPN 内部），`oss.ikingtec.com` 域名可能解析不到——这种情况下后端可用 OSS 的**内部网关**端点（`http://10.230.0.5:8009`）替代 `https://oss.ikingtec.com`，路径不变：

```
http://10.230.0.5:8009/hushi-test/illegal-compare/<taskId>/3DTiles/tileset.json
```

### 4.5 OSS 上传时机

- **同步上传**：算法子进程退出码 0 后，**算法服务**（在 reader 线程里）串行上传所有 3D Tiles chunks 与 `instances.json`，**全部传完才置 `SUCCESS`**；上传期间 `status` 仍为 `RUNNING`。
- **上传失败** → 整个 task 标 `FAILED`，`errorMessage` 含异常。
- **上传范围**：`<out>/<taskId>/3DTiles/` 整棵子树（**排除 `3DTiles/tmp/`** 算法中间产物） + `instances.json` 单文件。`input.xml` / `status.json` 不上传。
- **上传粒度**：4 worker 并行；约 260 个文件 / 20 MB 的 task 实测 5–15 秒传完。
- **预留渐进式钩子**：`task_manager.TaskStore.append_3dtiles_chunk(task_id, local_path)` 在 v2 算法每个 chunk 写完时被调时，会**立刻同步上传该 chunk**，URL 立即出现在下一次轮询响应里（即使 `status` 还是 `RUNNING`）；上传失败仍会标 FAILED。

### 4.6 终态回调（保底通知）

主路径仍是后端 GET 轮询（见 §3），但算法服务在状态进入终态（`SUCCESS` / `FAILED`）后，会**额外**主动 POST 一次后端回调地址，作为**保底通知**——避免后端轮询进程宕掉、轮询频率过低、或错过窗口时收不到通知。

#### 4.6.1 触发时机

| 事件 | 触发回调？ | status | 备注 |
|---|---|---|---|
| 算法退出 rc=0，OSS 上传完成 | ✅ | `SUCCESS` | payload 含完整 URLs |
| 算法退出 rc≠0 / 抛异常 | ✅ | `FAILED` | payload 含 `errorMessage`；若部分 chunk 已落盘 OSS，回填到 `3dtilesUrl`（渐进显示） |
| RUNNING 中 | ❌ | — | progress 通过 GET 拉 |

#### 4.6.2 接口契约

- **URL**：`POST http://192.168.4.20:8088/api/two-illegal-compare/tasks/callback`（在 `oss_config.json` 的 `backend_callback_url` 配置；空字符串 = 禁用回调）
- **Content-Type**：`application/json`
- **Body schema**：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `taskId` | string | 同提交 |
  | `status` | enum | **只 `SUCCESS` 或 `FAILED`**；不带 RUNNING/PENDING |
  | `progress` | string | `"0"`～`"100"`；SUCCESS 时 `"100"`，FAILED 时为失败时的进度 |
  | `3dtilesUrl` | list \| null | 已上传到 OSS 的 chunk URL 列表（按算法产出顺序）；全无则 `null`；FAILED 时也可能非空（partial） |
  | `instanceJsonUrl` | string \| null | instances.json 已上传 OSS 后的 URL；否则 `null` |
  | `errorMessage` | string \| null | FAILED 时填失败原因；SUCCESS 时 `null` |

- **回调示例**：

  ```json
  // SUCCESS
  {
    "taskId": "20260715103045A1B2C3",
    "status": "SUCCESS",
    "progress": "100",
    "3dtilesUrl": [
      "https://oss.ikingtec.com/hushi-test/illegal-compare/20260715103045A1B2C3/dongshan1/3DTiles/tileset.json",
      "https://oss.ikingtec.com/hushi-test/illegal-compare/20260715103045A1B2C3/dongshan2/3DTiles/tileset.json"
    ],
    "instanceJsonUrl": "https://oss.ikingtec.com/hushi-test/illegal-compare/20260715103045A1B2C3/instances.json",
    "errorMessage": null
  }

  // FAILED
  {
    "taskId": "20260715103045A1B2C3",
    "status": "FAILED",
    "progress": "20",
    "3dtilesUrl": null,
    "instanceJsonUrl": null,
    "errorMessage": "XML文件解析失败"
  }
  ```

- **期望响应**：HTTP 2xx。算法服务对非 2xx 视为失败并重试。

#### 4.6.3 重试 & 超时

| 项 | 默认值 | 配置字段 |
|---|---|---|
| 单次超时 | 10s | `callback_timeout_seconds` |
| 最大重试次数 | 3 | `callback_max_retries` |
| 退避 | 指数 2s/4s/8s（封顶 30s） | 硬编码 |

3 次都失败 → 记 `ERROR` 日志（`callback failed after N attempts`），任务状态保持 `SUCCESS`/`FAILED` 不变。**后端必须容忍回调丢失**——通过 GET 轮询端点自愈（轮询永远返回当前真实状态）。

#### 4.6.4 稳定性保证

- 回调在独立 daemon 线程里跑，与 reader 线程 / 主服务循环解耦
- 回调失败/超时**绝不阻塞**新任务提交：状态先置终态，再异步 POST
- 服务重启时 daemon 线程不会等回调完成 → 当前在跑的回调可能丢失；后端靠 GET 轮询兜底
- 与 §4.5 OSS 上传的关系：OSS 同步上传完成（且 `instances.json` 上传完成）→ 状态置终态 → 触发回调。回调和 OSS 上传互不阻塞

#### 4.6.5 后端建议

- **幂等处理**：同一 `taskId` 可能因重试收到多次回调；用 `taskId` 做 dedup key
- **超时容忍**：不要把 callback handler 写太重；回调要求秒级响应（≤10s）
- **失败可恢复**：即使全程没收到回调，下一次 `GET /two-violation/tasks/{taskId}` 仍能拿到完整状态 + URLs

#### 4.6.6 关闭回调

把 `oss_config.json` 的 `backend_callback_url` 设为空字符串（`""`），重启服务即可。轮询端点行为完全不变。

---

## 5. 错误码总表

### 业务端点（`POST /compare`、`GET /tasks/{id}`）

| `code` | `message` | 触发场景 |
|---|---|---|
| `0` | `"success"` | 提交成功 / 任务可读（任意 RUNNING/SUCCESS/PENDING 内部态） |
| `500` | `"failed"` | 提交被拒（重复 taskId / 已在跑 / 异常）/ 任务失败 / 任务未注册 |

### Pydantic 校验（HTTP 层，非业务码）

| HTTP | body | 触发场景 |
|---|---|---|
| 422 | `{"detail":[...]}` | `taskId` 格式错 / 字段缺失 / 字段类型错 |

> HTTP 422 走 FastAPI 默认 Pydantic 校验，与业务的 `code: 500` 不同；调用方按 HTTP 状态码分流即可。

---

## 6. 端到端示例

```bash
SERVICE=http://192.168.2.195:8901
TID=TW-DEMO-$(date +%s)
A=/home/zhangzhong/illegal_construction_inspection/dataset/wuxi_251022
B=/home/zhangzhong/illegal_construction_inspection/dataset/wuxi_260205

# 1) healthz
curl -s $SERVICE/healthz
# 期望: {"status":"ok"}

# 2) 提交
curl -s -X POST $SERVICE/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d "{\"taskId\":\"$TID\",\"baseModelPath\":\"$A\",\"compareModelPath\":\"$B\"}"
# 期望: {"code":0,"message":"success",
#         "data":{"taskId":"$TID","status":"PENDING","errorMessage":null}}

# 3) 轮询到终态（超时 30 分钟）
for i in {1..120}; do
  s=$(curl -s $SERVICE/two-violation/tasks/$TID \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['status'])")
  echo "[$i] status=$s"
  case "$s" in SUCCESS|FAILED) break;; esac
  sleep 15
done

# 4) 取到产物 URL（从响应 data 里读）
curl -s $SERVICE/two-violation/tasks/$TID \
  | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print('tileset:',d['3dtilesUrl']);print('instances:',d['instanceJsonUrl'])"

# 5) 用产物 URL 去云端拉
curl -s "$(curl -s $SERVICE/two-violation/tasks/$TID \
   | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['instanceJsonUrl'])")" \
   | python3 -c "import sys,json;d=json.load(sys.stdin);print('clusters:',d['n_clusters'])"
```

---

## 7. Cesium 前端集成要点

```javascript
const API = "http://192.168.2.195:8901";

// 1) 提交
await fetch(`${API}/two-violation/compare`, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    taskId: "TW20260714000001",
    baseModelPath: "/.../dataset/wuxi_251022",
    compareModelPath: "/.../dataset/wuxi_260205",
    xmlFile: null,                       // 或 base64 字符串
    // --- 以下三项 Optional，v0.6 起支持 ---
    positionMode: "WGS-84",              // string | null
    areaCoordinates: [                   // [{latitude, longitude, altitude}, ...] | null
      {latitude: 31.4912779, longitude: 121.0935673, altitude: 13.51},
    ],
    radius: 500,                         // number | null
  }),
});

// 2) 轮询
const poll = async () => {
  const r = await fetch(`${API}/two-violation/tasks/TW20260714000001`).then(r => r.json());
  if (r.code !== 0) throw new Error(r.data.errorMessage || r.message);
  return r.data;                          // { taskId, progress, status, step, 3dtilesUrl, instanceJsonUrl, errorMessage }
};

// 3) 拿到产物 URL 后，直接喂给 Cesium（URL 是 OSS 公共读地址，浏览器会按相对路径自动拉子文件）
const data = await poll();                // 等到 status === "SUCCESS"
const tilesetUrl = data["3dtilesUrl"]?.[0]; // null-safe; v1 单元素；v2 多元素时按顺序逐块喂给不同 viewer
const instanceUrl = data["instanceJsonUrl"];
if (!tilesetUrl) {
  throw new Error(`no tiles URL (status=${data.status}, err=${data.errorMessage})`);
}
const tileset = await Cesium.Cesium3DTileset.fromUrl(tilesetUrl);
viewer.scene.primitives.add(tileset);
const instances = await fetch(instanceUrl).then(r => r.json());
```

`3dtilesUrl` / `instanceJsonUrl` 是 OSS 公共读 URL，**算法服务不再代理子文件**——Cesium 浏览器按 `tileset.json` 里的相对路径去 OSS 拉 `r.pnts` / `Tile_x/...`（同 bucket、同 prefix，相对引用直接命中）。

> 终态回调到达时 payload 与上述 `poll()` 返回的 `data` 同构（去掉 `progress` / `step` 两个 RUNNING 字段）。后端可以共用一套渲染代码。

---

## 8. 不在契约里的事

以下行为刻意未约定 / 暂不支持：

- 同一 `taskId` 复用（提交同 taskId 永远 500 + "task_id already exists"）
- 取消任务（要停就 `pkill` 子进程；cancel endpoint 暂无）
- 鉴权 / 跨域 CORS（默认同源；若部署跨域请在反代层处理）
- 进度主动推送（WebSocket / SSE）—— progress 仍走 GET 拉；终态（SUCCESS / FAILED）有保底回调（见 §4.6），但 RUNNING 中无中间推送
- 自动 presigned URL（当前 bucket 是公共读；如要切到私有 bucket 改 `oss_config.json` 的 `use_presigned: true`，URL 会带 5 天 `?X-Amz-...` 签名）
- v2 多 chunk 渐进式 IPC（算法子进程主动通知父进程有新 chunk）——v2 算法未实现

> 多任务并行 **已实现**（v0.7+），由 `max_concurrent_tasks` 控制（默认 4；详见 §3 状态机）。超出 N 的提交以 `PENDING` 入队 FIFO，不再 500 拒接。

需要任一项时再迭代。

---

## 9. 当前部署与跨机器访问

### 9.1 当前部署形态（2026-07 起）

| 项 | 值 |
|---|---|
| 服务地址 | `http://192.168.2.195:8901` |
| Docker 容器 | `shj-work-test-20260123-10-8900`（container id `bbf9d7f7415b`） |
| 镜像 | `hub.ikingtec.com/odm/iking_shanhaijing_20.04:1.0.10` |
| 容器内服务端口 | 8901（`uvicorn --port 8901`） |
| 端口映射 | `host:8900→22/tcp`（SSH）；`host:8901-8903→container:8901-8903`（当前用 8901） |
| 数据集 | `/home/zhangzhong/illegal_construction_inspection/dataset/{wuxi_251022,wuxi_260205,...}` |
| 卷挂载 | `/mnt/work_odm:/mnt/work_odm`；算法代码在 `/code`，服务代码在 `/app/shanhaijing-api` |
| OSS endpoint（SDK） | `http://10.230.0.5:8009`（容器内能解析；boto3 走这个） |
| OSS public base（响应 URL） | `https://oss.ikingtec.com/hushi-test`（后端用这个拉数据） |
| OSS bucket | `hushi-test` |
| OSS key prefix | `illegal-compare/<taskId>/...` |
| OSS 配置文件 | `scripts/service/oss_config.json`（覆盖用 `OSS_CONFIG` 环境变量） |
| 后端回调 URL（保底） | `http://192.168.4.20:8088/api/two-illegal-compare/tasks/callback` |
| 回调超时 / 重试 | `callback_timeout_seconds=10`、`callback_max_retries=3`（均在 `oss_config.json`） |
| 算法后处理主开关 | `ALGO_VIOLATION_MODE=on`（默认） / `off`（见 §9.6） |

### 9.2 SSH 接入

`~/.ssh/config`：
```
Host 张仲开发195
    HostName 192.168.2.195
    Port 8900
    User root
```

注意：**SSH 端口是 8900**，**算法服务端口是 8901**，两个不一样（host 上不能共用同一个端口）。

### 9.3 跨机器访问的常见坑

1. **不要凭印象写 `-p`**，先用 `docker ps` 看 `PORTS` 列确认实际映射。例：
   ```
   0.0.0.0:8900->22/tcp, [::]:8900->22/tcp
   0.0.0.0:8901-8903->8901-8903/tcp, [::]:8901-8903->8901-8903/tcp
   ```
   一眼能看出 SSH 在 8900，算法在 8901-8903。

2. **服务端口和 SSH 端口不能共用 host 同一端口**——docker 也不支持给运行中的容器热加端口（必须 stop+run）。

3. **服务实际在容器内的哪个端口、host 映到哪个端口**，以 `docker inspect` 为准：
   ```bash
   docker inspect <容器名> --format '{{json .HostConfig.PortBindings}}' | python3 -m json.tool
   ```

4. **`baseModelPath` / `compareModelPath` 是容器内视角的绝对路径**——不要写 host 上的 `/mnt/...`，要写容器内能看到的路径。当前部署的 dataset 在容器内就是 `/home/zhangzhong/illegal_construction_inspection/dataset/...`。

5. **`PUBLIC_BASE_URL` 已废弃**（v0.5 起）：响应里的 `3dtilesUrl` / `instanceJsonUrl` 是 OSS 公共读 URL，由 `oss_config.json` 的 `public_base` 决定，**不再由环境变量控制**。启动命令参考：
   ```bash
   # 仅需 PORT / OUTPUT_BASE_DIR / LOG_LEVEL；OSS 配置在 oss_config.json
   nohup ... python -m uvicorn scripts.service.api_server:app \
       --host 0.0.0.0 --port 8901 --workers 1 > /tmp/uvicorn.log 2>&1 &
   ```

6. **host 防火墙**（`sudo iptables -L INPUT -n`）要放行服务端口，否则外网打不通。`sudo iptables -I INPUT -p tcp --dport 8901 -j ACCEPT`。

### 9.4 Windows 客户端踩坑速查

| 现象 | 原因 | 修法 |
|---|---|---|
| `curl` 弹「脚本执行风险」确认 | PowerShell 把 `curl` 别名到 `Invoke-WebRequest` | 用 `curl.exe` |
| `Invoke-WebRequest : 找不到与参数名称"X"匹配的参数` | 同上，`-X -F -d` 是 curl 语法不是 PowerShell cmdlet 参数 | 用 `curl.exe` |
| `Invoke-WebRequest : 缺少与"Content-Type"对应的参数` 等 | PowerShell 把 JSON 体当 hash table 解析 | 用 `curl.exe`，body 用 `'...'` 单引号 |
| Windows 多行命令用 `\` 报错 | PowerShell 行续是反引号 `` ` `` | 改用 `` ` ``（或把整条命令写一行） |

### 9.5 端到端联调最小验证脚本

从任意能访问 `192.168.2.195:8901` 的机器跑一遍（Linux/macOS/WSL 适用；Windows 把 `curl` 全换成 `curl.exe`，行续用 `` ` ``）：

```bash
SERVICE=http://192.168.2.195:8901
TID=TW-DEMO-$(date +%s)
A=/home/zhangzhong/illegal_construction_inspection/dataset/wuxi_251022
B=/home/zhangzhong/illegal_construction_inspection/dataset/wuxi_260205

# 1) healthz
curl -s $SERVICE/healthz
# 期望: {"status":"ok"}

# 2) 提交
curl -s -X POST $SERVICE/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d "{\"taskId\":\"$TID\",\"baseModelPath\":\"$A\",\"compareModelPath\":\"$B\"}"
# 期望: {"code":0,"message":"success",
#         "data":{"taskId":"$TID","status":"PENDING","errorMessage":null}}

# 3) 轮询到 SUCCESS 或 FAILED
for i in {1..120}; do
  s=$(curl -s $SERVICE/two-violation/tasks/$TID \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['status'])")
  echo "[$i] status=$s"
  case "$s" in SUCCESS|FAILED) break;; esac
  sleep 15
done

# 4) 失败场景 1：重复 taskId（应 code:500 + "task_id already exists"）
curl -s -X POST $SERVICE/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d "{\"taskId\":\"$TID\",\"baseModelPath\":\"$A\",\"compareModelPath\":\"$B\"}"

# 5) 失败场景 2：路径无效（提交仍 code:0；轮询会变 FAILED）
curl -s -X POST $SERVICE/two-violation/compare \
  -H 'Content-Type: application/json' \
  -d "{\"taskId\":\"TW-BAD\",\"baseModelPath\":\"/no/such/path\",\"compareModelPath\":\"$B\"}"
curl -s $SERVICE/two-violation/tasks/TW-BAD

# 6) 失败场景 3：taskId 不存在
curl -s $SERVICE/two-violation/tasks/TW-NOT-EXIST
# 期望: code:500, status:FAILED, errorMessage: "taskId not found: TW-NOT-EXIST"

# 7) 失败场景 4：旧下载端点不存在
curl -s -o /dev/null -w '%{http_code}\n' \
  $SERVICE/two-violation/tasks/$TID/3dtiles/tileset.json
curl -s -o /dev/null -w '%{http_code}\n' \
  $SERVICE/two-violation/tasks/$TID/instance
# 期望: 404 / 404
```

### 9.6 算法后处理主开关（`ALGO_VIOLATION_MODE`）

> **部署级配置**，不是请求字段。该 env var 在 uvicorn 启动时（容器层）读取；同一容器内的所有任务共享同一模式。

| 取值 | 含义 | 接受写法 |
|---|---|---|
| `on`（默认） | 模式 A：DBSCAN 之后按高度区间（`HAG_MAX_LOW_M` / `HAG_MIN_HIGH_M`）硬过滤中间层杂簇，再按 Gaussian 置信度（`CONFIDENCE_PEAK_N` / `CONFIDENCE_SIGMA_N`）排序保留的簇 | `on` / `1` / `true` / `yes` / `y`（大小写不敏感） |
| `off` | 模式 B：legacy 行为——不剔除任何簇，直接按 `num_points` 从大到小排序 | `off` 及任何不被识别为 `on` 的字符串（含 `""`、`0`、`false`、`no`、乱写） |

**对 `instances.json` 的影响**：

| 字段 | 模式 A（`on`） | 模式 B（`off`） |
|---|---|---|
| `height_filter_enabled` | `true` | `false` |
| `hag_max_low_m` / `hag_min_high_m` / `confidence_peak_n` / `confidence_sigma_n` | 数值 | `null` |
| `n_clusters_before_height_filter` | 过滤前的簇数（通常 `> n_clusters`） | `null` |
| `n_clusters` | 过滤后的簇数（`≤` 前者） | 等于过滤前（无任何剔除） |
| 簇对象里的 `scenario` / `confidence_score` / `hag_min` / `hag_max` / `hag_mean` / `passed_height_filter` / `dbscan_label` | 存在 | **不存在**（与早期 legacy 输出同构） |

`dtm_ground_count` / `dtm_quality` 在两种模式下**都正常出现**（是 Stage 2 的标量地面估计质量诊断，跟过滤模式无关）。

**对后端 / 前端的影响**：

- **后端不感知模式**：依旧只转发 `3dtilesUrl` / `instanceJsonUrl`；后端不需要写分支代码。
- **前端 Cesium 已兼容**：`scripts/visualization/cesium.html` 只读 `id` / `bbox_*` / `hull_*` / `num_points`（legacy 字段）；新字段(`scenario` / `confidence_score` 等)即便存在也不消费。模式 B 时这些字段缺失不会报错。

**运维用法**：

```bash
# 临时对单次任务切到 legacy 排序（在该次提交的子进程里临时注入）
ALGO_VIOLATION_MODE=off nohup ... python -m uvicorn scripts.service.api_server:app ...

# 改默认（影响所有不显式设置环境变量的运行；改动 algo_config.py L108）
# 推荐改 shell 环境而非代码默认：
echo 'export ALGO_VIOLATION_MODE=off' >> ~/.bashrc
```

详细设计与取舍见代码 PR 描述 / plan 文件 `~/.claude/plans/.../radiant-wilkes.md`。

---

## 10. 多 chunk 渐进式接口（v2 预留）

> **当前状态：算法实现尚未多 chunk 化，但接口已预留。** 本节描述将来 v2 算法接入时怎么用，**v1 算法不调用、不需要关心**。

### 10.1 动机

未来可能把一个模型（特别是大场景）拆成多个 chunk 跑：算法先把第一个 chunk 跑完、上传云端，回前端一个 URL；前端先看这一块；与此同时算法继续跑第二个 chunk。这样前端能渐进式显示差异，而不是等整个模型跑完才看到任何东西。

### 10.2 算法侧的钩子

`task_manager.TaskStore.append_3dtiles_chunk(task_id, local_path)`：

```python
from scripts.service.task_manager import TaskStore
store.append_3dtiles_chunk("TW-V2", "<OUTPUT_BASE_DIR>/TW-V2/3DTiles/chunk_0")
store.append_3dtiles_chunk("TW-V2", "<OUTPUT_BASE_DIR>/TW-V2/3DTiles/chunk_1")
```

每次调用会把一个本地路径追加到该任务的状态里 `three_dtiles_paths: list[str]`。

约束：
- 同一 taskId 同一 path 重复调用是幂等的（不会重复加入）
- 调用时不需要 chunk 文件真的存在——只是登记一个"路径"，什么时候上传云端由算法/上传流水线自己决定
- `instances_path` 也类似：v2 算法可以在每个 chunk 跑完后设置一个 partial instances.json，或等全部跑完后设置最终的

### 10.3 何时调用

v2 算法的子进程脚本（`run_pipeline_subprocess.py` 或其下游 `algorithm/run_pipeline.py`）在每个 chunk 写完 3DTiles 目录后调一次 `append_3dtiles_chunk`。可以是：
- 在 stage 4（`convert_point_ecef_and_3dtiles`）内每个 chunk 写完后
- 或 stage 4 整体完成后一次性调多次

### 10.4 服务端行为

- `append_3dtiles_chunk` 被调后，**立刻同步上传**该 chunk 到 OSS，并把 URL 写到 `status.oss_chunk_urls[local_path]`；下一次 GET `/tasks/{id}` 的响应里 `3dtilesUrl` 立刻包含这个 chunk 的 OSS URL（前端无需重新提交；**`status` 此时仍可能是 `RUNNING`**）
- 子进程退出码为 0 时，`_finalize_success` **不会重复上传**已经累加好的 chunks（`oss_chunk_urls` 是去重键），且 `instances.json` 也会同步上传后才置 `SUCCESS`
- `3dtilesUrl` 的元素顺序就是调用 `append_3dtiles_chunk` 的顺序；调用方按顺序逐块喂给前端
- 任何 chunk 的上传失败都会把整个 task 标 `FAILED`，`errorMessage` 含 OSS 异常信息

### 10.5 现在的状态

- 接口已实现并保留：`scripts/service/task_manager.py:append_3dtiles_chunk`
- v1 算法未调用该接口——v1 单 chunk 走 `_finalize_success` 兜底逻辑（用 `<out>/3DTiles` 作为唯一 chunk）
- 前端目前看不到该接口效果（v1 算法永远只一个 chunk）