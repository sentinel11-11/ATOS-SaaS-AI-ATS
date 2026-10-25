#!/usr/bin/env bash
# Initial simulator installation on Ubuntu 24.04 x86_64; not an updater for a live beta.
set -euo pipefail
umask 0022
if [[ "$(id -u)" != 0 ]]; then
  printf 'Run this initial installer as root.\n' >&2
  exit 1
fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(uname -m)" != x86_64 ]]; then
  printf 'This installer targets Ubuntu 24.04 x86_64 only.\n' >&2
  exit 1
fi
project_dir=/opt/ats-beta
state_dir=/var/lib/ats-beta
tools_dir=/opt/ats-beta-tools
db_url='postgresql+psycopg://ats-beta@/ats_beta?host=/var/run/postgresql'
node_version=24.19.0
uv_version=0.12.23
expected_env() {
  cat <<'ENV'
ATS_DEPLOYMENT=preprod
ATS_INSTANCE_NAME=ats-preprod
ATS_DATABASE_URL=postgresql+psycopg://ats-beta@/ats_beta?host=/var/run/postgresql
ATS_PUBLIC_ORIGIN=http://127.0.0.1:9130
ATS_SECURE_COOKIES=false
ATS_LIVE_CALLS_ENABLED=false
ATS_CRM_ENABLED=false
ATS_INBOUND_ENABLED=false
ATS_MEDIA_ENABLED=false
ENV
}
cd "$project_dir"
if [[ ! -f pyproject.toml || ! -f deploy/ubuntu/ats-beta-api.service || -L "$project_dir" ]]; then
  printf 'Expected a standalone checkout at /opt/ats-beta.\n' >&2
  exit 1
fi
for unit in ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm; do
  if systemctl is-active --quiet "$unit"; then
    printf 'Existing beta service is active: %s. This is an initial installer, not an updater.\n' "$unit" >&2
    exit 1
  fi
done
for unit_file in ats-beta-api.service ats-beta-worker@.service; do
  if [[ -e "/etc/systemd/system/$unit_file" ]] && ! cmp -s "deploy/ubuntu/$unit_file" "/etc/systemd/system/$unit_file"; then
    printf 'Preserve and review the existing custom unit: %s\n' "$unit_file" >&2
    exit 1
  fi
done
if ss -ltnH | awk '{print $4}' | grep -E '(^|:)9130$' >/dev/null; then
  printf 'Port 9130 is already occupied; installation stopped.\n' >&2
  exit 1
fi
if [[ -e /etc/ats-beta/ats.env ]]; then
  # Allow resuming our unchanged simulator config; preserve any user-edited/live config.
  if ! cmp -s /etc/ats-beta/ats.env <(expected_env); then
    printf 'Existing custom /etc/ats-beta/ats.env found; preserve it and use a reviewed update procedure.\n' >&2
    exit 1
  fi
fi

# Install new prerequisites without requesting automatic restarts of unrelated services.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
apt-get update
apt-get install -y --no-install-recommends python3-venv postgresql-16 postgresql-client-16 curl ca-certificates xz-utils
python3 -c 'import sys; assert sys.version_info[:2] == (3, 12), "Expected Ubuntu Python 3.12"'

if id ats-beta >/dev/null 2>&1; then
  if [[ "$(getent passwd ats-beta | cut -d: -f6)" != "$state_dir" || "$(id -gn ats-beta)" != ats-beta ]]; then
    printf 'Existing ats-beta account has unexpected home/group; preserve it and review.\n' >&2
    exit 1
  fi
else
  useradd --system --user-group --create-home --home-dir "$state_dir" --shell /usr/sbin/nologin ats-beta
fi
install -d -o ats-beta -g ats-beta -m 0750 "$state_dir"
install -d -o root -g root -m 0755 "$tools_dir"

# Keep build tools private to this installation; do not replace /usr/bin/node or system Python.
if [[ ! -x "$tools_dir/installer/bin/python" ]]; then
  python3 -m venv "$tools_dir/installer"
fi
"$tools_dir/installer/bin/python" -m pip install --disable-pip-version-check "uv==$uv_version"
node_dir="$tools_dir/node-v$node_version-linux-x64"
if [[ ! -x "$node_dir/bin/node" ]]; then
  download_dir="$(mktemp -d)"
  trap 'rm -rf "$download_dir"' EXIT
  node_file="node-v$node_version-linux-x64.tar.xz"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    "https://nodejs.org/dist/v$node_version/$node_file" --output "$download_dir/$node_file"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    "https://nodejs.org/dist/v$node_version/SHASUMS256.txt" --output "$download_dir/SHASUMS256.txt"
  (cd "$download_dir"; awk -v name="$node_file" '$2 == name {print}' SHASUMS256.txt > expected.sha256
   test -s expected.sha256; sha256sum --check expected.sha256)
  tar -xJf "$download_dir/$node_file" -C "$tools_dir" --no-same-owner
