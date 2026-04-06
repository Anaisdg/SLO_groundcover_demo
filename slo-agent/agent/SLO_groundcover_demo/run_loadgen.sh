#!/usr/bin/env bash
# Port-forward order-service and run load generator
# Usage: ./run_loadgen.sh [duration_seconds] [rps]

set -euo pipefail
DURATION="${1:-60}"
RPS="${2:-2}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Start port-forward if not already running
if ! pgrep -f "port-forward svc/order-service" > /dev/null 2>&1; then
  echo "[*] Starting port-forward..."
  kubectl port-forward svc/order-service -n slo-demo 8000:80 &>/dev/null &
  sleep 2
fi

# Verify connectivity
if ! curl -sf http://localhost:8000/healthz > /dev/null; then
  echo "[!] order-service not reachable on localhost:8000"
  exit 1
fi

echo "[*] Running load generator: ${RPS} RPS for ${DURATION}s"
cd "$SCRIPT_DIR/load-gen"
python load_gen.py http://localhost:8000 --rps "$RPS" --duration "$DURATION"
