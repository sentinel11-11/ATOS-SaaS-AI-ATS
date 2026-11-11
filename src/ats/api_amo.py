"""Tenant scoped amoCRM import, preview, webhook and synchronization endpoints."""

import hashlib
import json
from datetime import datetime, time, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ats.adapters import AmoCRM, Rejected, Retryable
from ats.amo_context import contact_context, reference_data
from ats.config import settings
from ats.connections import settings_for_tenant
from ats.db import session
from ats.domain import Problem
from ats.models import (
    AmoSyncItem,
    AmoSyncJob,
    Audit,
    BlacklistEntry,
    ConnectionProfile,
    Contact,
    Inbox,
    LoginBucket,
    Operator,
    Outbox,
    WorkspaceSettings,
    now,
)
from ats.repositories import TenantRepository
from ats.schemas import CampaignCreate, phone_from_import
from ats.security import Principal, admin, principal
from ats.services import create_campaign
from ats.workspace_schemas import (
    AmoClickToCall,
    AmoSyncConfirm,
    AmoSyncPreview,
    AmoWidgetCard,
    AmoWidgetClickToCall,
)

router = APIRouter(prefix="/api/v1/workspace/amo", tags=["amocrm"])
public_router = APIRouter(tags=["amocrm"])


def _check_widget_origin(request: Request):
    origin = request.headers.get("origin", "")
    if not origin.endswith(".amocrm.ru") and origin != "https://amocrm.ru":
        raise Problem(403, "amo_widget_origin_rejected")


def _widget_phone(raw):
    for field in raw.get("custom_fields_values") or []:
        code = str(field.get("field_code") or field.get("code") or "").upper()
        name = str(field.get("field_name") or field.get("name") or "").lower()
        if code != "PHONE" and "телефон" not in name and "phone" not in name:
            continue
        for value in field.get("values") or []:
            phone = _phone((value or {}).get("value") if isinstance(value, dict) else value)
            if phone:
                return phone
    return _phone(raw.get("phone"))


@public_router.post("/api/v1/amo/widget/card")
def widget_card(data: AmoWidgetCard, request: Request, db=Depends(session)):
    """Resolve an amoCRM card server-side for shells without ``crm_get``."""
    _check_widget_origin(request)
    tenant_id = settings().amo_tenant_id
    if not tenant_id:
        raise Problem(503, "crm_tenant_not_bound")
    config = settings_for_tenant(db, tenant_id)
    if not config.crm_enabled or config.amo_tenant_id != tenant_id:
        raise Problem(409, "crm_tenant_not_bound")
    crm = AmoCRM(config)
    try:
        raw = crm.read(f"/api/v4/{data.entity_type}/{data.entity_id}", {"with": "contacts"})
        contact_id = data.entity_id if data.entity_type == "contacts" else None
        phone = _widget_phone(raw)
        if not contact_id:
            linked = raw.get("_embedded", {}).get("contacts", [])
            if linked:
                contact_id = int(linked[0].get("id") or linked[0].get("contact_id"))
        if contact_id and not phone and data.entity_type != "contacts":
            contact = crm.read(f"/api/v4/contacts/{contact_id}")
            phone = _widget_phone(contact)
        if not contact_id:
            raise Problem(404, "amo_card_contact_missing")
        if not phone:
            raise Problem(404, "amo_card_phone_missing")
    except (Rejected, Retryable) as exc:
        raise Problem(502, getattr(exc, "code", "crm_read_failed")) from exc
    return {
        "ok": True,
        "entity_type": data.entity_type,
        "entity_id": data.entity_id,
        "crm_contact_id": contact_id,
        "phone": phone,
        "responsible_user_id": raw.get("responsible_user_id"),
        "name": str(raw.get("name") or ""),
    }


