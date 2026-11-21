"""Legacy workspace contracts backed by tenant-scoped PostgreSQL use cases.

The compatibility boundary translates field names only. It does not recreate the
SQLite engine, accept a tenant from a request body, or bypass dispatch safeguards.
"""

import json
from datetime import timedelta
from math import ceil
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select

from ats import api_identity, api_resources
from ats.db import session
from ats.domain import Problem
from ats.models import (
    Attempt,
    BlacklistEntry,
    CallerNumber,
    Operator,
    QueueEntry,
    Template,
    Tenant,
    User,
    now,
)
from ats.repositories import TenantRepository
from ats.schemas import phone_from_import
from ats.security import admin, principal
from ats.workspace_schemas import BlockSave, OperatorStatus, Outcome, Scenario
from ats.workspace_service import audit

router = APIRouter(prefix="/api/v2", tags=["legacy workspace resources"])


class LegacyInput(BaseModel):
    # Existing forms POST their projected rows, including read-only statistics.
    # Only explicitly declared fields are ever written to the database.
    model_config = ConfigDict(extra="ignore")


class EntityCommand(LegacyInput):
    id: UUID


class TemplateCommand(LegacyInput):
    id: UUID | Literal[0] | None = None
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(default="", max_length=10000)
    scenario: Scenario = Field(default_factory=Scenario)
    active: bool = True

    @field_validator("scenario", mode="before")
    @classmethod
    def decode_scenario(cls, value):
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid scenario JSON") from exc
        return value or {}


class NumberCommand(LegacyInput):
    id: UUID | Literal[0] | None = None
    number: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=32)
    label: str | None = Field(default=None, max_length=120)
    provider: Literal["ami", "megafon_vats", "multicom", "uis", "multicom_ami"] | None = None
    kind: str | None = Field(default=None, min_length=1, max_length=50)
    active: bool | None = None
    verified: bool | None = None
    enabled_outgoing: bool | None = None
    quarantined: bool | None = None
    daily_limit: int | None = Field(default=None, ge=1, le=100000)
    weight: int | None = Field(default=None, ge=1, le=100000)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86400)
    carrier: str | None = Field(default=None, max_length=50)
    provider_ref: str | None = Field(default=None, max_length=64)


class QuarantineCommand(EntityCommand):
    on: bool = True


class OperatorConnection(LegacyInput):
    connection_method: Literal["sip", "mobile"]
    mobile_phone: str = Field(default="", max_length=32)

    @field_validator("mobile_phone")
    @classmethod
    def mobile_number(cls, value):
        if not value.strip():
            return ""
        result = phone_from_import(value)
        if not result.startswith("+7") or len(result) != 12:
            raise ValueError("Use a Russian mobile number in +7 format")
        return result


class BlacklistCommand(LegacyInput):
    phone: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="Отказ клиента / Не звонить", min_length=1, max_length=500)
    _phone = field_validator("phone")(phone_from_import)


class AcceptCommand(EntityCommand):
    operator_id: UUID | None = None


class CompleteCommand(LegacyInput):
    call_id: UUID
    operator_id: UUID | None = None
    outcome: str = Field(default="operator_ok", min_length=1, max_length=500)


def template_projection(row):
    return {
        "id": row.id,
        "name": row.name,
        "text": row.text,
        "scenario": row.scenario,
        "active": not row.archived,
        "created": row.created_at,
    }


@router.get("/templates")
def templates(
    offset: int = Query(0, ge=0),
    limit: int = Query(5000, ge=1, le=5000),
    p=Depends(principal),
    db=Depends(session),
):
    rows = db.scalars(
        TenantRepository(db, p.tenant_id)
        .query(Template)
        .where(~Template.archived)
        .order_by(Template.created_at, Template.id)
        .offset(offset)
        .limit(limit)
    )
    return {"ok": True, "templates": [template_projection(row) for row in rows]}


