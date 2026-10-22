#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/dev-env.sh
bash scripts/dev-db.sh
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/alembic upgrade head
