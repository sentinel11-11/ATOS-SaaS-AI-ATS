"""Mobile operator-first calls. Asterisk channel/bridge evidence stays tenant scoped."""

import re
from uuid import UUID

from sqlalchemy import func, select

from ats.domain import ACTIVE, DIAL_STATUSES, Problem
from ats.models import (
    Attempt,
    BlacklistEntry,
    CallerNumber,
    CallLeg,
    Campaign,
    CampaignItem,
    Contact,
    Operator,
    Outbox,
    QueueEntry,
    Tenant,
    User,
    WidgetCall,
    now,
)
from ats.workspace_service import event

FLOW = "mobile_callback"
FLOWS = {FLOW, "browser_callback"}
REASONS = {
    "operator_unavailable", "operator_busy", "operator_no_answer", "operator_not_confirmed",
    "customer_unavailable", "customer_busy", "customer_no_answer", "operator_cancelled",
    "callback_gate_denied", "callback_authorization_timeout", "callback_originate_failed",
    "callback_prompt_unavailable", "callback_confirmation_failed", "callback_parameters_invalid",
}


def validate_mobile(config, operator, customer_phone=None):
    if not config.callback_enabled:
        raise Problem(409, "callback_disabled", "Установите callback-диалплан и включите ATS_CALLBACK_ENABLED.")
    if config.ami_cli_endpoint_prefix != "mcm-cli-" or config.ami_tech != "PJSIP":
        raise Problem(409, "callback_requires_cli_alias", "Для callback требуется PJSIP и префикс mcm-cli-.")
    if not re.fullmatch(r"\+7[0-9]{10}", operator.mobile_phone):
        raise Problem(409, "operator_mobile_phone_required", "Сохраните личный номер менеджера в формате +7.")
    if not config.destination_allowed(operator.mobile_phone):
        raise Problem(409, "operator_destination_not_allowed", "Добавьте номер менеджера в тестовые направления VPS.")
    if customer_phone:
        if customer_phone == operator.mobile_phone:
            raise Problem(409, "callback_parties_must_differ", "Для теста нужны разные номера менеджера и клиента.")
        if not re.fullmatch(r"\+7[0-9]{10}", customer_phone) or not config.destination_allowed(customer_phone):
            raise Problem(409, "destination_not_allowed")


def authorize_customer(db, attempt, config):
    """Called under the tenant mutex immediately before the permission Setvar."""
    if attempt.call_flow not in FLOWS or attempt.status not in (ACTIVE - {"unknown"}) or not attempt.manager_confirmed_at:
        return False
    tenant = db.get(Tenant, attempt.tenant_id)
    item = db.get(CampaignItem, attempt.item_id)
    campaign, contact = db.get(Campaign, item.campaign_id), db.get(Contact, item.contact_id)
    queue = db.scalar(select(QueueEntry).where(QueueEntry.attempt_id == attempt.id))
    operator = db.get(Operator, queue.operator_id) if queue and queue.operator_id else None
    user = db.get(User, operator.user_id) if operator else None
    leg = db.scalar(select(CallLeg).where(CallLeg.attempt_id == attempt.id, CallLeg.role == "operator"))
    number = db.scalar(select(CallerNumber).where(
        CallerNumber.tenant_id == tenant.id, CallerNumber.phone == attempt.caller_number,
    ))
    from zoneinfo import ZoneInfo

    local = now().astimezone(ZoneInfo(tenant.timezone))
    widget_id = attempt.route_snapshot.get("widget_call_id")
    widget = db.get(WidgetCall, UUID(widget_id)) if widget_id else None
    browser = attempt.call_flow == "browser_callback"
    if widget and (widget.cancel_requested or widget.expires_at <= now()):
        return False
    if browser:
        from ats.webrtc import check_device

        try:
            device = check_device(db, config, operator, widget.browser_session_id if widget else None)
        except (Problem, AttributeError):
            return False
        if not widget or not widget.media_ready_at or device.endpoint != attempt.route_snapshot.get("operator_endpoint"):
            return False
    if (
        not config.live_calls_enabled or config.telephony_tenant_id != tenant.id
        or campaign.status != "running" or campaign.archived
        or not campaign.window_start <= local.hour < campaign.window_end
        or local.weekday() not in campaign.weekdays
        or contact.phone != attempt.phone or not contact.consent or contact.blocked or contact.archived
        or db.scalar(select(BlacklistEntry.id).where(
            BlacklistEntry.tenant_id == tenant.id, BlacklistEntry.phone == contact.phone,
        ))
        or not operator or not user or not user.active or user.role != "operator"
        or operator.status != "busy"
        or (not browser and not widget and operator.connection_method != "mobile")
        or not leg or not leg.answered_at or (not browser and operator.mobile_phone != leg.phone)
        or not number or not number.enabled or not number.verified
        or not number.enabled_outgoing or number.quarantined or number.provider != "multicom_ami"
        or attempt.route_snapshot.get("endpoint") != config.ami_cli_endpoint_prefix + attempt.caller_number
    ):
        return False
    rules = config.ami_operator_number_map
    if rules and attempt.caller_number not in rules.get(str(operator.extension), []):
        return False
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    usage = db.scalar(select(func.coalesce(func.sum(Attempt.channel_units), 0)).where(
        Attempt.tenant_id == tenant.id, Attempt.caller_number == attempt.caller_number,
        Attempt.created_at >= day_start,
    ))
    if usage > number.daily_limit:
        return False
    try:
        if not browser:
            validate_mobile(config, operator, contact.phone)
        elif not config.destination_allowed(contact.phone):
            return False
    except Problem:
        return False
    return True