@public_router.post("/api/v1/amo/widget/click-to-call")
def widget_click_to_call(data: AmoWidgetClickToCall, request: Request, db=Depends(session)):
    """CORS entry point used by the amoCRM widget for a single operator call."""
    _check_widget_origin(request)
    base = settings()
    tenant_id = base.amo_tenant_id
    if not tenant_id:
        raise Problem(503, "crm_tenant_not_bound")
    config = settings_for_tenant(db, tenant_id)
    if not config.crm_enabled or config.amo_tenant_id != tenant_id:
        raise Problem(409, "crm_tenant_not_bound")
    if data.amo_user_id:
        try:
            AmoCRM(config).read(f"/api/v4/users/{data.amo_user_id}")
        except (Rejected, Retryable) as exc:
            raise Problem(401, "amo_user_not_verified") from exc
    repo = TenantRepository(db, tenant_id)
    contact = db.scalar(repo.query(Contact).where(Contact.crm_contact_id == data.crm_contact_id)) if data.crm_contact_id else None
    if contact is None and data.phone:
        contact = db.scalar(repo.query(Contact).where(Contact.phone == _phone(data.phone), ~Contact.archived))
    if contact is None:
        try:
            raw = AmoCRM(config).read(f"/api/v4/contacts/{data.crm_contact_id}")
        except (Rejected, Retryable) as exc:
            raise Problem(502, getattr(exc, "code", "crm_read_failed")) from exc
        phone = _widget_phone(raw) or _phone(data.phone)
        if not phone:
            raise Problem(404, "amo_contact_phone_missing")
        contact = repo.add(
            Contact,
            name=str(raw.get("name") or "Новый контакт")[:120],
            phone=phone,
            source="amoCRM",
            consent=True,
            consent_source="amoCRM widget",
            crm_contact_id=data.crm_contact_id,
        )
        db.flush()
    if data.phone and _phone(data.phone) != contact.phone:
        raise Problem(409, "contact_phone_mismatch")
    provider = db.scalar(repo.query(WorkspaceSettings))
    values = provider.values if provider else {}
    configured_route = values.get("provider") or values.get("route") or "megafon_vats"
    route = data.route or {"multicom": "multicom_ami", "multicom_ami": "multicom_ami", "megafon_vats": "megafon_vats"}.get(configured_route, "megafon_vats")
    mapping = (provider.values if provider else {}).get("operator_user_map", {})
    profile = db.scalar(select(ConnectionProfile).where(ConnectionProfile.tenant_id == tenant_id, ConnectionProfile.provider == "amocrm"))
    if profile:
        mapping = {**mapping, **(profile.public_values or {}).get("operator_user_map", {})}
    mapped_extension = next((ext for ext, amo_id in mapping.items() if str(amo_id) == str(data.amo_user_id)), None) if data.amo_user_id else None
    operator_query = repo.query(Operator).where(Operator.status == "free")
    if route == "multicom_ami":
        operator_query = operator_query.where(Operator.extension != "")
    if mapped_extension:
        operator_query = operator_query.where(Operator.extension == str(mapped_extension))
    free_operators = db.scalars(operator_query.with_for_update()).all()
    if len(free_operators) != 1:
        if data.amo_user_id and not mapped_extension:
            raise Problem(409, "amo_user_operator_mapping_missing")
        raise Problem(409, "operator_selection_required")
    operator = free_operators[0]
    if operator is None:
        raise Problem(409, "operator_unavailable")
    command = CampaignCreate(name=f"CRM call {contact.name or contact.phone}", route=route, contact_ids=[contact.id], max_channels=1, window_start=0, window_end=24, mode="operator", retry_max=0)
    key = f"amo-widget:{data.request_id or uuid4()}"
    campaign, _ = create_campaign(db, Principal(tenant_id, operator.user_id, "operator", "amo-widget"), command, key)
    campaign.schedule = {"operator_id": str(operator.id), "source": "amo_widget"}
    campaign.status = "running"
    db.commit()
    return {"ok": True, "campaign_id": str(campaign.id), "contact_id": str(contact.id), "operator_id": str(operator.id), "route": route, "status": "queued"}


@router.post("/click-to-call")
def click_to_call(data: AmoClickToCall, p=Depends(principal), db=Depends(session)):
    """Start a one-contact operator call; the scheduler then uses MegaFon/Multicom."""
    repo = TenantRepository(db, p.tenant_id)
    contact = db.scalar(repo.query(Contact).where(Contact.crm_contact_id == data.crm_contact_id))
    if contact is None:
        raise Problem(404, "crm_contact_not_synced")
    if data.phone and _phone(data.phone) != contact.phone:
        raise Problem(409, "contact_phone_mismatch")
    command = CampaignCreate(
        name=f"CRM call {contact.name or contact.phone}",
        route="megafon_vats",
        contact_ids=[contact.id],
        max_channels=1,
        window_start=0,
        window_end=24,
        mode="operator",
        retry_max=0,
    )
    campaign, _ = create_campaign(db, p, command, f"amo-click:{uuid4()}")
    campaign.status = "running"
    db.commit()
    return {"ok": True, "campaign_id": str(campaign.id), "contact_id": str(contact.id), "status": "queued"}


