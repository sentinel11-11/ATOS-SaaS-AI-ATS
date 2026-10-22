#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/dev-env.sh
.venv/bin/alembic current
pids=()
cleanup() { for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done; wait || true; }
trap cleanup EXIT INT TERM
.venv/bin/uvicorn ats.api:app --host 127.0.0.1 --port 8000 --no-access-log &
pids+=("$!")
for role in scheduler telephony inbox crm; do
  .venv/bin/python -m ats.workers "$role" &
  pids+=("$!")
done
for attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8000/health/ready >/dev/null; then
    printf 'API ready; four worker processes started. Live calls require explicit configuration.\n'
    wait -n "${pids[@]}"
    exit 1
  fi
  sleep 1
done
printf 'API startup failed.\n' >&2
exit 1
