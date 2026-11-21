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
from sqlalchemy import select

from ats.adapters import AmoCRM, Rejected, Retryable
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
from ats.workspace_service import audit

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


def owned_call(db, auth, ident, lock=False):
    query = select(WidgetCall).where(
        WidgetCall.id == ident, WidgetCall.tenant_id == auth.p.tenant_id,
        WidgetCall.operator_id == auth.operator_id,
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
    elif call.cancel_requested:
        state, reason = "ended", "cancelled_by_user"
    elif call.expires_at <= now():
        state, reason = "failed", "widget_queue_timeout"
    return {
        "ok": True, "call_id": str(call.id), "attempt_id": str(attempt.id) if attempt else None,
        "state": state, "reason": reason, "mode": call.mode,
        "duration_sec": max(0, int(((attempt.bridge_ended_at or now()) - attempt.bridged_at).total_seconds()))
        if attempt and attempt.bridged_at else 0,
    }


@router.get("/me")
def me(auth=Depends(widget_principal), db=Depends(session)):
    operator = db.get(Operator, auth.operator_id)
    current = db.scalar(select(WidgetCall).join(Campaign, Campaign.id == WidgetCall.campaign_id).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == operator.id,
        Campaign.status.in_(["draft", "running"]), ~Campaign.archived,
    ).order_by(WidgetCall.created_at.desc()).limit(1))
    device = device_for(db, auth.p.tenant_id, operator.id)
    phone = operator.mobile_phone
    return {"ok": True, "operator_id": str(operator.id), "operator_name": operator.name,
            "amo_user_id": auth.amo_user_id, "account_id": auth.account_id,
            "status": operator.status, "forward_number_masked": phone[:2] + "***" + phone[-4:] if phone else "",
            "forward_available": bool(phone), "browser_available": bool(settings().webrtc_enabled and device),
            "active_call": projection(db, current) if current else None}


@router.get("/webrtc/config")
def browser_config(auth=Depends(widget_principal), db=Depends(session)):
    config = settings_for_tenant(db, auth.p.tenant_id)
    operator = db.get(Operator, auth.operator_id)
    device = check_device(db, config, operator)
    return {"ok": True, "ready": True, "wss_url": config.webrtc_wss_url,
            "sip_uri": f"sip:{device.username}@{config.webrtc_sip_domain}",
            "auth_user": device.username, "sip_password": password_for(device),
            "ice_servers": ice_servers(config, operator),
            "library_url": config.public_origin.rstrip("/") + "/api/v1/amo/widget/assets/jssip.min.js"}