def _crm(db, tenant_id):
    config = settings_for_tenant(db, tenant_id)
    if not config.crm_enabled or config.amo_tenant_id != tenant_id:
        raise Problem(409, "crm_tenant_not_bound")
    return AmoCRM(config)


def _phone(value):
    try:
        return phone_from_import(str(value or ""))
    except (TypeError, ValueError):
        return ""


def _item_json(item):
    return {
        "id": str(item.id),
        "crm_contact_id": item.crm_contact_id,
        "name": item.name,
        "phone": item.phone,
        "email": item.email,
        "group": item.group,
        "source": item.source,
        "action": item.action,
        "consent_required": not item.consent,
        "selected": item.selected,
        "applied": item.applied,
        "error": item.error,
        "crm_context": item.crm_context or {},
    }


def _merge_contacts(rows):
    merged = {}
    for row in rows:
        current = merged.get(row["crm_contact_id"])
        if not current:
            merged[row["crm_contact_id"]] = row
            continue
        for key in ("pipeline_ids", "status_ids", "tag_names"):
            current[key] = list(dict.fromkeys(current.get(key, []) + row.get(key, [])))
        current["pipeline_stages"] = list({
            (pair["pipeline_id"], pair["stage_id"]): pair
            for pair in current.get("pipeline_stages", []) + row.get("pipeline_stages", [])
            if pair.get("pipeline_id") and pair.get("stage_id")
        }.values())
        if not current.get("responsible_user_id"):
            current["responsible_user_id"] = row.get("responsible_user_id")
    return list(merged.values())


@router.post("/sync/preview")
def sync_preview(data: AmoSyncPreview, p=Depends(admin), db=Depends(session)):
    crm = _crm(db, p.tenant_id)
    try:
        if data.contact_ids:
            raw_contacts = crm.contacts(ids=data.contact_ids, limit=min(data.limit, 250), enrich_missing=True)
        else:
            raw_contacts = []
            created_from = int(datetime.combine(data.created_from, time.min, tzinfo=timezone.utc).timestamp()) if data.created_from else None
            created_to = int(datetime.combine(data.created_to, time.max, tzinfo=timezone.utc).timestamp()) if data.created_to else None
            ranges = [(created_from, created_to, None, None)]
            if created_from or created_to:
                ranges.append((None, None, created_from, created_to))
            if (data.pipeline_id or data.status_id) and (created_from or created_to):
                # A lead created before the selected window can still be active
                # in the selected funnel/stage during that window.
                ranges.append((None, created_to, None, None))
            for range_created_from, range_created_to, updated_from, updated_to in ranges:
                for page in range(1, 101):
                    batch = crm.lead_contacts(
                        page=page,
                        limit=250,
                        created_from=range_created_from,
                        created_to=range_created_to,
                        updated_from=updated_from,
                        updated_to=updated_to,
                        pipeline_id=data.pipeline_id,
                        status_id=data.status_id,
                    )
                    raw_contacts.extend(batch)
                    if getattr(crm, "last_lead_page_count", len(batch)) < 250:
                        break
            raw_contacts = _merge_contacts(raw_contacts)[: data.limit]
        if data.pipeline_id is not None:
            raw_contacts = [item for item in raw_contacts if data.pipeline_id in item.get("pipeline_ids", []) and (data.status_id is None or data.status_id in item.get("status_ids", []))]
    except (Rejected, Retryable) as exc:
        raise Problem(502, getattr(exc, "code", "crm_read_failed")) from exc
    try:
        pipelines, statuses, users = reference_data(crm, raw_contacts)
    except (Rejected, Retryable) as exc:
        raise Problem(502, getattr(exc, "code", "crm_reference_read_failed")) from exc
    job = AmoSyncJob(tenant_id=p.tenant_id, mode=data.mode, status="preview", counts={})
    db.add(job)
    db.flush()
    repo = TenantRepository(db, p.tenant_id)
    seen_phones = set()
    seen_crm_ids = set()
    counts = {"create": 0, "update": 0, "unchanged": 0, "duplicate_phone": 0, "duplicate_crm_id": 0, "invalid_phone": 0, "archive": 0}
    for raw in raw_contacts:
        phone = _phone(raw.get("phone"))
        crm_id = int(raw["crm_contact_id"])
        action, existing, error = "create", None, ""
        if crm_id in seen_crm_ids:
            action, error = "duplicate_crm_id", "duplicate_crm_id"
        else:
            seen_crm_ids.add(crm_id)
        if action == "duplicate_crm_id":
            pass
        elif raw.get("archived"):
            existing = db.scalar(repo.query(Contact).where(Contact.crm_contact_id == crm_id))
            action = "archive" if existing else "unchanged"
        elif not phone:
            action, error = "invalid_phone", "invalid_phone"
        elif phone in seen_phones:
            action, error = "duplicate_phone", "duplicate_phone"
        else:
            seen_phones.add(phone)
            existing = db.scalar(repo.query(Contact).where(Contact.crm_contact_id == crm_id))
            if existing is None:
                existing = db.scalar(repo.query(Contact).where(Contact.phone == phone, ~Contact.archived))
            phone_owner = db.scalar(repo.query(Contact).where(Contact.phone == phone, ~Contact.archived))
            if phone_owner is not None and (existing is None or phone_owner.id != existing.id):
                action, error = "duplicate_phone", "duplicate_phone"
            if existing:
                if action != "duplicate_phone":
                    action = "update" if (
                        existing.archived
                        or existing.name != raw.get("name", "")
                        or existing.phone != phone
                        or existing.email != raw.get("email", "")
                    ) else "unchanged"
        counts[action] = counts.get(action, 0) + 1
        item = AmoSyncItem(
            tenant_id=p.tenant_id,
            job_id=job.id,
            crm_contact_id=crm_id,
            name=str(raw.get("name") or "")[:120],
            phone=phone,
            email=str(raw.get("email") or "")[:254],
            group=str(raw.get("group") or "")[:120],
            source="amoCRM",
            action=action,
            consent=True,
            contact_id=existing.id if existing else None,
            error=error,
            crm_context=contact_context(raw, pipelines, statuses, users),
            selected=action in {"create", "update", "unchanged", "archive"},
        )
        db.add(item)
    job.counts = counts
    job.updated_at = now()
    db.add(Audit(tenant_id=p.tenant_id, actor_id=p.user_id, action="amo.sync.preview", entity_id=job.id))
    db.commit()
    return {"job_id": str(job.id), "status": job.status, "counts": counts, "items": [_item_json(i) for i in db.scalars(select(AmoSyncItem).where(AmoSyncItem.job_id == job.id)).all()]}

