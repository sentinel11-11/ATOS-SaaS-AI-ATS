"""Close cancelled callbacks only after complete live AMI evidence, without redial."""

from datetime import timedelta

from sqlalchemy import select

from ats.adapters import Rejected, Retryable
from ats.callbacks import FLOWS
from ats.domain import ACTIVE, Problem
from ats.models import Attempt, CallLeg, Outbox, Tenant, now
from ats.repositories import TenantRepository
from ats.services import finish_attempt
from ats.workspace_service import event


def owned_channels(driver, attempt_id, known_channels=(), known_ids=()):
    """Do not infer absence from a missing saved channel or an incomplete list."""
    before = driver.channels()
    owned = []
    for row in before:
        channel, uniqueid = row["Channel"], row["Uniqueid"]
        owner = driver.action("Getvar", [("Channel", channel), ("Variable", "ATS_ATTEMPT_ID")]).get("Value", "")
        if owner == str(attempt_id):
            current = driver.action("Getvar", [("Channel", channel), ("Variable", "UNIQUEID")]).get("Value", "")
            if current != uniqueid:
                raise Retryable("ami_channel_identity_changed")
            owned.append((channel, uniqueid))
        elif channel in known_channels or uniqueid in known_ids or row.get("Linkedid") in known_ids:
            raise Rejected("callback_channel_identity_mismatch")
    after = driver.channels()
    if {(r["Channel"], r["Uniqueid"]) for r in before} != {(r["Channel"], r["Uniqueid"]) for r in after}:
        raise Retryable("ami_channels_changed")
    return owned


def reconcile_callback(factory, tenant_id, attempt_id, driver, *, hangup=False):
    with factory() as db:
        call = TenantRepository(db, tenant_id).get(Attempt, attempt_id)
        terminal_status = call.status if call.status not in ACTIVE else None
        if terminal_status and not hangup:
            return {"state": "terminal", "status": call.status}
        if call.call_flow not in FLOWS or call.route != "multicom_ami":
            raise Problem(409, "callback_required")
        legs = list(db.scalars(select(CallLeg).where(CallLeg.tenant_id == tenant_id, CallLeg.attempt_id == call.id)))
        channels = {leg.channel for leg in legs if leg.channel} | ({call.channel} if call.channel else set())
        identities = {value for leg in legs for value in (leg.uniqueid, leg.linkedid) if value}
    live = owned_channels(driver, attempt_id, channels, identities)
    if live:
        if hangup:
            for channel, uniqueid in live:
                # Revalidate immediately before a targeted write; never hang up
                # a reused channel or another operator's conversation.
                owner = driver.action("Getvar", [("Channel", channel), ("Variable", "ATS_ATTEMPT_ID")]).get("Value", "")
                current = driver.action("Getvar", [("Channel", channel), ("Variable", "UNIQUEID")]).get("Value", "")
                if owner != str(attempt_id) or current != uniqueid:
                    raise Retryable("ami_channel_identity_changed")
                driver.action("Hangup", [("Channel", channel)])
        return {"state": "ending" if hangup else "live", "channels": len(live)}
    if terminal_status:
        # An earlier manual SQL cleanup may have closed the record without
        # stopping its PBX channels. Close only this UUID, preserving its history.
        return {"state": "terminal", "status": terminal_status}
    with factory.begin() as db:
        db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        call = TenantRepository(db, tenant_id).get(Attempt, attempt_id, lock=True)
        if call.status not in ACTIVE:
            return {"state": "terminal", "status": call.status}
        dispatch = db.scalar(select(Outbox).where(Outbox.tenant_id == tenant_id, Outbox.dedupe_key == f"dial:{call.id}"))
        outcome = call.pending_terminal or {}
        observed_failure = (outcome.get("state") == "failed" and outcome.get("source") == "asterisk_scenario"
                            and not outcome.get("conflict"))
        # An empty snapshot while Originate is in flight cannot prove absence.
        # The fixed Originate timeout is 30s. Allow another 15s for asynchronous
        # creation and delayed worker acknowledgement before releasing resources.
        if not observed_failure and (not dispatch or dispatch.status in {"pending", "leased"}
                                    or dispatch.available_at > now() - timedelta(seconds=45)):
            return {"state": "dispatch_settling", "channels": 0}
        legs = list(db.scalars(select(CallLeg).where(CallLeg.tenant_id == tenant_id, CallLeg.attempt_id == call.id)))
        timestamp = now()
        known_ends = [leg.left_bridge_at or leg.ended_at for leg in legs if leg.left_bridge_at or leg.ended_at]
        if call.bridged_at and not call.bridge_ended_at and known_ends:
            call.bridge_ended_at = min(known_ends)
        if call.bridged_at and not call.bridge_ended_at:
            call.agent_result = {**(call.agent_result or {}), "duration_unconfirmed": True}
        for leg in legs:
            if not leg.ended_at:
                leg.ended_at, leg.status = timestamp, "ended" if leg.channel else "cancelled"
        if outcome.get("state") == "failed" and not outcome.get("conflict"):
            status, reason = "failed", outcome.get("reason") or "cancelled_by_user"
        elif outcome.get("state") == "completed" and call.bridged_at and call.bridge_ended_at and not outcome.get("conflict"):
            status, reason = "completed", ""
        else:
            # Channel absence proves resource release, not a successful talk.
            status, reason = "failed", "cancelled_by_user" if hangup else "callback_terminal_evidence_missing"
        event(db, call, "callback_channels_reconciled", "Complete AMI snapshots confirmed no channels for this attempt")
        finish_attempt(db, call, status, reason)
        return {"state": "terminal", "status": status, "reason": reason}