def queue_authorization(db, attempt):
    key = f"callback-authorize:{attempt.id}"
    if not db.scalar(select(Outbox.id).where(Outbox.tenant_id == attempt.tenant_id, Outbox.dedupe_key == key)):
        db.add(Outbox(tenant_id=attempt.tenant_id, kind="control", dedupe_key=key,
                      payload={"attempt_id": str(attempt.id), "command": "callback_authorize"}))


def queue_hangup(db, attempt):
    """A manager can cancel while the operator is still ringing, before Local ;1 is reported."""
    channel = attempt.channel or db.scalar(select(CallLeg.channel).where(
        CallLeg.attempt_id == attempt.id, CallLeg.role == "operator", CallLeg.channel != "",
    ))
    key = f"hangup:{attempt.id}"
    existing = db.scalar(select(Outbox).where(Outbox.tenant_id == attempt.tenant_id, Outbox.dedupe_key == key))
    payload = {"attempt_id": str(attempt.id), "command": "hangup", "channel": channel or ""}
    if existing and existing.status in {"done", "dead", "unknown"}:
        # An explicit retry must be useful after a missing channel, lost AMI
        # response or a previous Hangup acknowledgement with missing events.
        existing.status, existing.available_at, existing.last_error = "pending", now(), ""
        existing.lease_token, existing.lease_until = None, None
        existing.payload = payload
    elif not existing:
        db.add(Outbox(tenant_id=attempt.tenant_id, kind="control", dedupe_key=key,
                      payload=payload))


def queue_reconciliation(db, attempt):
    key = f"callback-reconcile:{attempt.id}"
    existing = db.scalar(select(Outbox).where(Outbox.tenant_id == attempt.tenant_id, Outbox.dedupe_key == key))
    if existing:
        if existing.status in {"done", "dead", "unknown"}:
            existing.status, existing.available_at, existing.last_error = "pending", now(), ""
            existing.lease_token, existing.lease_until = None, None
    else:
        db.add(Outbox(tenant_id=attempt.tenant_id, kind="control", dedupe_key=key,
                      payload={"attempt_id": str(attempt.id), "command": "callback_reconcile"}))


def _uncertain(db, attempt, reason):
    if attempt.status in ACTIVE and (attempt.status != "unknown" or attempt.reason != reason):
        attempt.status, attempt.reason, attempt.updated_at = "unknown", reason, now()
        event(db, attempt, "unknown", reason)
    if attempt.status in ACTIVE:
        queue_reconciliation(db, attempt)