@router.get("/pipelines")
def pipelines(p=Depends(admin), db=Depends(session)):
    try:
        return {"pipelines": _crm(db, p.tenant_id).pipelines()}
    except (Rejected, Retryable) as exc:
        raise Problem(502, getattr(exc, "code", "crm_read_failed")) from exc

@router.get("/pipelines/{pipeline_id}/statuses")
def pipeline_statuses(pipeline_id: int, p=Depends(admin), db=Depends(session)):
    try:
        return {"statuses": _crm(db, p.tenant_id).pipeline_statuses(pipeline_id)}
    except (Rejected, Retryable) as exc:
        raise Problem(502, getattr(exc, "code", "crm_read_failed")) from exc


@router.post("/sync/run")
def sync_run(p=Depends(admin), db=Depends(session)):
    """Queue a repeatable full synchronization for the scheduler/worker."""
    job = AmoSyncJob(tenant_id=p.tenant_id, mode="scheduled", status="queued", counts={})
    db.add(job)
    db.flush()
    db.add(Outbox(tenant_id=p.tenant_id, kind="crm_sync", dedupe_key=f"amo-sync:{job.id}", payload={"job_id": str(job.id)}))
    db.commit()
    return {"job_id": str(job.id), "status": job.status}


@router.get("/sync/{job_id}")
def sync_status(job_id: UUID, p=Depends(principal), db=Depends(session)):
    job = TenantRepository(db, p.tenant_id).get(AmoSyncJob, job_id)
    items = db.scalars(select(AmoSyncItem).where(AmoSyncItem.tenant_id == p.tenant_id, AmoSyncItem.job_id == job.id)).all()
    return {"job_id": str(job.id), "status": job.status, "mode": job.mode, "counts": job.counts, "error": job.error, "items": [_item_json(i) for i in items]}


