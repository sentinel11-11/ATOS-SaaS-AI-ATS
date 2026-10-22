#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
runtime_dir="$(pwd)/.runtime"
mkdir -p "$runtime_dir/pgsocket" "$runtime_dir/pgdata"
chmod 700 "$runtime_dir"
# Local development only: Unix socket under a private directory; no TCP listener/port.
if ! docker container inspect atos-dev-postgres >/dev/null 2>&1; then
  docker run -d --name atos-dev-postgres \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=ats \
    -v "$runtime_dir/pgsocket:/var/run/postgresql" \
    -v "$runtime_dir/pgdata:/var/lib/postgresql/data" \
    postgres:17-alpine -c listen_addresses= >/dev/null
else
  actual_socket="$(docker inspect atos-dev-postgres --format '{{range .Mounts}}{{if eq .Destination "/var/run/postgresql"}}{{.Source}}{{end}}{{end}}')"
  if [[ "$actual_socket" != "$runtime_dir/pgsocket" ]]; then
    printf 'Container name already belongs to another checkout; preserve it and choose a separate instance.\n' >&2
    exit 1
  fi
  docker start atos-dev-postgres >/dev/null
fi
for attempt in $(seq 1 30); do
  if docker exec atos-dev-postgres pg_isready -U postgres -d ats >/dev/null; then
    printf 'Development PostgreSQL ready (Unix socket).\n'
    exit 0
  fi
  sleep 1
done
printf 'PostgreSQL did not become ready.\n' >&2
exit 1
