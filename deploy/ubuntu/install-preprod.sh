#!/usr/bin/env bash
# First installation of the current ATS as an isolated pre-production stack.
# The legacy checkout/service is preserved; this script never stops it.
set -Eeuo pipefail
umask 0022

[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
repo_url=${ATS_PREPROD_REPO_URL:-https://github.com/sentinel11-11/ATOS-SaaS-AI-ATS.git}
branch=${ATS_PREPROD_BRANCH:-main}
project_dir=/opt/ats-beta

if [[ -e "$project_dir" ]]; then
  [[ -d "$project_dir/.git" ]] || { echo "$project_dir exists but is not a Git checkout; stopping." >&2; exit 1; }
  echo "$project_dir already exists. Use deploy/ubuntu/update-preprod.sh for updates." >&2
  exit 1
fi

install -d -o root -g root -m 0755 /opt
git clone --branch "$branch" --single-branch "$repo_url" "$project_dir"
cd "$project_dir"
bash deploy/ubuntu/install-beta.sh
bash deploy/ubuntu/preprod-audit.sh

cat <<'EOF'

Pre-production installation completed.
The old ATS service and Asterisk were not stopped or reconfigured.
Next steps:
  bash /opt/ats-beta/deploy/ubuntu/manage-beta.sh -m ats.cli bootstrap --name 'ATS Preprod' --login admin
  ssh -N -L 9130:127.0.0.1:9130 root@SERVER_IP
  open http://127.0.0.1:9130 in the local browser
EOF
