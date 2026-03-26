#!/usr/bin/env bash
# Run the SLO agent locally (after port-forwarding order-service)
# Usage:
#   1. Ensure .env is populated in the repo root (../. env)
#   2. ./run_local.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load environment
set -a
source "$SCRIPT_DIR/../.env"
set +a

cd "$SCRIPT_DIR/agent"
python slo_agent.py
