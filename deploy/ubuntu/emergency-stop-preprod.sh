#!/usr/bin/env bash
# Pause all preprod campaigns and stop only the beta telephony worker.
# Existing calls may continue; use Asterisk/PBX controls to hang them up.
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }

cd /opt/ats-beta
bash deploy/ubuntu/manage-beta.sh -c '
from sqlalchemy import select
from ats.db import session_factory
from ats.models import Audit, Campaign, Tenant, now

with session_factory().begin() as db:
    changed = 0
    for tenant in db.scalars(select(Tenant).with_for_update()).all():
        for campaign in db.scalars(select(Campaign).where(
            Campaign.tenant_id == tenant.id,
            Campaign.status == "running",
            ~Campaign.archived,
        ).with_for_update()).all():
            campaign.status = "paused"
            db.add(Audit(tenant_id=tenant.id, actor_id=None,
                         action="system.emergency_stop", entity_id=campaign.id,
                         created_at=now()))
            changed += 1
    print(f"paused_campaigns={changed}")
'
systemctl stop ats-beta-worker@telephony
printf 'Emergency stop applied: campaigns paused and beta telephony worker stopped.\n'
printf 'Restart only after investigation: systemctl start ats-beta-worker@telephony\n'
