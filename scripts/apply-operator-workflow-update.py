"""Preview/apply the configured tenant's caller limits and removal of old archived identities."""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select


def apply_update(db, tenant_id, actor, *, apply=False, caller_limit=None):
    from ats.deletions import delete_contact, delete_user
    from ats.models import CallerNumber, Contact, Tenant, User

    if apply:
        db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    users = db.scalars(select(User.id).where(User.tenant_id == tenant_id, User.archived)).all()
    contacts = db.scalars(select(Contact.id).where(Contact.tenant_id == tenant_id, Contact.archived)).all()
    numbers = db.scalars(select(CallerNumber).where(CallerNumber.tenant_id == tenant_id)).all()
    if apply:
        from ats.caller_quota import settings_changed

        for number in numbers:
            if caller_limit is not None:
                previous_quota = number.daily_limit, number.quota_cooldown_seconds, number.quota_cooldown_until
                number.daily_limit = caller_limit
                settings_changed(db, number, *previous_quota)
        for ident in users:
            delete_user(db, actor, ident)
        for ident in contacts:
            delete_contact(db, actor, ident)
    return {"apply": apply, "caller_numbers": len(numbers), "per_number_limit": caller_limit,
            "archived_users": len(users), "archived_contacts": len(contacts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path("/etc/ats-beta/ats.env"))
    parser.add_argument("--caller-limit", type=int, help="Set all numbers in this tenant to the given limit; omitted preserves custom limits")
    args = parser.parse_args()
    if args.caller_limit is not None and not 1 <= args.caller_limit <= 100000:
        parser.error("Caller limit must be from 1 to 100000")
    env_file = args.env_file.resolve()
    # Management commands may be launched from /root; settings uses project .env.
    import os

    os.chdir(Path(__file__).resolve().parents[1])
    load_dotenv(env_file, override=True, interpolate=False)
    from ats.config import settings
    from ats.db import session_factory
    from ats.domain import Problem
    from ats.models import User
    from ats.security import Principal

    config = settings()
    tid = config.telephony_tenant_id
    if not tid:
        parser.error("No configured telephony tenant")
    try:
        with session_factory()() as db:
            admin = db.scalar(select(User).where(User.tenant_id == tid, User.role == "admin", User.active, ~User.archived).limit(1))
            if not admin:
                parser.error("An active administrator is required")
            actor = Principal(tid, admin.id, "admin", admin.login)
            result = apply_update(db, tid, actor, apply=args.apply, caller_limit=args.caller_limit)
            if args.apply:
                db.commit()
            print(json.dumps(result))
    except Problem as exc:
        parser.exit(1, f"Update not applied: {exc.code}. Finish/reconcile calls before deleting identities.\n")


if __name__ == "__main__":
    main()
