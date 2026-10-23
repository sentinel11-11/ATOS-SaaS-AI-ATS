"""Original campaign actions translated to the new transactional call engine."""

import re
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from ats.compat_contacts import legacy_call
from ats.db import session
from ats.domain import Problem
from ats.models import (
    Attempt,
    Campaign,
    CampaignItem,
    Contact,
    ContactList,
    ListMember,
    Template,
    Tenant,
    now,
)
from ats.repositories import TenantRepository
from ats.security import admin, principal
from ats.services import create_campaign, validate_route
from ats.workspace_queries import calls_query
from ats.workspace_schemas import CampaignEdit, Scenario
from ats.workspace_service import (
    add_members,
    audit,
    lock_campaign,
    require_idle,
    retry_failed,
    validate_template,
)

router = APIRouter(prefix="/api/v2", tags=["legacy campaigns"])


class CampaignSave(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID | int | None = None
    name: str = Field(min_length=1, max_length=120)
    template_id: UUID | int | None = None
    flow: str = "operator"
    route: str | None = None
    schedule: dict = Field(default_factory=dict)
    max_channels: int = Field(default=0, ge=0, le=100)
    retry_max: int = Field(default=-1, ge=-1, le=5)
    retry_delay_min: int = Field(default=-1, ge=-1, le=1440)
    connect_on_qualify: bool = True
    scenario: dict = Field(default_factory=dict)
    retry_map: dict = Field(default_factory=dict)


class AddContacts(BaseModel):
    contact_ids: list[UUID] | None = Field(default=None, max_length=5000)
    database_id: UUID | None = None
    group: str = Field(default="", max_length=120)
    consent_only: bool = True
    with_consent: bool | None = None


def campaign_dict(row, template_name="", counts=None, call_count=0, call_ok=0):
    counts = counts or {}
    schedule = getattr(row, "schedule", None) or {"start": f"{row.window_start:02d}:00", "end": f"{row.window_end:02d}:00", "days": row.weekdays}
    return {
        "id": str(row.id), "name": row.name, "template_id": str(row.template_id) if row.template_id else None,
        "template_name": template_name, "flow": row.mode, "route": row.route,
        "status": row.status if row.status in {"running", "paused"} else "stopped",
        "schedule": schedule, "max_channels": row.max_channels,
        "retry_max": row.retry_max, "retry_delay_min": row.retry_delay_minutes,
        "connect_on_qualify": int(row.connect_on_qualify),
        "scenario": getattr(row, "scenario", {}), "retry_map": getattr(row, "retry_map", {}),
        "created": row.created_at.isoformat(),
        "items_total": sum(counts.values()), "items_queued": counts.get("queued", 0), "queue_cnt": counts.get("queued", 0),
        "items_done": sum(counts.get(status, 0) for status in ("completed", "failed", "blocked")),
        "calls": call_count, "calls_ok": call_ok,
    }


def _counts(db, p, ids):
    grouped = db.execute(select(CampaignItem.campaign_id, CampaignItem.status, func.count()).where(
        CampaignItem.tenant_id == p.tenant_id, CampaignItem.campaign_id.in_(ids), CampaignItem.status != "cancelled"
    ).group_by(CampaignItem.campaign_id, CampaignItem.status)).all()
    result = {}
    for campaign_id, status, count in grouped:
        result.setdefault(campaign_id, {})[status] = count
    calls = db.execute(select(CampaignItem.campaign_id, Attempt.status, func.count()).join(
        Attempt, (Attempt.item_id == CampaignItem.id) & (Attempt.tenant_id == CampaignItem.tenant_id)
    ).where(CampaignItem.tenant_id == p.tenant_id, CampaignItem.campaign_id.in_(ids)).group_by(CampaignItem.campaign_id, Attempt.status)).all()
    totals, successes = {}, {}
    for campaign_id, status, count in calls:
        totals[campaign_id] = totals.get(campaign_id, 0) + count
        if status == "completed":
            successes[campaign_id] = successes.get(campaign_id, 0) + count
    return result, totals, successes


@router.get("/campaigns")
def campaigns(offset: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=5000), p=Depends(principal), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    query = repo.query(Campaign).where(~Campaign.archived)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(select(Campaign, Template.name).outerjoin(Template, (Template.id == Campaign.template_id) & (Template.tenant_id == Campaign.tenant_id))
        .where(Campaign.tenant_id == p.tenant_id, ~Campaign.archived)
        .order_by(Campaign.created_at.desc(), Campaign.id).offset(offset).limit(limit)).all()
    counts, calls, ok = _counts(db, p, [c.id for c, _ in rows])
    return {"ok": True, "campaigns": [campaign_dict(c, name or "", counts.get(c.id), calls.get(c.id, 0), ok.get(c.id, 0)) for c, name in rows], "total": total, "offset": offset, "limit": limit}


def _items(db, p, campaign_id, limit=2000):
    attempts = select(Attempt.item_id, func.count().label("count"), func.max(Attempt.created_at).label("last")).where(Attempt.tenant_id == p.tenant_id).group_by(Attempt.item_id).subquery()
    rows = db.execute(select(CampaignItem, Contact, attempts.c.count).join(
        Contact, (Contact.id == CampaignItem.contact_id) & (Contact.tenant_id == CampaignItem.tenant_id)
    ).outerjoin(attempts, attempts.c.item_id == CampaignItem.id).where(
        CampaignItem.tenant_id == p.tenant_id, CampaignItem.campaign_id == campaign_id, CampaignItem.status != "cancelled"
    ).order_by(CampaignItem.id).limit(limit)).all()
    mapping = {"completed": "done_ok", "failed": "exhausted", "blocked": "blocked_no_consent", "active": "dialing"}
    return [{"id": str(i.id), "contact_id": str(c.id), "contact_name": c.name, "contact_phone": c.phone,
        "name": c.name, "phone": c.phone, "status": mapping.get(i.status, i.status), "attempts": count or 0,
        "next_attempt_at": i.available_at.isoformat() if i.status == "queued" else ""} for i, c, count in rows]


@router.get("/campaigns/{campaign_id}")
def campaign_detail(campaign_id: UUID, p=Depends(principal), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(Campaign, campaign_id)
    template = repo.get(Template, row.template_id) if row.template_id else None
    counts, totals, ok = _counts(db, p, [row.id])
    calls = [legacy_call(*r) for r in db.execute(calls_query(p).where(Campaign.id == row.id).order_by(Attempt.created_at.desc()).limit(1000))]
    return {"ok": True, "campaign": campaign_dict(row, template.name if template else "", counts.get(row.id), totals.get(row.id, 0), ok.get(row.id, 0)), "items": _items(db, p, row.id), "calls": calls}


@router.get("/campaigns/{campaign_id}/items")
def campaign_items(campaign_id: UUID, p=Depends(principal), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Campaign, campaign_id)
    return {"ok": True, "items": _items(db, p, row.id)}


def _schedule(raw, defaults):
    start = raw.get("start") or defaults.get("work_start", "08:00")
    end = raw.get("end") or defaults.get("work_end", "20:00")
    if not isinstance(start, str) or not isinstance(end, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start) or not re.fullmatch(r"(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)", end):
        raise Problem(422, "bad_schedule")
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    if sh * 60 + sm >= eh * 60 + em:
        raise Problem(422, "bad_schedule")
    return {"start": start, "end": end, "days": raw.get("days", list(range(7)))}, sh, min(24, eh + bool(em))


@router.post("/campaigns/save")
def campaign_save(data: CampaignSave, p=Depends(admin), db=Depends(session)):
    from ats.compat_settings import legacy_basic

    defaults = legacy_basic(db, p)
    repo = TenantRepository(db, p.tenant_id)
    # UUIDs are opaque. Old zero IDs are accepted only as a create sentinel.
    if isinstance(data.id, int) and data.id != 0:
        raise Problem(422, "bad_id")
    row = lock_campaign(db, p, data.id) if data.id else None
    if row:
        require_idle(db, row)
    template_id = data.template_id
    if isinstance(template_id, int):
        if template_id not in (0, 1):
            raise Problem(422, "bad_template_id")
        template = db.scalar(repo.query(Template).where(~Template.archived).order_by(Template.created_at).limit(1))
        template_id = template.id if template else None
    schedule, start, end = _schedule(data.schedule, defaults)
    provider = defaults.get("provider", "sim")
    route = data.route or (row.route if row else {"ami": "multicom_ami", "multicom": "multicom_ami", "megafon_vats": "megafon_vats", "sim": "sim"}.get(provider, "sim"))
    command = CampaignEdit(name=data.name, route=route, template_id=template_id, mode=data.flow,
        max_channels=data.max_channels or int(defaults.get("max_channels", 1)),
        window_start=start, window_end=end, weekdays=schedule["days"],
        retry_max=data.retry_max if data.retry_max >= 0 else int(defaults.get("retry_max", 2)),
        retry_delay_minutes=data.retry_delay_min if data.retry_delay_min > 0 else int(defaults.get("retry_delay_min", 15)),
        connect_on_qualify=data.connect_on_qualify)
    if data.scenario:
        Scenario.model_validate(data.scenario)
    if len(data.retry_map) > 30 or any(not isinstance(k, str) or len(k) > 80 or not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1440 for k, v in data.retry_map.items()):
        raise Problem(422, "bad_retry_map")
    if row:
        validate_template(db, p.tenant_id, command.template_id)
        if command.max_channels > db.get(Tenant, p.tenant_id).max_channels:
            raise Problem(422, "tenant_channel_limit")
        for key, value in command.model_dump(exclude={"contact_ids"}).items():
            setattr(row, key, value)
        audit(db, p, "campaign.updated", row.id)
    else:
        row, _ = create_campaign(db, p, command, f"legacy:{uuid4()}")
    row.scenario, row.retry_map, row.schedule = data.scenario, data.retry_map, schedule
    db.commit()
    return {"ok": True, "id": str(row.id)}


@router.post("/campaigns/{campaign_id}/add-contacts")
def campaign_add_contacts(campaign_id: UUID, data: AddContacts, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    if row.archived:
        raise Problem(409, "campaign_archived")
    repo = TenantRepository(db, p.tenant_id)
    identities = data.contact_ids
    if not identities:
        query = repo.query(Contact).where(~Contact.archived, ~Contact.blocked)
        if data.group:
            query = query.where(Contact.group == data.group)
        if data.consent_only or data.with_consent is True:
            query = query.where(Contact.consent)
        if data.with_consent is False:
            query = query.where(~Contact.consent)
        if data.database_id:
            repo.get(ContactList, data.database_id)
            query = query.where(Contact.id.in_(select(ListMember.contact_id).where(ListMember.tenant_id == p.tenant_id, ListMember.list_id == data.database_id)))
        identities = list(db.scalars(query.with_only_columns(Contact.id).limit(5001)))
        if len(identities) > 5000:
            raise Problem(422, "select_at_most_5000_contacts")
    cancelled = db.scalars(repo.query(CampaignItem).where(CampaignItem.campaign_id == row.id, CampaignItem.contact_id.in_(identities), CampaignItem.status == "cancelled").with_for_update()).all()
    added = add_members(db, p, row, identities)
    # Clear/stop preserve attempts. Explicit re-add can requeue a cancelled row without losing history.
    for item in cancelled:
        item.status, item.available_at = "queued", now()
    db.commit()
    return {"ok": True, "added": added + len(cancelled)}


@router.post("/campaigns/{campaign_id}/start")
def campaign_start(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    if row.archived:
        raise Problem(409, "campaign_archived")
    validate_route(p.tenant_id, row.route, db)
    for item in db.scalars(TenantRepository(db, p.tenant_id).query(CampaignItem).where(CampaignItem.campaign_id == row.id, CampaignItem.status == "cancelled").with_for_update()):
        item.status, item.available_at = "queued", now()
    row.status = "running"
    audit(db, p, "campaign.running", row.id)
    db.commit()
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/pause")
def campaign_pause(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    row.status = "paused"
    audit(db, p, "campaign.paused", row.id)
    db.commit()
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/stop")
def campaign_stop(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    row.status = "draft"
    for item in db.scalars(TenantRepository(db, p.tenant_id).query(CampaignItem).where(CampaignItem.campaign_id == row.id, CampaignItem.status == "queued").with_for_update()):
        item.status = "cancelled"
    audit(db, p, "campaign.stopped", row.id)
    db.commit()
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/retry-exhausted")
def campaign_retry(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    count = retry_failed(db, p, row)
    validate_route(p.tenant_id, row.route, db)
    row.status = "running"
    db.commit()
    return {"ok": True, "requeued": count}


@router.post("/campaigns/{campaign_id}/clear")
def campaign_clear(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    require_idle(db, row)
    for item in db.scalars(TenantRepository(db, p.tenant_id).query(CampaignItem).where(CampaignItem.campaign_id == row.id).with_for_update()):
        item.status = "cancelled"
    audit(db, p, "campaign.queue_cleared", row.id)
    db.commit()
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/delete")
def campaign_delete(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    require_idle(db, row)
    row.archived = True
    audit(db, p, "campaign.archived", row.id)
    db.commit()
    return {"ok": True}


@router.post("/campaigns/clear_all")
def campaigns_clear_all(p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    rows = db.scalars(TenantRepository(db, p.tenant_id).query(Campaign).where(~Campaign.archived, Campaign.status != "running").with_for_update()).all()
    for row in rows:
        require_idle(db, row)
        row.archived = True
        audit(db, p, "campaign.archived", row.id)
    db.commit()
    return {"ok": True, "deleted": len(rows)}
