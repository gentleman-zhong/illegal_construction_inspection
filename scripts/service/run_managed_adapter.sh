#!/usr/bin/env bash
set -euo pipefail
cd /root/illegal_construction_inspection
set -a
source .runtime-services/services.env
set +a
exec bash run_gateway_adapter.sh
