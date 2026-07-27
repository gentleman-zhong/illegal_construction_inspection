#!/bin/bash
# 启动 illegal_construction_inspection 算法服务
# 用法:
#   ./run_service.sh         # 启动
#   ./run_service.sh stop    # 停止
#   ./run_service.sh status  # 查看状态
#   ./run_service.sh restart # 重启

set -e

REPO_ROOT="/root/illegal_construction_inspection"
PY="/root/miniconda3/envs/illegal_construction_inspection/bin/python"
PORT="${PORT:-6601}"
OUTPUT_BASE_DIR="${OUTPUT_BASE_DIR:-/root/illegal_construction_inspection/output}"
MODEL_ROOT="${MODEL_ROOT:-/model}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
PID_FILE="$REPO_ROOT/service.pid"
LOG_FILE="$REPO_ROOT/service.log"
UVICORN_PATTERN="uvicorn scripts.service.api_server"

cd "$REPO_ROOT"

is_running() {
    # 1) PID 文件存在且进程在跑
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    # 2) 即便 PID 文件失效,也用 pgrep 兜底
    if pgrep -f "$UVICORN_PATTERN" > /dev/null; then
        return 0
    fi
    return 1
}

start_service() {
    if is_running; then
        echo "服务已在运行 (PID $(pgrep -f "$UVICORN_PATTERN" | head -1))"
        return 0
    fi

    echo "启动服务 (port=$PORT, OUTPUT_BASE_DIR=$OUTPUT_BASE_DIR, MODEL_ROOT=$MODEL_ROOT) ..."
    mkdir -p "$(dirname "$OUTPUT_BASE_DIR")"

    export OUTPUT_BASE_DIR LOG_LEVEL PORT MODEL_ROOT
    nohup "$PY" -m uvicorn scripts.service.api_server:app \
        --host 0.0.0.0 --port "$PORT" --workers 1 \
        > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    disown
    sleep 3

    # 校验:进程是否真在跑 + 健康检查
    if kill -0 "$pid" 2>/dev/null; then
        echo "PID: $pid  (日志: $LOG_FILE)"
        if command -v curl > /dev/null; then
            local health
            health=$(curl -s "http://localhost:$PORT/healthz" || true)
            echo "healthz: $health"
        fi
    else
        echo "启动失败,日志如下:"
        tail -30 "$LOG_FILE"
        return 1
    fi
}

stop_service() {
    if ! is_running; then
        echo "服务未运行"
        rm -f "$PID_FILE"
        return 0
    fi
    echo "停止服务 ..."
    pkill -f "$UVICORN_PATTERN" || true
    # 等最多 5 秒优雅退出
    for _ in $(seq 1 10); do
        if ! pgrep -f "$UVICORN_PATTERN" > /dev/null; then
            break
        fi
        sleep 0.5
    done
    # 强杀兜底
    pkill -9 -f "$UVICORN_PATTERN" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "已停止"
}

status_service() {
    if is_running; then
        local pid
        pid=$(pgrep -f "$UVICORN_PATTERN" | head -1)
        echo "运行中 (PID $pid)"
        if command -v curl > /dev/null; then
            curl -s "http://localhost:$PORT/healthz" || echo "(healthz 不可达)"
        fi
    else
        echo "未运行"
    fi
}

case "${1:-start}" in
    start)   start_service ;;
    stop)    stop_service ;;
    restart) stop_service; start_service ;;
    status)  status_service ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac