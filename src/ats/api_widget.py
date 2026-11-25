"""The signed protocol used by the moderated amoCRM widget (forward + WebRTC)."""

import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from ats.adapters import AmoCRM, Rejected, Retryable
from ats.amo_context import contact_context
from ats.config import settings
from ats.connections import settings_for_tenant
from ats.db import session
from ats.domain import ACTIVE, Problem
from ats.models import (
    Attempt,
    Campaign,
    CampaignItem,
    Contact,
    Operator,
    Tenant,
    WidgetCall,
    now,
)
from ats.schemas import CampaignCreate, phone_from_import
from ats.services import create_campaign, finish_attempt, validate_route
from ats.webrtc import check_device, device_for, ice_servers, password_for
from ats.widget_auth import widget_principal
from ats.widget_context import browser_online, manager_context, read_call_card, read_responsible
from ats.workspace_schemas import ContactEdit
from ats.workspace_service import admit_crm_contact, audit, save_contact

router = APIRouter(prefix="/api/v1/amo/widget/v2", tags=["signed amoCRM widget"])
assets_router = APIRouter(tags=["widget assets"])


@assets_router.get("/api/v1/amo/widget/assets/jssip.min.js", include_in_schema=False)
def sip_library():
    path = Path(__file__).resolve().parents[2] / "amo-widget/vendor/jssip.min.js"
    if not path.is_file():
        raise Problem(503, "widget_library_not_built")
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "public, max-age=3600"})


class CallCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(max_length=32)
    crm_contact_id: int = Field(gt=0)
    mode: Literal["forward", "browser"]
    client_request_id: UUID
    amo_account: str = Field(default="", max_length=100)
    entity_type: Literal["contacts", "leads", "companies"] = "contacts"
    entity_id: int | None = Field(default=None, gt=0)
    protocol_version: int = Field(default=2, ge=2, le=3)


class CardCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: Literal["contacts", "leads", "companies"]
    entity_id: int = Field(gt=0)


class AgentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    registered: bool = False


class InviteCommand(AgentCommand):
    token: str = Field(pattern=r"^[a-f0-9]{48}$")


class ReadyCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID | None = None
    token: str = Field(default="", max_length=128)


class AvailabilityCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["free", "break"]


def owned_call(db, auth, ident, lock=False, participant=False, initiator=False):
    allowed = or_(WidgetCall.operator_id == auth.operator_id,
                  WidgetCall.initiator_user_id == auth.p.user_id)
    if participant:
        allowed = WidgetCall.operator_id == auth.operator_id
    if initiator:
        allowed = WidgetCall.initiator_user_id == auth.p.user_id
    query = select(WidgetCall).where(
        WidgetCall.id == ident, WidgetCall.tenant_id == auth.p.tenant_id,
        allowed,
    )
    call = db.scalar(query.with_for_update() if lock else query)
    if not call:
        raise Problem(404, "not_found")
    return call


def attempt_for(db, call):
    return db.scalar(select(Attempt).join(CampaignItem, CampaignItem.id == Attempt.item_id).where(
        Attempt.tenant_id == call.tenant_id, CampaignItem.campaign_id == call.campaign_id,
    ).order_by(Attempt.ordinal.desc()).limit(1))


def projection(db, call):
    attempt = attempt_for(db, call)
    state, reason = "queued", ""
    if attempt:
        reason = attempt.reason
        if attempt.status == "completed":
            state = "ended"
        elif attempt.status == "failed":
            state = "failed"
        elif attempt.status == "unknown":
            state = "reconciliation_required"
        elif attempt.bridged_at:
            state = "connected"
        else:
            from ats.models import CallLeg

            customer = db.scalar(select(CallLeg).where(CallLeg.attempt_id == attempt.id, CallLeg.role == "customer"))
            state = "ringing_customer" if customer and customer.channel else "ringing_operator"
    elif (call.crm_context or {}).get("failure_reason"):
        state, reason = "failed", call.crm_context["failure_reason"]
    elif call.cancel_requested:
        state, reason = "ended", "cancelled_by_user"
    elif call.expires_at <= now():
        state, reason = "failed", "widget_queue_timeout"
    operator = db.get(Operator, call.operator_id)
    context = call.crm_context or {}
    return {
        "ok": True, "call_id": str(call.id), "attempt_id": str(attempt.id) if attempt else None,
        "state": state, "reason": reason, "mode": call.mode,
        "cancel_requested": call.cancel_requested,
        "duration_sec": max(0, int(((attempt.bridge_ended_at or now()) - attempt.bridged_at).total_seconds()))
        if attempt and attempt.bridged_at and not (attempt.agent_result or {}).get("duration_unconfirmed") else 0,
        "participant": {
            "operator_id": str(call.operator_id),
            "amo_user_id": context.get("responsible_user_id") or context.get("amo_user_id"),
            "operator_name": context.get("responsible_name") or operator.name,
            "forward_number_masked": context.get("forward_number_masked", ""),
        },
        "context": {key: context.get(key) for key in (
            "entity_type", "entity_id", "contact_id", "phone", "contact_name", "initiator_name",
        )},
    }


def call_response(db, call, auth):
    result = projection(db, call)
    result["is_participant"] = call.operator_id == auth.operator_id
    if call.browser_session_id is None and call.operator_id == auth.operator_id:
        result["operator_call_token"] = call.operator_token
    return result


def fail_unstarted_call(db, call, reason):
    attempt = attempt_for(db, call)
    if attempt:
        if attempt.status != "prepared":
            raise Problem(409, "call_already_started")
        finish_attempt(db, attempt, "failed", reason)
    else:
        call.crm_context = {**call.crm_context, "failure_reason": reason}
        campaign = db.get(Campaign, call.campaign_id)
        campaign.status, campaign.archived = "completed", True
        for item in db.scalars(select(CampaignItem).where(CampaignItem.campaign_id == campaign.id)):
            item.status = "failed"


@router.post("/context")
def card_context(data: CardCommand, auth=Depends(widget_principal), db=Depends(session)):
    config = settings_for_tenant(db, auth.p.tenant_id)
    crm = None
    try:
        crm = AmoCRM(config)
        _, responsible_id = read_responsible(crm, data.entity_type, data.entity_id)
        _, manager = manager_context(db, config, auth.p.tenant_id, responsible_id, crm)
        return {"ok": True, "entity_type": data.entity_type, "entity_id": data.entity_id,
                "responsible": manager, "is_self": manager["operator_id"] == str(auth.operator_id)}
    except (Rejected, Retryable) as exc:
        raise Problem(502, "crm_read_failed") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise Problem(502, "crm_context_invalid") from exc
    finally:
        if crm:
            crm.close()


@router.get("/me")
def me(auth=Depends(widget_principal), db=Depends(session)):
    operator = db.get(Operator, auth.operator_id)
    current = db.scalar(select(WidgetCall).join(Campaign, Campaign.id == WidgetCall.campaign_id).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == operator.id,
        Campaign.status.in_(["draft", "running"]), ~Campaign.archived,
    ).order_by(WidgetCall.created_at.desc()).limit(1))
    initiated = db.scalar(select(WidgetCall).join(Campaign, Campaign.id == WidgetCall.campaign_id).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.initiator_user_id == auth.p.user_id,
        Campaign.status.in_(["draft", "running"]), ~Campaign.archived,
    ).order_by(WidgetCall.created_at.desc()).limit(1))
    device = device_for(db, auth.p.tenant_id, operator.id)
    phone = operator.mobile_phone
    return {"ok": True, "operator_id": str(operator.id), "operator_name": operator.name,
            "amo_user_id": auth.amo_user_id, "account_id": auth.account_id,
            "status": operator.status, "forward_number_masked": phone[:2] + "***" + phone[-4:] if phone else "",
            "forward_available": bool(phone), "browser_available": bool(settings().webrtc_enabled and device),
            "active_call": call_response(db, current, auth) if current else None,
            "initiated_call": call_response(db, initiated, auth) if initiated else None,
            "protocol_version": 3}


@router.get("/webrtc/config")
def browser_config(session_id: UUID | None = None, auth=Depends(widget_principal), db=Depends(session)):
    config = settings_for_tenant(db, auth.p.tenant_id)
    operator = db.get(Operator, auth.operator_id)
    device = check_device(db, config, operator)
    if session_id is not None and (device.agent_session_id != session_id
                                  or not device.agent_expires_at or device.agent_expires_at <= now()):
        raise Problem(409, "agent_session_lost")
    return {"ok": True, "ready": True, "wss_url": config.webrtc_wss_url,
            "sip_uri": f"sip:{device.username}@{config.webrtc_sip_domain}",
            "auth_user": device.username, "sip_password": password_for(device),
            "ice_servers": ice_servers(config, operator),
            "library_url": config.public_origin.rstrip("/") + "/api/v1/amo/widget/assets/jssip.min.js"}


def require_agent(db, auth, session_id):
    device = device_for(db, auth.p.tenant_id, auth.operator_id)
    if (not session_id or not device or device.agent_session_id != session_id
        or not device.agent_expires_at or device.agent_expires_at <= now()):
        raise Problem(409, "agent_session_lost")
    return device


@router.post("/agent/lease")
def agent_lease(data: AgentCommand, auth=Depends(widget_principal), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    operator = db.get(Operator, auth.operator_id)
    device = check_device(db, settings_for_tenant(db, auth.p.tenant_id), operator)
    if device.agent_session_id != data.session_id:
        if (device.agent_expires_at and device.agent_expires_at > now()) or operator.status == "busy":
            raise Problem(409, "agent_in_another_tab")
        # A new page must never answer a request bound to a former page's nonce.
        pending = db.scalars(select(WidgetCall).join(Campaign, Campaign.id == WidgetCall.campaign_id).where(
            WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == operator.id,
            WidgetCall.browser_session_id.is_not(None), ~Campaign.archived,
            Campaign.status.in_(["draft", "running"]),
        )).all()
        for call in pending:
            fail_unstarted_call(db, call, "responsible_browser_offline")
        device.agent_session_id, device.agent_registered = data.session_id, False
    device.agent_expires_at = now() + timedelta(seconds=90)
    db.commit()
    return {"ok": True}


@router.post("/agent/heartbeat")
def agent_heartbeat(data: AgentCommand, auth=Depends(widget_principal), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    device = require_agent(db, auth, data.session_id)
    device.agent_expires_at, device.agent_registered = now() + timedelta(seconds=90), data.registered
    operator = db.get(Operator, auth.operator_id)
    if data.registered and operator.status == "offline":
        operator.status = "free"
    db.commit()
    return {"ok": True, "status": operator.status}


@router.get("/agent/calls")
def agent_calls(session_id: UUID, auth=Depends(widget_principal), db=Depends(session)):
    require_agent(db, auth, session_id)
    calls = db.scalars(select(WidgetCall).join(Campaign, Campaign.id == WidgetCall.campaign_id).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == auth.operator_id,
        WidgetCall.browser_session_id == session_id, ~Campaign.archived,
        Campaign.status.in_(["draft", "running"]), ~WidgetCall.cancel_requested,
    )).all()
    return {"ok": True, "calls": [{**projection(db, call), "operator_call_token": call.operator_token}
                                  for call in calls]}


@router.post("/agent/invite")
def agent_invite(data: InviteCommand, auth=Depends(widget_principal), db=Depends(session)):
    require_agent(db, auth, data.session_id)
    call = db.scalar(select(WidgetCall).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == auth.operator_id,
        WidgetCall.browser_session_id == data.session_id, WidgetCall.operator_token == data.token,
        ~WidgetCall.cancel_requested,
    ))
    if not call or projection(db, call)["state"] in {"ended", "failed", "reconciliation_required"}:
        raise Problem(403, "browser_call_mismatch")
    return {**projection(db, call), "operator_call_token": call.operator_token}


