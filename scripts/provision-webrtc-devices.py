"""Run as ats-beta. Export private browser devices for the root Asterisk installer."""

import argparse
import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select, text


def provision(db, tenant_id):
    from ats.models import Operator, Tenant, User
    from ats.webrtc import device_for, password_for, save_device

    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    operators = db.scalars(select(Operator).join(User, User.id == Operator.user_id).where(
        Operator.tenant_id == tenant_id, User.active, ~User.archived, User.role == "operator",
    ).order_by(Operator.extension)).all()
    devices = []
    for operator in operators:
        device = device_for(db, tenant_id, operator.id)
        if not device:
            endpoint = "ats-web-" + operator.id.hex
            device = save_device(db, operator, endpoint, endpoint, secrets.token_urlsafe(36))
        devices.append({"endpoint": device.endpoint, "username": device.username,
                        "password": password_for(device)})
    return devices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/etc/ats-beta/ats.env"))
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run this helper as ats-beta, through install-widget-webrtc.py")
    load_dotenv(args.env_file, override=True, interpolate=False)
    from ats.config import settings
    from ats.db import session_factory

    config = settings()
    if not config.amo_tenant_id or config.amo_tenant_id != config.telephony_tenant_id:
        parser.error("Bind amoCRM and telephony to the same tenant before provisioning")
    with session_factory()() as db:
        if db.scalar(text("SELECT version_num FROM alembic_version")) != "0013":
            parser.error("Update the beta database to schema 0013 first")
        devices = provision(db, config.amo_tenant_id)
        if not devices:
            parser.error("No active operator users in the configured tenant")
        # The directory is private and controlled by the parent installer. Never print secrets.
        descriptor = os.open(args.private_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(devices, output)
                output.flush()
                os.fsync(output.fileno())
            db.commit()
        except Exception:
            args.private_output.unlink(missing_ok=True)
            raise
    print(f"Prepared {len(devices)} private browser devices. No calls placed.")


if __name__ == "__main__":
    main()
