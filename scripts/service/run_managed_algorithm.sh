#!/usr/bin/env bash
set -euo pipefail
cd /root/illegal_construction_inspection
# Preserve the already-running service and its in-memory tasks. Once it exits,
# replace this watcher with the foreground service owned by Supervisor.
pattern='^/root/miniconda3/envs/illegal_construction_inspection/bin/python -m uvicorn scripts.service.api_server:app --host 0.0.0.0 --port 6601 --workers 1$'
existing_pid="$(pgrep -f "$pattern" || true)"
if [[ -n "$existing_pid" ]]; then
    if [[ "$existing_pid" == *$'\n'* ]]; then
        echo 'Multiple algorithm processes found; refusing to launch another.' >&2
        exit 1
    fi
    echo "Monitoring existing algorithm PID $existing_pid without restarting it."
    while [[ -r "/proc/$existing_pid/cmdline" ]] && pgrep -f "$pattern" | grep -qx "$existing_pid"; do
        sleep 2
    done
    echo 'Previous algorithm process exited; starting under Supervisor.'
fi
export PORT=6601
exec bash run_service.sh foreground