@router.post("/calls", status_code=202)
def create_call(data: CallCommand, auth=Depends(widget_principal), db=Depends(session)):
    config = settings_for_tenant(db, auth.p.tenant_id)
    try:
        phone = phone_from_import(data.phone)
    except ValueError as exc:
        raise Problem(422, "invalid_phone") from exc
    fingerprint = hashlib.sha256(json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(WidgetCall).where(
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == auth.operator_id,
        WidgetCall.client_request_id == data.client_request_id,
    ))
    if existing:
        if existing.request_hash != fingerprint:
            raise Problem(409, "idempotency_key_reused")
        return {**projection(db, existing), "operator_call_token": existing.operator_token}
    if data.amo_account and data.amo_account != config.amo_url.split("//")[-1].split(".")[0]:
        raise Problem(403, "amo_account_mismatch")
    # CRM reads happen before allocation/row locks. The signed user's identity is never taken from JSON.
    try:
        crm = AmoCRM(config)
        raw = crm.read(f"/api/v4/contacts/{data.crm_contact_id}")
        phones = set()
        for field in raw.get("custom_fields_values") or []:
            if field.get("field_code") != "PHONE":
                continue
            for value in field.get("values") or []:
                try:
                    phones.add(phone_from_import(str(value.get("value", ""))))
                except ValueError:
                    continue
        if phone not in phones:
            raise Problem(409, "contact_phone_mismatch")
        if data.entity_type != "contacts":
            if not data.entity_id:
                raise Problem(422, "entity_id_required")
            entity = crm.read(f"/api/v4/{data.entity_type}/{data.entity_id}", {"with": "contacts"})
            linked = entity.get("_embedded", {}).get("contacts", [])
            if data.crm_contact_id not in {int(row["id"]) for row in linked}:
                raise Problem(403, "contact_not_in_card")
        elif data.entity_id and data.entity_id != data.crm_contact_id:
            raise Problem(403, "contact_not_in_card")
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
        WidgetCall.tenant_id == auth.p.tenant_id, WidgetCall.operator_id == auth.operator_id,
        WidgetCall.client_request_id == data.client_request_id,
    ))
    if existing:
        if existing.request_hash != fingerprint:
            raise Problem(409, "idempotency_key_reused")
        return {**projection(db, existing), "operator_call_token": existing.operator_token}
    operator = db.scalar(select(Operator).where(Operator.id == auth.operator_id).with_for_update())
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
    contact = db.scalar(select(Contact).where(
        Contact.tenant_id == auth.p.tenant_id, Contact.crm_contact_id == data.crm_contact_id,
        Contact.phone == phone, ~Contact.archived,
    ))
    if not contact:
        raise Problem(409, "crm_contact_not_synced")
    if not contact.consent or contact.blocked:
        raise Problem(409, "no_consent")
    connection = "mobile" if data.mode == "forward" else "browser"
    validate_route(auth.p.tenant_id, "multicom_ami", db, mode="operator",
                   operator_id=operator.id, connection_method=connection)
    command = CampaignCreate(name=f"CRM call {contact.name}"[:120], route="multicom_ami",
                             contact_ids=[contact.id], mode="operator", retry_max=0,
                             max_channels=1, window_start=0, window_end=24)
    campaign, _ = create_campaign(db, auth.p, command, f"widget:{operator.id}:{data.client_request_id}")
    call = WidgetCall(tenant_id=auth.p.tenant_id, operator_id=operator.id, campaign_id=campaign.id,
                      client_request_id=data.client_request_id, request_hash=fingerprint, mode=data.mode,
                      operator_token=secrets.token_hex(24),
                      crm_context={"account_id": auth.account_id, "contact_id": data.crm_contact_id,
                                   "entity_type": data.entity_type, "entity_id": data.entity_id,
                                   "amo_user_id": auth.amo_user_id},
                      expires_at=now() + timedelta(seconds=config.widget_call_ttl_seconds))
    db.add(call)
    db.flush()
    campaign.status, campaign.schedule = "draft", {
        "operator_id": str(operator.id), "source": "amo_widget", "connection_method": connection,
        "widget_call_id": str(call.id), "amo_responsible_user_id": auth.amo_user_id,
    }
    audit(db, auth.p, "widget.call_requested", call.id)
    db.commit()
    return {**projection(db, call), "operator_call_token": call.operator_token}


@router.post("/calls/{ident}/start", status_code=202)
def activate(ident: UUID, auth=Depends(widget_principal), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    call = owned_call(db, auth, ident, lock=True)
    if call.cancel_requested or call.expires_at <= now():
        raise Problem(409, "call_request_expired")
    campaign = db.get(Campaign, call.campaign_id)
    if campaign.archived:
        return projection(db, call)
    campaign.status = "running"
    db.commit()
    return projection(db, call)


@router.get("/calls/{ident}")
def call_state(ident: UUID, auth=Depends(widget_principal), db=Depends(session)):
    return projection(db, owned_call(db, auth, ident))


@router.post("/calls/{ident}/ready")
def media_ready(ident: UUID, auth=Depends(widget_principal), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == auth.p.tenant_id).with_for_update())
    call = owned_call(db, auth, ident, lock=True)
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
