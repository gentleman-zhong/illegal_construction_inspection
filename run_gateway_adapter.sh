#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"
ADAPTER_PYTHON="${ADAPTER_PYTHON:-$REPO_ROOT/.venv-adapter/bin/python}"
export ALGO_NAME="${ALGO_NAME:-illegal_construction_inspection}"
export ALGO_VERSION="${ALGO_VERSION:-v1.1.0}"
export LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
export LISTEN_PORT="${LISTEN_PORT:-6602}"
export CALLBACK_PORT="${CALLBACK_PORT:-6602}"
export UNREACHABLE_EXIT_ENABLED=false
: "${INSTANCE_TOKEN:?Set INSTANCE_TOKEN to the controller-issued secret}"
: "${CALLBACK_HOST:?Set CALLBACK_HOST to the controller-reachable adapter host}"
: "${CALLBACK_PORT:?Set CALLBACK_PORT to the controller-reachable adapter port}"
if [[ -z "${CONTROLLER_URL:-}" && -z "${CONTROLLER_HOST:-}" ]]; then
    echo "Set CONTROLLER_URL or CONTROLLER_HOST" >&2
    exit 1
fi
exec "$ADAPTER_PYTHON" -m uvicorn scripts.gateway_adapter.app:create_app --factory \
    --host "$LISTEN_HOST" --port "$LISTEN_PORT" --workers 1
