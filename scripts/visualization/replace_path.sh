#!/usr/bin/env bash
# replace_path.sh <task_id>
#
# 替换 scripts/visualization/cesium.html 中:
#   - 参考模型 (epoch A, mesh)        → request.json baseModelPathResolved
#   - 对比模型 (epoch B, mesh)        → request.json compareModelPathResolved
#   - 算法结果点云 (change / 3DTiles) → output/<task_id>/3DTiles/tileset.json
#   - 聚类结果 (instances.json)       → output/<task_id>/instances.json
#
# 这 4 个路径都是绝对路径(以 / 开头),配合 Live Server serve root=/ 使用。
# 不会改 cesium.html 的其他任何内容。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── 参数校验 ──
if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <task_id>" >&2
    echo "  task_id: output/ 下的子目录名,例如 20260811170311EF8936" >&2
    exit 1
fi
TASK_ID="$1"

HTML="$SCRIPT_DIR/cesium.html"
REQ="$REPO_ROOT/output/$TASK_ID/request.json"
CHANGE="$REPO_ROOT/output/$TASK_ID/3DTiles/tileset.json"
INSTANCES="$REPO_ROOT/output/$TASK_ID/instances.json"

[[ -f "$HTML" ]]      || { echo "FATAL: $HTML 不存在" >&2; exit 1; }
[[ -f "$REQ" ]]       || { echo "FATAL: $REQ 不存在 — 任务 $TASK_ID 未跑过" >&2; exit 1; }
[[ -f "$CHANGE" ]]    || { echo "FATAL: $CHANGE 不存在" >&2; exit 1; }
[[ -f "$INSTANCES" ]] || { echo "FATAL: $INSTANCES 不存在" >&2; exit 1; }

# ── 从 request.json 取 base / compare 的绝对路径 ──
PYTHON_BIN="${PYTHON:-/root/miniconda3/envs/illegal_construction_inspection/bin/python}"
read -r BASE_ABS COMPARE_ABS < <(
    "$PYTHON_BIN" - "$REQ" <<'PY'
import json, sys
req = json.load(open(sys.argv[1]))
def coalesce(*vals):
    for v in vals:
        if v is not None and v != "":
            return v
    return ""
print(
    coalesce(req.get("baseModelPathResolved"), req.get("baseModelPath")),
    coalesce(req.get("compareModelPathResolved"), req.get("compareModelPath")),
)
PY
)
[[ -n "$BASE_ABS" ]]    || { echo "FATAL: request.json 缺 baseModelPathResolved" >&2; exit 1; }
[[ -n "$COMPARE_ABS" ]] || { echo "FATAL: request.json 缺 compareModelPathResolved" >&2; exit 1; }

# ── 用 Python os.path.relpath 把 4 个绝对路径转成 cesium.html 视角的相对路径 ──
# 不再手动数 ../ —— 任何深度都自动算正确,URL 永远用 / 分隔符。
mapfile -t REL < <(
    "$PYTHON_BIN" - "$SCRIPT_DIR" \
        "$BASE_ABS/tileset.json" \
        "$COMPARE_ABS/tileset.json" \
        "$CHANGE" \
        "$INSTANCES" <<'PY'
import os, sys
html_dir = os.path.abspath(sys.argv[1])
for p in sys.argv[2:]:
    rel = os.path.relpath(os.path.abspath(p), html_dir)
    # 浏览器/URL 永远用正斜杠,即便在 Windows 上跑也能正确生成 ../../../../model/...
    print(rel.replace(os.sep, '/'))
PY
)
BASE_REL="${REL[0]}"
COMPARE_REL="${REL[1]}"
CHANGE_REL="${REL[2]}"
INSTANCES_REL="${REL[3]}"

# ── 替换 ──
"$PYTHON_BIN" - "$HTML" \
    "$BASE_REL" \
    "$COMPARE_REL" \
    "$CHANGE_REL" \
    "$INSTANCES_REL" <<'PY'
import re, sys

html_path = sys.argv[1]
new_a, new_b, new_change, new_instances = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

src = open(html_path, encoding="utf-8").read()

# cesium.html 里的 4 个目标行大致是:
#   const ts = await Cesium.Cesium3DTileset.fromUrl(
#     'XXX/tileset.json'
#   );
# 或者
#   const resp = await fetch('XXX/instances.json');
#
# 替换策略:按出现顺序(参考模型 → 点云 → 聚类 → 对比模型)各替换一次。

def replace_single_quoted_path(src, new_value, label):
    """替换 cesium.html 中第一个匹配 '<spaces>'  ...  '<spaces>'  的
    单引号字符串,把字符串内容换成 new_value。"""
    pattern = re.compile(r"(')(\s*)'[^']*'(\s*)'")
    # 上面这个正则不严谨;改用更直接的办法:匹配从单引号开始到下一个单引号
    # 的非贪婪字符串。
    pattern = re.compile(r"'([^']*)'")
    matches = list(pattern.finditer(src))
    if not matches:
        sys.exit(f"FATAL: cesium.html 没找到任何单引号字符串,无法替换 ({label})")
    # 取第一个出现的单引号字符串,替换它。
    m = matches[0]
    new_src = src[:m.start(1)] + new_value + src[m.end(1):]
    return new_src, 1

# 因为 cesium.html 里有多个 fromUrl/fetch 调用,而我们希望按顺序替换:
# 第 1 个 fromUrl → 参考模型 (A)
# 第 2 个 fromUrl → 算法结果点云 (change)
# 第 3 个 fetch     → 聚类结果 (instances)
# 第 4 个 fromUrl → 对比模型 (B)
# 实际操作:在 fromUrl 后的第一个单引号字符串 / fetch 后的第一个单引号字符串
# 用更精确的正则。

def replace_after(src, marker, new_value, label, start_from=0):
    """在 src 中找第一个 marker(like "Cesium.Cesium3DTileset.fromUrl("),
    在它之后找第一个单引号字符串,替换。start_from 限定搜索起点,确保
    多次替换按文件顺序而非每次都从开头开始。"""
    idx = src.find(marker, start_from)
    if idx == -1:
        sys.exit(f"FATAL: cesium.html 没找到 marker {marker!r} ({label})")
    # 从 marker 之后开始找第一个 '...'
    rest = src[idx + len(marker):]
    m = re.search(r"'([^']*)'", rest)
    if not m:
        sys.exit(f"FATAL: {marker!r} 之后没找到单引号字符串 ({label})")
    start = idx + len(marker) + m.start(1)
    end   = idx + len(marker) + m.end(1)
    new_src = src[:start] + new_value + src[end:]
    return new_src, end  # 返回下次搜索起点(替换位置之后)

# 按文件出现顺序替换 4 个目标:
#   1st fromUrl  → 参考模型 (epoch A)
#   2nd fromUrl  → 算法结果点云 (change)
#   fetch        → 聚类结果 (instances.json)
#   3rd fromUrl  → 对比模型 (epoch B / compareModel)
new_paths = [
    ("Cesium.Cesium3DTileset.fromUrl(", new_a,         "参考模型 (epoch A)"),
    ("Cesium.Cesium3DTileset.fromUrl(", new_change,    "算法结果点云 (change)"),
    ("fetch(",                          new_instances, "聚类结果 (instances.json)"),
    ("Cesium.Cesium3DTileset.fromUrl(", new_b,         "对比模型 (compareModel)"),
]

cursor = 0
for marker, new_value, label in new_paths:
    src, next_cursor = replace_after(src, marker, new_value, label, start_from=cursor)
    cursor = next_cursor

open(html_path, "w", encoding="utf-8").write(src)
print(f"  cesium.html 已更新 → {html_path}")
PY

echo "================================================================"
echo "  cesium.html 路径替换完成"
echo "================================================================"
echo "  taskId         : $TASK_ID"
echo "  参考模型 (A)   : $BASE_REL"
echo "  算法结果点云   : $CHANGE_REL"
echo "  聚类结果       : $INSTANCES_REL"
echo "  对比模型 (B)   : $COMPARE_REL"
echo "================================================================"