@router.post("/agent/availability")
def availability(data: AvailabilityCommand, auth=Depends(widget_principal), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    operator = db.get(Operator, auth.operator_id)
    if operator.status == "busy":
        raise Problem(409, "operator_unavailable")
    operator.status = data.status
    audit(db, auth.p, "operator.availability_changed", operator.id)
    db.commit()
    return {"ok": True, "status": data.status}


@router.post("/calls", status_code=202)
def create_call(data: CallCommand, auth=Depends(widget_principal), db=Depends(session)):
    config = settings_for_tenant(db, auth.p.tenant_id)
    try:
        phone = phone_from_import(data.phone)
    except ValueError as exc:
        raise Problem(422, "invalid_phone") from exc
    request_data = data.model_dump(mode="json")
    if data.protocol_version == 2:
        request_data.pop("protocol_version")  # Preserve retries made by the installed 5.2 widget.
    fingerprint = hashlib.sha256(json.dumps(request_data, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(WidgetCall).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.initiator_user_id == auth.p.user_id,
        WidgetCall.client_request_id == data.client_request_id,
    ))
    if existing:
        if existing.request_hash != fingerprint:
            raise Problem(409, "idempotency_key_reused")
        return call_response(db, existing, auth)
    if data.amo_account and data.amo_account != config.amo_url.split("//")[-1].split(".")[0]:
        raise Problem(403, "amo_account_mismatch")
    # CRM reads happen before allocation/row locks. The signed user's identity is never taken from JSON.
    try:
        crm = AmoCRM(config)
        _, responsible_id, imported = read_call_card(
            crm, data.entity_type, data.entity_id, data.crm_contact_id, phone,
        )
        target, manager = manager_context(db, config, auth.p.tenant_id, responsible_id, crm)
        if target is None:
            raise Problem(409, "responsible_mapping_missing")
        target_id = target.id
        if data.protocol_version == 2 and target_id != auth.operator_id:
            raise Problem(409, "widget_upgrade_required")
    except (Rejected, Retryable) as exc:
        raise Problem(502, "crm_read_failed") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise Problem(422, "crm_phone_invalid") from exc
    finally:
        if "crm" in locals():
            crm.close()
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    # Recheck the request key under the same mutex used by scheduling.
    existing = db.scalar(select(WidgetCall).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.initiator_user_id == auth.p.user_id,
        WidgetCall.client_request_id == data.client_request_id,
    ))
    if existing:
        if existing.request_hash != fingerprint:
            raise Problem(409, "idempotency_key_reused")
        # Release the tenant mutex before request logging uses a separate transaction.
        db.commit()
        return call_response(db, existing, auth)
    operator = db.scalar(select(Operator).where(Operator.id == target_id).with_for_update())
    if db.scalar(select(WidgetCall.id).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == target_id,
        WidgetCall.client_request_id == data.client_request_id,
    )):
        raise Problem(409, "idempotency_key_reused")
    pending = db.scalar(select(WidgetCall).join(Campaign, Campaign.id == WidgetCall.campaign_id).where(
        WidgetCall.operator_id == operator.id, Campaign.status.in_(["draft", "running"]), ~Campaign.archived,
    ).order_by(WidgetCall.created_at.desc()).limit(1))
    if pending and pending.expires_at <= now() and not attempt_for(db, pending):
        expired = db.get(Campaign, pending.campaign_id)
        expired.status, expired.archived = "completed", True
        pending.cancel_requested = True
        pending = None
    if operator.status != "free" or pending:
        raise Problem(409, "operator_unavailable")
    if data.mode == "forward" and not operator.mobile_phone:
        raise Problem(409, "forward_number_not_set")
    device = device_for(db, auth.p.tenant_id, operator.id) if data.mode == "browser" else None
    if data.mode == "browser" and data.protocol_version == 3 and not browser_online(device):
        raise Problem(409, "responsible_browser_offline")
    contact = db.scalar(select(Contact).where(
        Contact.tenant_id == auth.p.tenant_id, Contact.phone == phone,
    ).with_for_update())
    if contact is None:
        contact = save_contact(db, auth.p, ContactEdit(
            name=imported["name"] or "Без имени", phone=phone,
            email=imported["email"], group=imported["group"],
            crm_contact_id=data.crm_contact_id, consent=True, consent_source="amoCRM",
        ))
        contact.source = "amoCRM"
        contact.tags = imported["tag_names"]
        contact.crm_context = contact_context(imported, {}, {}, {})
    admit_crm_contact(db, contact, auth.p.user_id, restore_for_call=True)
    if contact.blocked:
        raise Problem(409, "contact_blacklisted")
    if contact.crm_contact_id is None:
        # The live CRM contact, selected phone and card link were verified above.
        # Bind this admitted local number without rewriting another contact's identity.
        contact.crm_contact_id = data.crm_contact_id
        audit(db, auth.p, "contact.crm_linked", contact.id)
    connection = "mobile" if data.mode == "forward" else "browser"
    validate_route(auth.p.tenant_id, "multicom_ami", db, mode="operator",
                   operator_id=operator.id, connection_method=connection)
    command = CampaignCreate(name=f"CRM call {contact.name}"[:120], route="multicom_ami",
                             contact_ids=[contact.id], mode="operator", retry_max=0,
                             max_channels=1, window_start=0, window_end=24)
    campaign, _ = create_campaign(db, auth.p, command, f"widget:{operator.id}:{data.client_request_id}")
    call = WidgetCall(tenant_id=auth.p.tenant_id, operator_id=operator.id, campaign_id=campaign.id,
                      initiator_user_id=auth.p.user_id,
                      browser_session_id=device.agent_session_id if device and data.protocol_version == 3 else None,
                      client_request_id=data.client_request_id, request_hash=fingerprint, mode=data.mode,
                      operator_token=secrets.token_hex(24),
                      crm_context={"account_id": auth.account_id, "contact_id": data.crm_contact_id,
                                   "entity_type": data.entity_type, "entity_id": data.entity_id,
                                   "amo_user_id": auth.amo_user_id, "phone": phone,
                                   "initiator_ats_user_id": str(auth.p.user_id),
                                   "initiator_name": db.get(Operator, auth.operator_id).name,
                                   "responsible_user_id": responsible_id,
                                   "responsible_name": manager["operator_name"],
                                   "forward_number_masked": manager["forward_number_masked"],
                                   "protocol_version": data.protocol_version,
                                   "browser_session_id": str(device.agent_session_id)
                                   if device and data.protocol_version == 3 else None,
                                   "contact_name": imported["name"] or contact.name},
                      expires_at=now() + timedelta(seconds=config.widget_call_ttl_seconds))
    db.add(call)
    db.flush()
    campaign.status, campaign.schedule = "draft", {
        "operator_id": str(operator.id), "source": "amo_widget", "connection_method": connection,
        "widget_call_id": str(call.id), "amo_responsible_user_id": responsible_id,
    }
    audit(db, auth.p, "widget.call_requested", call.id)
    db.commit()
    return call_response(db, call, auth)