fi
"$node_dir/bin/node" --version

systemctl start postgresql
pg() { runuser -u postgres -- psql -X --host=/var/run/postgresql --port=5432 --dbname=postgres --set=ON_ERROR_STOP=1 "$@"; }
if [[ "$(pg -tAc 'SHOW listen_addresses' | tr -d '[:space:]')" != localhost ]]; then
  printf 'PostgreSQL is not configured for localhost only; review existing settings before continuing.\n' >&2
  exit 1
fi
install -d -o root -g ats-beta -m 0750 /etc/ats-beta
marker=/etc/ats-beta/bootstrap-owner
if [[ ! -e "$marker" ]]; then
  if [[ "$(pg -tAc "SELECT count(*) FROM pg_roles WHERE rolname='ats-beta'")" != 0 || \
        "$(pg -tAc "SELECT count(*) FROM pg_database WHERE datname='ats_beta'")" != 0 ]]; then
    printf 'Existing unowned ats-beta DB/role detected; preserve and review before installation.\n' >&2
    exit 1
  fi
  (umask 0077; set -o noclobber; printf '/opt/ats-beta\n' > "$marker")
fi
if [[ -L "$marker" || "$(stat -c %u "$marker")" != 0 || "$(cat "$marker")" != "$project_dir" ]]; then
  printf 'Unexpected database bootstrap marker; installation stopped.\n' >&2
  exit 1
fi
if [[ "$(pg -tAc "SELECT count(*) FROM pg_roles WHERE rolname='ats-beta'")" == 0 ]]; then
  pg -c 'CREATE ROLE "ats-beta" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
fi
if [[ "$(pg -tAc "SELECT count(*) FROM pg_roles WHERE rolname='ats-beta' AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)")" != 0 ]]; then
  printf 'The beta database role has unexpected privileges; review required.\n' >&2
  exit 1
fi
if [[ "$(pg -tAc "SELECT count(*) FROM pg_database WHERE datname='ats_beta'")" == 0 ]]; then
  pg -c 'CREATE DATABASE ats_beta OWNER "ats-beta"'
fi
if [[ "$(pg -tAc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='ats_beta'")" != ats-beta ]]; then
  printf 'Existing ats_beta database has a different owner; installation stopped.\n' >&2
  exit 1
fi

chown -R ats-beta:ats-beta "$project_dir"
run_beta() {
  runuser -u ats-beta -- env -i HOME="$state_dir" LANG=C.UTF-8 \
    PATH="$node_dir/bin:/usr/bin:/bin" UV_CACHE_DIR="$state_dir/.cache/uv" \
    npm_config_cache="$state_dir/.cache/npm" ATS_DATABASE_URL="$db_url" "$@"
}
run_beta "$tools_dir/installer/bin/uv" sync --frozen --no-dev --python /usr/bin/python3
run_beta nice -n 10 "$node_dir/bin/npm" --prefix frontend ci
run_beta nice -n 10 "$node_dir/bin/npm" --prefix frontend run build
# Explicit peer-authentication check as the new OS user, with a separate DB owner.
run_beta "$project_dir/.venv/bin/python" -m alembic upgrade head

if [[ ! -e /etc/ats-beta/ats.env ]]; then
  (umask 0027; set -o noclobber; expected_env > /etc/ats-beta/ats.env)
fi
chown root:ats-beta /etc/ats-beta/ats.env
chmod 0640 /etc/ats-beta/ats.env
for unit_file in ats-beta-api.service ats-beta-worker@.service; do
  install -o root -g root -m 0644 "deploy/ubuntu/$unit_file" "/etc/systemd/system/$unit_file"
done
systemd-analyze verify /etc/systemd/system/ats-beta-api.service /etc/systemd/system/ats-beta-worker@.service
systemctl daemon-reload
systemctl enable --now ats-beta-api.service \
  ats-beta-worker@scheduler.service ats-beta-worker@telephony.service \
  ats-beta-worker@inbox.service ats-beta-worker@crm.service
for attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:9130/health/ready >/dev/null; then
    printf 'Beta API ready on loopback port 9130. Live calls and CRM are disabled.\n'
    printf 'Create a beta administrator using the command in docs/SERVER_BETA_RU.md.\n'
    exit 0
  fi
  sleep 1
done
printf 'Readiness failed. Inspect: journalctl -u ats-beta-api -n 60 --no-pager\n' >&2
exit 1
