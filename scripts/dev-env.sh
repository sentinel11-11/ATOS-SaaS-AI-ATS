#!/usr/bin/env bash
# Source this script from the repository root. Existing explicit settings win.
export ATS_DATABASE_URL="${ATS_DATABASE_URL:-postgresql+psycopg://postgres@/ats?host=$(pwd)/.runtime/pgsocket}"
export ATS_PUBLIC_ORIGIN="${ATS_PUBLIC_ORIGIN:-http://127.0.0.1:8000}"
export ATS_SECURE_COOKIES="${ATS_SECURE_COOKIES:-false}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/workspace/.cache/uv}"
export npm_config_cache="${npm_config_cache:-/workspace/.cache/npm}"