@router.post("/calls/{ident}/start", status_code=202)
def activate(ident: UUID, auth=Depends(widget_principal), db=Depends(session)):
    call = owned_call(db, auth, ident, initiator=True)
    campaign = db.get(Campaign, call.campaign_id)
    if campaign.archived or campaign.status == "running":
        return call_response(db, call, auth)
    from ats.widget_context import verify_responsible

    try:
        verify_responsible(db, call, settings_for_tenant(db, auth.p.tenant_id), AmoCRM)
    except (Rejected, Retryable) as exc:
        raise Problem(502, "crm_read_failed") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise Problem(502, "crm_context_invalid") from exc
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    call = owned_call(db, auth, ident, lock=True, initiator=True)
    if call.cancel_requested or call.expires_at <= now():
        raise Problem(409, "call_request_expired")
    campaign = db.get(Campaign, call.campaign_id)
    if campaign.archived:
        db.commit()
        return projection(db, call)
    campaign.status = "running"
    db.commit()
    return call_response(db, call, auth)


@router.get("/calls/{ident}")
def call_state(ident: UUID, auth=Depends(widget_principal), db=Depends(session)):
    return call_response(db, owned_call(db, auth, ident), auth)


@router.post("/calls/{ident}/ready")
def media_ready(ident: UUID, data: ReadyCommand = ReadyCommand(), auth=Depends(widget_principal), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    call = owned_call(db, auth, ident, lock=True, participant=True)
    if call.browser_session_id:
        require_agent(db, auth, data.session_id)
        if call.browser_session_id != data.session_id or not secrets.compare_digest(call.operator_token, data.token):
            raise Problem(403, "browser_call_mismatch")
    if (call.mode != "browser" or call.cancel_requested or call.expires_at <= now()
        or projection(db, call)["state"] in {"ended", "failed", "reconciliation_required"}):
        raise Problem(409, "call_not_ready")
    call.media_ready_at = call.media_ready_at or now()
    attempt = attempt_for(db, call)
    if attempt and attempt.manager_confirmed_at and attempt.status in ACTIVE:
        from ats.callbacks import queue_authorization

        queue_authorization(db, attempt)
    db.commit()
    return {"ok": True}


@router.post("/calls/{ident}/hangup", status_code=202)
def hangup(ident: UUID, auth=Depends(widget_principal), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    call = owned_call(db, auth, ident, lock=True)
    call.cancel_requested = True
    attempt = attempt_for(db, call)
    campaign = db.get(Campaign, call.campaign_id)
    if not attempt:
        campaign.status, campaign.archived = "draft", True
        for item in db.scalars(select(CampaignItem).where(CampaignItem.campaign_id == campaign.id)):
            item.status = "cancelled"
    elif attempt.status == "prepared":
        finish_attempt(db, attempt, "failed", "cancelled_by_user")
    elif attempt.status in ACTIVE:
        from ats.callbacks import queue_hangup

        queue_hangup(db, attempt)
    audit(db, auth.p, "widget.hangup_requested", call.id)
    db.commit()
    return {"ok": True, "state": "ending"}
