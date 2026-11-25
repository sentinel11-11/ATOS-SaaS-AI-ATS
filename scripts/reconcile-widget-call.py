"""Inspect one widget call, or cancel it using complete live Asterisk evidence."""

import argparse
import json
import re
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--widget-call-id", required=True, help="Full UUID or unique prefix of at least eight hex characters")
    parser.add_argument("--hangup", action="store_true", help="Cancel this call and reconcile its channels; no other calls are changed")
    parser.add_argument("--env-file", type=Path, default=Path("/etc/ats-beta/ats.env"))
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{8}[0-9a-f-]{0,28}", args.widget_call_id):
        parser.error("Use a UUID or its unique prefix of at least eight characters")
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True, interpolate=False)
    from sqlalchemy import String, cast, select

    from ats.adapters import AMI, DeliveryUnknown, Rejected, Retryable
    from ats.api_widget import attempt_for
    from ats.call_reconciliation import owned_channels, reconcile_callback
    from ats.callbacks import queue_hangup
    from ats.config import settings
    from ats.connections import settings_for_tenant
    from ats.db import session_factory
    from ats.domain import ACTIVE, Problem
    from ats.models import Audit, CallLeg, Tenant, WidgetCall
    from ats.services import finish_attempt

    config, factory = settings(), session_factory()
    tenant_id = config.amo_tenant_id
    if not tenant_id or tenant_id != config.telephony_tenant_id:
        parser.error("Bind amoCRM and telephony to the same tenant first")
    with factory() as db:
        calls = db.scalars(select(WidgetCall).where(
            WidgetCall.tenant_id == tenant_id, cast(WidgetCall.id, String).startswith(args.widget_call_id),
        )).all()
        if len(calls) != 1:
            parser.error("The widget call prefix must identify exactly one call in this tenant")
        call = calls[0]
        attempt = attempt_for(db, call)
        if not attempt:
            parser.error("No attempt has been allocated; cancel the queued call in the widget")
        call_id, attempt_id = call.id, attempt.id
        runtime = settings_for_tenant(db, tenant_id)
        status = attempt.status
        channels = {attempt.channel} if attempt.channel else set()
        identities = set()
        for leg in db.scalars(select(CallLeg).where(CallLeg.tenant_id == tenant_id, CallLeg.attempt_id == attempt.id)):
            if leg.channel:
                channels.add(leg.channel)
            identities.update(value for value in (leg.uniqueid, leg.linkedid) if value)
    print(json.dumps({"widget_call_id": str(call_id), "attempt_id": str(attempt_id), "status": status}))
    if args.hangup:
        with factory.begin() as db:
            db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
            call = db.get(WidgetCall, call_id)
            call.cancel_requested = True
            attempt = attempt_for(db, call)
            if attempt.status == "prepared":
                finish_attempt(db, attempt, "failed", "cancelled_by_user")
            elif attempt.status in ACTIVE:
                queue_hangup(db, attempt)
            db.add(Audit(
                tenant_id=tenant_id, actor_id=None, action="widget.management_hangup_requested", entity_id=call_id,
            ))
    driver = AMI(runtime, lambda _frame: None)
    try:
        driver.connect()
        if not args.hangup:
            print(json.dumps({"live_channels": len(owned_channels(driver, attempt_id, channels, identities))}))
            return
        deadline = time.monotonic() + 15
        while True:
            try:
                result = reconcile_callback(factory, tenant_id, attempt_id, driver, hangup=True)
                print(json.dumps(result))
                if result["state"] == "terminal":
                    return
            except (Rejected, Retryable, DeliveryUnknown) as exc:
                print(json.dumps({"state": "requires_evidence", "error": getattr(exc, "code", "ami_outcome_unknown")}))
            if time.monotonic() >= deadline:
                raise SystemExit("The operator remains reserved until Asterisk evidence is complete. Retry the same command.")
            time.sleep(1)
    except (Rejected, Retryable, DeliveryUnknown, Problem) as exc:
        raise SystemExit(getattr(exc, "code", "ami_outcome_unknown")) from exc
    finally:
        driver.close()


if __name__ == "__main__":
    main()
