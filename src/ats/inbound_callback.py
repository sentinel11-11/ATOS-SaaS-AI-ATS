"""Reserve a return call on the last operator's Asterisk endpoint."""

import re
import secrets
from datetime import timedelta

from sqlalchemy import case, func, select

from ats.callbacks import validate_mobile
from ats.connections import settings_for_tenant
from ats.domain import ACTIVE, Problem
from ats.inbound import ingest_inbound, normalize_phone
from ats.models import (
    Attempt,
    CallbackAffinity,
    CallerNumber,
    Contact,
    InboundCall,
    InboundCallback,
    Operator,
    QueueEntry,
    Tenant,
    User,
    now,
)
from ats.webrtc import check_device, pending_calls
from ats.widget_context import available_browser_device
from ats.workspace_service import select_caller

RING_SECONDS = 20
RESERVATION_GRACE = timedelta(seconds=15)


def active_route(tenant_id, timestamp):
    return (
        InboundCallback.tenant_id == tenant_id,
        InboundCallback.released_at.is_(None),
        InboundCallback.expires_at > timestamp,
    )


def active_channel_units(db, tenant_id):
    outgoing = select(func.coalesce(func.sum(Attempt.channel_units), 0)).where(
        Attempt.tenant_id == tenant_id, Attempt.status.in_(ACTIVE),
    ).scalar_subquery()
    inbound = select(func.coalesce(func.sum(case(
        (InboundCallback.caller_number != "", 2), else_=1,
    )), 0)).where(*active_route(tenant_id, now())).scalar_subquery()
    return db.scalar(select(outgoing + inbound))


def choose_target(db, tenant, config, operator, call_flow, phone, destination):
    if call_flow == "browser_callback" and config.webrtc_enabled:
        device = available_browser_device(db, operator)
        if device:
            try:
                device = check_device(db, config, operator, device.agent_session_id)
            except Problem:
                pass
            else:
                return f"PJSIP/{device.endpoint}", device.agent_session_id, ""
    if call_flow != "mobile_callback" and operator.connection_method == "sip":
        if re.fullmatch(r"[0-9]{1,20}", operator.extension):
            return f"PJSIP/{operator.extension}", None, ""
        return None

    try:
        validate_mobile(config, operator, phone)
    except Problem:
        return None
    mobile = operator.mobile_phone
    if active_channel_units(db, tenant.id) + 2 > tenant.max_channels or mobile == normalize_phone(destination):
        return None
    # The target must never be one of our DIDs. It could reenter this same trunk.
    if db.scalar(select(CallerNumber.id).where(
        CallerNumber.tenant_id == tenant.id, CallerNumber.phone.in_([mobile, mobile[2:]]),
    )) or mobile[2:] == config.ami_caller_id:
        return None
    try:
        caller_number = select_caller(db, tenant, "multicom_ami", config, operator)
    except Problem:
        return None
    if not re.fullmatch(r"[0-9]{10}", caller_number):
        return None
    return f"PJSIP/8{mobile[2:]}@{config.ami_cli_endpoint_prefix}{caller_number}", None, caller_number