def _finish_if_proven(db, attempt):
    from ats.services import finish_attempt

    if attempt.status not in ACTIVE:
        return
    legs = db.scalars(select(CallLeg).where(CallLeg.attempt_id == attempt.id)).all()
    if len(legs) != 2:
        return
    if all(leg.entered_bridge_at for leg in legs) and attempt.manager_confirmed_at:
        start = max(leg.entered_bridge_at for leg in legs)
        ends = [leg.left_bridge_at or leg.ended_at for leg in legs if leg.left_bridge_at or leg.ended_at]
        if not ends or min(ends) >= start:
            if not attempt.bridged_at:
                attempt.bridged_at = attempt.answered_at = start
                event(db, attempt, "callback_bridged")
                if attempt.status != "unknown":
                    finish_attempt(db, attempt, "answered")
            if ends:
                attempt.bridge_ended_at = min(ends)
    outcome = attempt.pending_terminal or {}
    if not outcome:
        return
    if outcome.get("conflict"):
        _uncertain(db, attempt, "callback_result_conflict")
        return
    if not any(leg.channel for leg in legs):
        _uncertain(db, attempt, "callback_leg_tracking_missing")
        return
    for leg in legs:
        if not leg.channel:
            leg.status, leg.ended_at = "cancelled", leg.ended_at or now()
    if any(not leg.ended_at for leg in legs):
        return
    if outcome["state"] == "completed" and not attempt.bridged_at:
        _uncertain(db, attempt, "callback_bridge_unconfirmed")
        return
    attempt.reason = ""
    finish_attempt(db, attempt, outcome["state"], outcome.get("reason", ""))


def _identity(value):
    return str(value or "") if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(value or "")) else ""


def leg_projection(db, p, attempt_id):
    owner = db.scalar(select(Operator.user_id).join(QueueEntry, QueueEntry.operator_id == Operator.id)
                      .where(QueueEntry.tenant_id == p.tenant_id, QueueEntry.attempt_id == attempt_id))
    return [{
        "role": leg.role, "status": leg.status,
        "phone": leg.phone if leg.role == "customer" or p.role == "admin" or p.user_id == owner else "",
        "channel": leg.channel, "answered_at": leg.answered_at,
        "bridged_at": leg.entered_bridge_at, "ended_at": leg.ended_at,
        "dial_status": leg.dial_status, "hangup_cause": leg.hangup_cause,
    } for leg in db.scalars(select(CallLeg).where(
        CallLeg.tenant_id == p.tenant_id, CallLeg.attempt_id == attempt_id,
    ).order_by(CallLeg.role))]