@router.post("/sync/{job_id}/confirm")
def sync_confirm(job_id: UUID, data: AmoSyncConfirm, p=Depends(admin), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    job = repo.get(AmoSyncJob, job_id, lock=True)
    if job.status != "preview":
        raise Problem(409, "amo_sync_already_confirmed")
    items = list(db.scalars(select(AmoSyncItem).where(AmoSyncItem.tenant_id == p.tenant_id, AmoSyncItem.job_id == job.id, AmoSyncItem.id.in_(data.item_ids))).all())
    if len(items) != len(set(data.item_ids)):
        raise Problem(404, "amo_sync_item_not_found")
    for item in items:
        if item.action in {"invalid_phone", "duplicate_phone", "duplicate_crm_id"}:
            continue
        if item.action == "unchanged":
            if item.contact_id:
                existing = repo.get(Contact, item.contact_id, lock=True)
                existing.crm_context = item.crm_context or {}
                item.applied = True
            continue
        if item.action == "archive":
            if item.contact_id:
                row = repo.get(Contact, item.contact_id, lock=True)
                row.archived, row.blocked, row.consent = True, True, False
                item.applied = True
            continue
        existing = repo.get(Contact, item.contact_id, lock=True) if item.contact_id else None
        if existing is None:
            blocked = db.scalar(repo.query(BlacklistEntry).where(BlacklistEntry.phone == item.phone)) is not None
            existing = repo.add(Contact, name=item.name, phone=item.phone, email=item.email, group=item.group, source=item.source, consent=True, consent_source=data.consent_source, crm_contact_id=item.crm_contact_id, crm_context=item.crm_context, blocked=blocked)
        else:
            existing.name, existing.phone, existing.email, existing.group = item.name, item.phone, item.email, item.group
            existing.source, existing.crm_contact_id = item.source, item.crm_contact_id
            existing.crm_context = item.crm_context or {}
            # Preserve an explicit opt-out on an existing contact.
            if existing.consent:
                existing.consent_source = data.consent_source
            existing.archived = False
        item.contact_id, item.applied, item.consent = existing.id, True, existing.consent
    job.status, job.updated_at = "completed", now()
    db.add(Audit(tenant_id=p.tenant_id, actor_id=p.user_id, action="amo.sync.confirm", entity_id=job.id))
    db.commit()
    return {"job_id": str(job.id), "status": job.status, "applied": sum(1 for i in items if i.applied)}


@router.post("/webhook/{tenant_id}", include_in_schema=False)
async def webhook(tenant_id: UUID, request: Request, x_amo_webhook_token: str | None = Header(default=None), db=Depends(session)):
    try:
        base_config = settings()
        tenant_config = base_config if base_config.amo_tenant_id == tenant_id else settings_for_tenant(db, tenant_id)
        expected = tenant_config.amo_webhook_token.get_secret_value()
        bound = tenant_config.amo_tenant_id == tenant_id and tenant_config.crm_enabled
    except Problem:
        expected, bound = "", False
    if not bound or not expected or not x_amo_webhook_token or not secrets_compare(x_amo_webhook_token, expected):
        raise Problem(401, "amo_webhook_unauthorized")
    bucket_key = f"amo-webhook:{tenant_id}:{now().strftime('%Y%m%d%H%M')}"
    bucket = db.scalar(select(LoginBucket).where(LoginBucket.key == bucket_key).with_for_update())
    if bucket is None:
        bucket = LoginBucket(key=bucket_key, count=1, reset_at=now() + timedelta(minutes=1))
        db.add(bucket)
    else:
        bucket.count += 1
        if bucket.count > 120:
            db.rollback()
            raise Problem(429, "amo_webhook_rate_limited")
    try:
        payload = await request.json()
    except ValueError:
        try:
            payload = dict(await request.form())
        except (ValueError, RuntimeError):
            raise Problem(400, "invalid_webhook_payload") from None
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    event_key = hashlib.sha256(raw.encode()).hexdigest()
    result = db.execute(insert(Inbox).values(id=uuid4(), tenant_id=tenant_id, route="amocrm", event_key=event_key, external_id=event_key, event="contact_changed", payload=payload, status="pending", received_at=now()).on_conflict_do_nothing())
    if result.rowcount:
        job = AmoSyncJob(tenant_id=tenant_id, mode="webhook", status="queued", counts={})
        db.add(job)
        db.flush()
        db.add(Outbox(tenant_id=tenant_id, kind="crm_sync", dedupe_key=f"amo-webhook:{event_key}", payload={"job_id": str(job.id), "event_key": event_key, "payload": payload, "mode": "webhook"}))
    db.commit()
    return {"accepted": True, "duplicate": not bool(result.rowcount)}


def secrets_compare(left, right):
    import hmac
    return hmac.compare_digest(left.encode(), right.encode())


public_router.add_api_route(
    "/webhooks/amocrm/{tenant_id}", webhook, methods=["POST"], include_in_schema=False
)