def reserve_route(db, tenant_id, external_id, raw_phone, destination):
    phone = normalize_phone(raw_phone)
    if not phone:
        return {}
    affinity = db.get(CallbackAffinity, (tenant_id, phone))
    if not affinity or affinity.expires_at <= now():
        return {}
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None:
        return {}
    config = settings_for_tenant(db, tenant_id)
    if tenant_id != config.telephony_tenant_id or not config.inbound_enabled or not config.live_calls_enabled:
        return {}
    # Re-read the affinity after the tenant lock. Expiry, deletion and a new
    # outgoing call can change it while the inbound channel is waiting.
    match = db.execute(select(CallbackAffinity, Operator).join(Operator, (
        Operator.tenant_id == CallbackAffinity.tenant_id
    ) & (Operator.id == CallbackAffinity.last_operator_id)).join(User, (
        User.tenant_id == Operator.tenant_id
    ) & (User.id == Operator.user_id)).where(
        CallbackAffinity.tenant_id == tenant_id, CallbackAffinity.phone == phone,
        CallbackAffinity.expires_at > now(), User.active, ~User.archived, User.role == "operator",
    ).with_for_update(of=Operator).execution_options(populate_existing=True)).first()
    if not match:
        return {}
    affinity, operator = match
    expire_routes(db, tenant_id)
    if operator.status != "free" or pending_calls(db, operator):
        return {}
    if active_channel_units(db, tenant_id) >= tenant.max_channels:
        return {}

    inbound = ingest_inbound(db, tenant_id, "multicom_ami", external_id, phone, destination, "ringing")
    if inbound.status != "ringing" or db.get(InboundCallback, inbound.id):
        return {}
    target = choose_target(db, tenant, config, operator, affinity.call_flow, phone, destination)
    if not target:
        return {}
    dial, session_id, caller_number = target
    token = secrets.token_hex(24) if session_id else None
    db.add(InboundCallback(
        tenant_id=tenant_id, inbound_id=inbound.id, operator_id=operator.id,
        caller_number=caller_number, browser_session_id=session_id, browser_token=token,
        expires_at=now() + timedelta(seconds=RING_SECONDS) + RESERVATION_GRACE,
    ))
    operator.status = "busy"
    return {
        "id": str(inbound.id), "dial": dial, "token": token or "", "caller_number": caller_number,
        "ring_seconds": RING_SECONDS, "talk_seconds": config.callback_max_seconds,
    }


def answer_route(db, route, config):
    timestamp = now()
    if route.released_at or route.expires_at <= timestamp or route.cancel_requested:
        return False
    if route.answered_at:
        return True
    inbound = db.get(InboundCall, route.inbound_id)
    if inbound.status != "ringing":
        return False
    route.answered_at = timestamp
    route.expires_at = timestamp + timedelta(seconds=config.callback_max_seconds) + RESERVATION_GRACE
    inbound.status, inbound.updated_at = "answered", timestamp
    return True


def release_route(db, route, *, hangup=False):
    if route.released_at:
        return
    timestamp = now()
    route.released_at = timestamp
    operator = db.scalar(select(Operator).where(
        Operator.tenant_id == route.tenant_id, Operator.id == route.operator_id,
    ).with_for_update().execution_options(populate_existing=True))
    if operator and operator.status == "busy" and not is_operator_reserved(db, operator) and not db.scalar(
        select(QueueEntry.id).where(
            QueueEntry.tenant_id == route.tenant_id, QueueEntry.operator_id == operator.id,
            QueueEntry.status == "accepted",
        ).limit(1)
    ):
        operator.status = "free"
    if route.answered_at or hangup:
        inbound = db.get(InboundCall, route.inbound_id)
        duration = int((timestamp - route.answered_at).total_seconds()) if route.answered_at else 0
        completed = route.answered_at or inbound.status == "answered"
        ingest_inbound(
            db, inbound.tenant_id, inbound.route, inbound.external_id, inbound.phone, inbound.destination,
            "completed" if completed else "failed", duration,
        )


def expire_routes(db, tenant_id):
    """Caller holds the tenant lock. Dial's deadline limits how long a channel can stay live."""
    for route in db.scalars(select(InboundCallback).where(
        InboundCallback.tenant_id == tenant_id, InboundCallback.released_at.is_(None),
        InboundCallback.expires_at <= now(),
    )).all():
        release_route(db, route)


def is_operator_reserved(db, operator):
    return bool(db.scalar(select(InboundCallback.inbound_id).where(
        *active_route(operator.tenant_id, now()), InboundCallback.operator_id == operator.id,
    ).limit(1)))


def browser_projection(db, route, inbound):
    timestamp = route.released_at or now()
    ended = bool(route.released_at or route.expires_at <= timestamp)
    duration = int((timestamp - route.answered_at).total_seconds()) if route.answered_at else 0
    contact = db.get(Contact, inbound.contact_id) if inbound.contact_id else None
    state = ("ended" if route.answered_at else "failed") if ended else (
        "connected" if route.answered_at else "ringing_operator"
    )
    return {
        "ok": True, "call_id": str(inbound.id), "mode": "browser", "state": state,
        "duration_sec": max(0, duration), "cancel_requested": route.cancel_requested,
        "reason": "operator_unavailable" if state == "failed" else "",
        "is_participant": True,
        "context": {
            "contact_id": contact.crm_contact_id if contact else None,
            "contact_name": contact.name if contact else inbound.phone,
            "phone": inbound.phone,
        },
    }