def handle_frame(factory, tenant_id, frame):
    """Return True only when the frame belongs to a registered callback attempt."""
    if not tenant_id:
        return False
    custom = frame.get("Event") == "UserEvent" and frame.get("UserEvent") == "ATSCallback"
    response = frame.get("Event") == "OriginateResponse"
    native = frame.get("Event") in {"BridgeEnter", "BridgeLeave", "Hangup", "Newstate"}
    if not (custom or response or native):
        return False
    with factory.begin() as db:
        if custom or response:
            try:
                ident = UUID(frame.get("AttemptID") if custom else frame.get("ActionID"))
            except (ValueError, TypeError, AttributeError):
                return custom
        else:
            found = db.scalar(select(CallLeg).join(Attempt, Attempt.id == CallLeg.attempt_id).where(
                CallLeg.tenant_id == tenant_id, CallLeg.channel == frame.get("Channel", ""),
                CallLeg.uniqueid == _identity(frame.get("Uniqueid")),
                Attempt.call_flow.in_(FLOWS), Attempt.status.in_(ACTIVE),
            ).limit(1))
            if not found:
                return False
            ident = found.attempt_id
        db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        attempt = db.scalar(select(Attempt).where(
            Attempt.id == ident, Attempt.tenant_id == tenant_id, Attempt.call_flow.in_(FLOWS),
        ).with_for_update())
        if not attempt:
            return custom
        if attempt.status not in ACTIVE:
            return True
        legs = {leg.role: leg for leg in db.scalars(select(CallLeg).where(CallLeg.attempt_id == ident))}
        if len(legs) != 2:
            return True
        browser = attempt.call_flow == "browser_callback"
        prefix = (f"Local/{attempt.route_snapshot.get('operator_id')}@ats-beta-callback-browser-"
                  if browser else f"Local/{legs['operator'].phone}@ats-beta-callback-operator-")
        channel = str(frame.get("Channel") or "")
        control = channel.startswith(prefix) and len(channel) <= 160
        state, role = frame.get("State"), frame.get("Leg")
        if response:
            if control:
                attempt.channel = channel
            if frame.get("Response") != "Failure":
                return True
            state, role = "failed", "operator"
        if custom or response:
            if role not in legs:
                return True
            leg = legs[role]
            if state in {"created", "answered"}:
                endpoint = attempt.route_snapshot.get("endpoint", "")
                if browser and role == "operator":
                    endpoint = attempt.route_snapshot.get("operator_endpoint", "")
                if not endpoint or not channel.startswith(f"PJSIP/{endpoint}-") or len(channel) > 160:
                    return True
                uniqueid = _identity(frame.get("Uniqueid"))
                if not uniqueid:
                    return True
                if leg.channel and (leg.channel != channel or leg.uniqueid != uniqueid):
                    _uncertain(db, attempt, "callback_leg_identity_conflict")
                    return True
                changed = not leg.channel or (state == "answered" and not leg.answered_at)
                leg.channel, leg.uniqueid = channel, uniqueid
                leg.linkedid = _identity(frame.get("Linkedid"))
                if leg.status == "cancelled":
                    leg.ended_at = None
                if not leg.ended_at:
                    leg.status = "answered" if state == "answered" or leg.answered_at else "ringing"
                if state == "answered" and not leg.answered_at:
                    leg.answered_at = now()
                if changed:
                    event(db, attempt, f"callback_{role}_{state}")
                widget_id = attempt.route_snapshot.get("widget_call_id")
                widget = db.get(WidgetCall, UUID(widget_id)) if widget_id else None
                if widget and widget.cancel_requested:
                    queue_hangup(db, attempt)
            elif state in {"ready", "confirmed"} and role == "operator" and control and channel.endswith(";1"):
                attempt.channel = channel
                if not attempt.manager_confirmed_at:
                    attempt.manager_confirmed_at = now()
                    source = "browser" if browser else "network_answer" if state == "ready" else "dtmf"
                    attempt.route_snapshot = {**attempt.route_snapshot, "ready_source": source}
                    event(db, attempt, "callback_operator_ready" if state == "ready" else "callback_operator_confirmed", source)
                    widget_id = attempt.route_snapshot.get("widget_call_id")
                    widget = db.get(WidgetCall, UUID(widget_id)) if widget_id else None
                    if not browser or (widget and widget.media_ready_at):
                        queue_authorization(db, attempt)
            elif state in {"completed", "failed"} and (control or response):
                if not attempt.channel and control:
                    attempt.channel = channel
                reason = frame.get("ReasonCode", "")
                reason = reason if reason in REASONS else (
                    "operator_busy" if response and frame.get("Reason") == "5"
                    else "callback_originate_failed" if response else "customer_unavailable"
                )
                result = {"state": state, "reason": reason if state == "failed" else ""}
                if custom and control:
                    result["source"] = "asterisk_scenario"
                previous = attempt.pending_terminal or {}
                if previous and previous.get("state") != state:
                    result["conflict"] = True
                elif previous and previous.get("reason") != "callback_originate_failed":
                    result = previous
                attempt.pending_terminal = result
                if frame.get("DialStatus") in DIAL_STATUSES:
                    leg.dial_status = frame["DialStatus"]
                if not previous:
                    event(db, attempt, f"callback_scenario_{state}", result["reason"])
        else:
            leg = next(leg for leg in legs.values() if leg.channel == channel)
            kind, timestamp = frame["Event"], now()
            if kind == "BridgeEnter":
                bridge = _identity(frame.get("BridgeUniqueid"))
                if bridge and not leg.entered_bridge_at:
                    leg.entered_bridge_at = timestamp
                    leg.bridge_id = bridge
                    leg.answered_at = leg.answered_at or timestamp
            elif kind == "BridgeLeave" and not leg.left_bridge_at:
                leg.left_bridge_at = timestamp
            elif kind == "Hangup" and not leg.ended_at:
                leg.ended_at, leg.status = timestamp, "ended"
                cause = str(frame.get("Cause") or "")
                if cause.isascii() and cause.isdigit() and 0 <= int(cause) <= 127:
                    leg.hangup_cause = int(cause)
                event(db, attempt, f"callback_{leg.role}_ended")
        _finish_if_proven(db, attempt)
        return True