@router.post("/templates/save")
def template_save(data: TemplateCommand, p=Depends(admin), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(Template, data.id, lock=True) if data.id else repo.add(Template)
    row.name, row.text = data.name, data.text
    row.scenario, row.archived = data.scenario.model_dump(), not data.active
    db.flush()
    audit(db, p, "template.updated" if data.id else "template.created", row.id)
    db.commit()
    return {"ok": True, "id": row.id}


@router.post("/templates/delete")
def template_delete(data: EntityCommand, p=Depends(admin), db=Depends(session)):
    api_resources.template_archive(template_id=data.id, p=p, db=db)
    return {"ok": True}


def caller_phone(value):
    value = str(value or "").strip()
    if len(value) == 10 and value.isascii() and value.isdigit():
        return value
    try:
        normalized = phone_from_import(value)
    except ValueError as exc:
        raise Problem(422, "bad_number") from exc
    if len(normalized) != 12 or not normalized.startswith("+7"):
        raise Problem(422, "caller_id_requires_ru_number")
    return normalized[2:]


def number_projection(row, stats, tenant, timestamp):
    timezone = ZoneInfo(tenant.timezone)
    day_start = timestamp.astimezone(timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    cooldown = row.last_used_at + timedelta(seconds=row.cooldown_seconds) if row.last_used_at else None
    counts = stats.get(row.phone, (0, 0, 0, 0))
    return {
        "id": row.id,
        "number": row.phone,
        "phone": row.phone,
        "label": row.label,
        "provider": row.provider,
        "kind": row.kind,
        "active": row.enabled,
        "verified": row.verified,
        "enabled_outgoing": row.enabled_outgoing,
        "daily_limit": row.daily_limit,
        "weight": row.weight,
        "quarantined": row.quarantined,
        "cooldown_until": cooldown.isoformat() if cooldown else "",
        "cooling": bool(cooldown and cooldown > timestamp),
        "cooldown_seconds": row.cooldown_seconds,
        "cooldown_remaining_seconds": max(0, ceil((cooldown - timestamp).total_seconds()))
        if cooldown else 0,
        "daily_date": day_start.date().isoformat(),
        "daily_count": counts[0],
        "dialed_total": counts[1],
        "answered_total": counts[2],
        "since_reset_count": counts[3],
        "carrier": row.carrier,
        "provider_ref": row.provider_ref,
    }


@router.get("/numbers")
def numbers(
    offset: int = Query(0, ge=0),
    limit: int = Query(5000, ge=1, le=5000),
    p=Depends(principal),
    db=Depends(session),
):
    repo = TenantRepository(db, p.tenant_id)
    tenant, timestamp = db.get(Tenant, p.tenant_id), now()
    day_start = timestamp.astimezone(ZoneInfo(tenant.timezone)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows = db.scalars(
        repo.query(CallerNumber).order_by(CallerNumber.phone).offset(offset).limit(limit)
    ).all()
    # One aggregate query for the whole page, never one query per number.
    stats = {
        phone: (daily, total, answered, since_reset)
        for phone, daily, total, answered, since_reset in db.execute(
            select(
                CallerNumber.phone,
                func.coalesce(func.sum(Attempt.channel_units).filter(
                    Attempt.created_at >= day_start,
                ), 0),
                func.count(Attempt.id),
                func.count(Attempt.id).filter(Attempt.answered_at.is_not(None)),
                func.count(Attempt.id).filter(
                    (CallerNumber.counters_reset_at.is_(None))
                    | (Attempt.created_at >= CallerNumber.counters_reset_at),
                ),
            )
            .outerjoin(
                Attempt,
                (Attempt.tenant_id == CallerNumber.tenant_id)
                & (Attempt.caller_number == CallerNumber.phone),
            )
            .where(CallerNumber.tenant_id == p.tenant_id, CallerNumber.id.in_([r.id for r in rows]))
            .group_by(CallerNumber.id, CallerNumber.phone)
        )
    }
    return {
        "ok": True,
        "numbers": [number_projection(row, stats, tenant, timestamp) for row in rows],
    }


@router.post("/numbers/save")
def number_save(data: NumberCommand, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(CallerNumber, data.id, lock=True) if data.id else None
    if row is None:
        phone = caller_phone(data.number or data.phone)
        if db.scalar(repo.query(CallerNumber).where(CallerNumber.phone == phone)):
            raise Problem(409, "number_exists")
        provider = data.provider or "multicom_ami"
        row = repo.add(
            CallerNumber, phone=phone, provider=provider,
            verified=provider != "multicom_ami", enabled=provider != "multicom_ami",
        )
    values = data.model_dump(exclude_unset=True, exclude_none=True)
    if "active" in values:
        row.enabled = values.pop("active")
    if "verified" in values:
        row.verified = values.pop("verified")
    for key in (
        "label", "provider", "kind", "enabled_outgoing", "quarantined", "daily_limit",
        "weight", "cooldown_seconds", "carrier", "provider_ref",
    ):
        if key in values:
            setattr(row, key, values[key])
    if row.provider == "multicom_ami" and row.enabled and not row.verified:
        raise Problem(409, "caller_number_not_verified")
    db.flush()
    audit(db, p, "number.updated" if data.id else "number.created", row.id)
    db.commit()
    return {"ok": True, "id": row.id}


@router.post("/numbers/quarantine")
def number_quarantine(data: QuarantineCommand, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    row = TenantRepository(db, p.tenant_id).get(CallerNumber, data.id, lock=True)
    row.quarantined = data.on
    audit(db, p, "number.quarantined" if data.on else "number.released", row.id)
    db.commit()
    return {"ok": True}


@router.post("/numbers/delete")
def number_delete(data: EntityCommand, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    api_resources.number_delete(number_id=data.id, p=p, db=db)
    return {"ok": True}


@router.post("/numbers/reset")
def number_reset(p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    rows = db.scalars(TenantRepository(db, p.tenant_id).query(CallerNumber).with_for_update()).all()
    timestamp = now()
    for row in rows:
        row.counters_reset_at = timestamp
        audit(db, p, "number.counters_reset", row.id)
    # Display periods do not release quarantine, cooldown, or actual quota usage.
    db.commit()
    return {"ok": True, "reset": len(rows)}


@router.post("/numbers/clear")
def numbers_clear(p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    db.execute(delete(CallerNumber).where(CallerNumber.tenant_id == p.tenant_id))
    audit(db, p, "number.pool_cleared")
    db.commit()
    return {"ok": True}


@router.get("/blacklist")
def blacklist(
    offset: int = Query(0, ge=0),
    limit: int = Query(5000, ge=1, le=5000),
    p=Depends(principal),
    db=Depends(session),
):
    rows = db.scalars(
        TenantRepository(db, p.tenant_id).query(BlacklistEntry)
        .order_by(BlacklistEntry.created_at.desc(), BlacklistEntry.id).offset(offset).limit(limit)
    )
    return {"ok": True, "blacklist": [
        {"id": row.id, "phone": row.phone, "reason": row.reason, "created": row.created_at}
        for row in rows
    ]}


@router.post("/blacklist/add")
def blacklist_add(data: BlacklistCommand, p=Depends(admin), db=Depends(session)):
    row = api_resources.block(data=BlockSave(phone=data.phone, reason=data.reason), p=p, db=db)
    return {"ok": True, "id": row.id}


@router.post("/blacklist/delete")
def blacklist_delete(data: EntityCommand, p=Depends(admin), db=Depends(session)):
    api_resources.unblock(entry_id=data.id, p=p, db=db)
    return {"ok": True}


def operator_projection(op, user, p=None):
    return {"id": op.id, "user_id": op.user_id, "name": op.name, "ext": op.extension,
            "status": op.status, "login": user.login, "megafon_user": op.megafon_user,
            "connection_method": op.connection_method,
            "mobile_phone": op.mobile_phone if p and (p.role == "admin" or p.user_id == user.id) else ""}


def acd_state(p, db):
    repo = TenantRepository(db, p.tenant_id)
    operators = [operator_projection(op, user, p) for op, user in db.execute(
        select(Operator, User).join(
            User, (User.id == Operator.user_id) & (User.tenant_id == Operator.tenant_id)
        ).where(Operator.tenant_id == p.tenant_id, User.active, User.role == "operator")
        .order_by(User.login)
    )]
    timestamp = now()
    queue = [
        {"id": row.id, "call_id": row.attempt_id, "operator_id": row.operator_id,
         "status": row.status, "phone": call.phone, "contact_phone": call.phone,
         "contact_name": call.contact_name, "created": row.created_at,
         "wait_sec": max(0, int((timestamp - row.created_at).total_seconds()))}
        for row, call in db.execute(
            repo.query(QueueEntry).add_columns(Attempt).join(
                Attempt, (Attempt.id == QueueEntry.attempt_id)
                & (Attempt.tenant_id == QueueEntry.tenant_id)
            ).where(QueueEntry.status == "waiting").order_by(QueueEntry.created_at).limit(1000)
        )
    ]
    return {"ok": True, "operators": operators, "acd": queue}


@router.get("/operators")
def operators(p=Depends(principal), db=Depends(session)):
    state = acd_state(p, db)
    return {"ok": True, "operators": state["operators"]}


@router.get("/acd")
def acd(p=Depends(principal), db=Depends(session)):
    return acd_state(p, db)


@router.post("/operators/status")
def operator_status(data: OperatorStatus, p=Depends(principal), db=Depends(session)):
    api_identity.operator_status(data=data, p=p, db=db)
    return {"ok": True}

@router.post("/operators/{operator_id}/status")
def admin_operator_status(operator_id: UUID, data: OperatorStatus, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Operator, operator_id, lock=True)
    if row.status == "busy" and data.status != "busy":
        active = db.scalar(select(func.count()).select_from(QueueEntry).where(QueueEntry.operator_id == row.id, QueueEntry.status == "accepted"))
        if active:
            raise Problem(409, "complete_current_task_first")
    row.status = data.status
    audit(db, p, "operator.status_changed_by_admin", row.id)
    db.commit()
    return {"ok": True, "status": row.status}


@router.post("/operators/{operator_id}/connection")
def operator_connection(
    operator_id: UUID, data: OperatorConnection, p=Depends(admin), db=Depends(session)
):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(Operator, operator_id, lock=True)
    if row.status == "busy" or db.scalar(
        select(QueueEntry.id).where(
            QueueEntry.tenant_id == p.tenant_id, QueueEntry.operator_id == row.id,
            QueueEntry.status == "accepted",
        ).limit(1)
    ):
        raise Problem(409, "finish_operator_calls_first")
    if data.connection_method == "mobile" and not data.mobile_phone:
        raise Problem(422, "operator_mobile_phone_required")
    row.connection_method, row.mobile_phone = data.connection_method, data.mobile_phone
    audit(db, p, "operator.connection_updated", row.id)
    db.commit()
    return {"ok": True}


@router.post("/acd/accept")
def accept(data: AcceptCommand, p=Depends(principal), db=Depends(session)):
    if p.role == "operator":
        row = api_identity.queue_accept(entry_id=data.id, p=p, db=db)
        op = TenantRepository(db, p.tenant_id).get(Operator, row.operator_id)
    elif p.role == "admin":
        repo = TenantRepository(db, p.tenant_id)
        query = repo.query(Operator).join(
            User, (User.id == Operator.user_id) & (User.tenant_id == Operator.tenant_id)
        ).where(User.active, User.role == "operator", Operator.status == "free")
        if data.operator_id:
            query = query.where(Operator.id == data.operator_id)
        op = db.scalar(query.order_by(Operator.id).with_for_update(of=Operator, skip_locked=True))
        if not op:
            raise Problem(409, "no_free_operator")
        row = repo.get(QueueEntry, data.id, lock=True)
        if row.status != "waiting":
            raise Problem(409, "task_already_assigned")
        row.operator_id, row.status, op.status = op.id, "accepted", "busy"
        audit(db, p, "acd.accepted", row.id)
        db.commit()
    else:
        raise Problem(403, "operator_required")
    return {"ok": True, "operator": op.name, "operator_id": op.id}


@router.post("/acd/miss")
def miss(data: EntityCommand, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(QueueEntry, data.id, lock=True)
    if row.status != "waiting":
        raise Problem(409, "task_already_assigned")
    row.status, row.outcome = "cancelled", "missed"
    audit(db, p, "acd.missed", row.id)
    db.commit()
    return {"ok": True}


def complete_operator_call(data: CompleteCommand, p, db):
    repo = TenantRepository(db, p.tenant_id)
    row = db.scalar(repo.query(QueueEntry).where(
        QueueEntry.attempt_id == data.call_id, QueueEntry.status == "accepted"
    ).with_for_update())
    if row is None:
        raise Problem(404, "operator_call_not_found")
    if data.operator_id and p.role == "admin" and row.operator_id != data.operator_id:
        raise Problem(409, "operator_mismatch")
    api_identity.queue_complete(entry_id=row.id, data=Outcome(outcome=data.outcome), p=p, db=db)
    return {"ok": True}
