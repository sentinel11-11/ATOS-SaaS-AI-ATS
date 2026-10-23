"""Original settings forms translated into validated, tenant-owned commands."""

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from ats.api_identity import users as workspace_users
from ats.api_identity import write_user
from ats.config import settings
from ats.connections import public_provider_config, save_provider_config
from ats.db import session
from ats.domain import Problem
from ats.models import DirectoryEntry, Outbox, Session, Tenant, User, WorkspaceSettings
from ats.repositories import TenantRepository
from ats.security import admin, hasher, principal
from ats.workspace_schemas import UserSave
from ats.workspace_service import audit

router = APIRouter(prefix="/api/v2", tags=["legacy workspace settings"])


class BasicSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: Literal["", "sim", "ami", "multicom", "megafon_vats", "uis"] = "sim"
    max_channels: int = Field(default=3, ge=1, le=100)
    retry_max: int = Field(default=2, ge=0, le=5)
    retry_delay_min: int = Field(default=15, ge=1, le=1440)
    line_cooldown_sec: int = Field(default=5, ge=0, le=86400)
    watchdog_timeout_min: int = Field(default=20, ge=1, le=1440)
    acd_wait_timeout_sec: int = Field(default=60, ge=5, le=86400)
    vats_conversation_timeout_min: int = Field(default=30, ge=0, le=1440)
    auto_quarantine_on_complaints: int = Field(default=3, ge=1, le=100)
    window_start: str = Field(default="08:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    window_end: str = Field(default="20:00", pattern=r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")
    consent_required: bool = True
    log_non_campaign_calls: bool = True
    new_password: str = Field(default="", max_length=256)


def options_row(db, tenant_id, lock=False):
    query = select(WorkspaceSettings).where(WorkspaceSettings.tenant_id == tenant_id)
    return db.scalar(query.with_for_update() if lock else query)


def legacy_basic(db, p):
    tenant = db.get(Tenant, p.tenant_id)
    row = options_row(db, p.tenant_id)
    basic = BasicSettings().model_dump(exclude={"new_password"})
    basic.update((row.values if row else {}).get("_legacy_basic", {}))
    basic["max_channels"] = tenant.max_channels
    return basic


@router.get("/settings")
def get_settings(p=Depends(admin), db=Depends(session)):
    return {"ok": True, "settings": legacy_basic(db, p)}


@router.post("/settings/save")
def save_settings(data: BasicSettings, p=Depends(admin), db=Depends(session)):
    tenant = db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    if data.new_password and len(data.new_password) < 12:
        raise Problem(422, "password_minimum_12")
    if data.window_start >= data.window_end:
        raise Problem(422, "bad_schedule")
    row = options_row(db, p.tenant_id, lock=True)
    if row is None:
        row = WorkspaceSettings(tenant_id=p.tenant_id, values={})
        db.add(row)
    values = dict(row.values or {})
    values["_legacy_basic"] = data.model_dump(exclude={"new_password"})
    values["auto_quarantine_on_complaints"] = data.auto_quarantine_on_complaints
    tenant.max_channels = data.max_channels
    row.values, row.revision = values, (row.revision or 0) + 1
    if data.new_password:
        user = TenantRepository(db, p.tenant_id).get(User, p.user_id, lock=True)
        user.password_hash = hasher.hash(data.new_password)
        # Keep the initiating session usable for the second configuration save;
        # revoke all other sessions, including sessions on other machines.
        # Password revocation of every session is intentional; the response lets
        # the browser return to the original login once saving has completed.
        db.execute(delete(Session).where(Session.tenant_id == p.tenant_id, Session.user_id == p.user_id))
    audit(db, p, "settings.saved")
    db.commit()
    return {"ok": True}


@router.get("/settings/raw")
def provider_config(p=Depends(admin), db=Depends(session)):
    return {"ok": True, "provider_config": public_provider_config(db, p.tenant_id), "encrypted_secrets_available": bool(settings().connection_encryption_key.get_secret_value())}


@router.post("/settings/raw")
def provider_save(body: dict = Body(...), p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    incoming = body.get("provider_config")
    save_provider_config(db, p.tenant_id, incoming)
    row = options_row(db, p.tenant_id, lock=True)
    if row is None:
        row = WorkspaceSettings(tenant_id=p.tenant_id, values={})
        db.add(row)
    values = dict(row.values or {})
    config = public_provider_config(db, p.tenant_id)
    amo = config.get("amocrm", {})
    values.update({
        "megafon_user": config.get("megafon_vats", {}).get("default_user", ""),
        "amo_responsible_user_id": amo.get("responsible_user_id"),
        "amo_auto_create_contacts": amo.get("auto_create_contacts", False),
        "amo_auto_create_tasks": amo.get("auto_create_tasks", False),
        "amo_operator_user_map": amo.get("operator_user_map", {}),
        "task_due_hours": amo.get("task_due_hours", 24),
    })
    row.values, row.revision = values, (row.revision or 0) + 1
    audit(db, p, "provider.settings_saved")
    db.commit()
    return {"ok": True}


@router.get("/users")
def users(p=Depends(admin), db=Depends(session)):
    records = workspace_users(p=p, db=db)
    archived = set(db.scalars(select(User.id).where(User.tenant_id == p.tenant_id, User.archived)))
    return {"ok": True, "users": [{**u, "op_name": u["name"], "ext": u["extension"]} for u in records if u["id"] not in archived]}


class UserCommand(UserSave):
    model_config = ConfigDict(extra="ignore")
    id: UUID | Literal[0] | None = None
    ext: str = Field(default="", pattern=r"^[0-9]{0,20}$")


@router.post("/users/save")
def save_user(data: UserCommand, p=Depends(admin), db=Depends(session)):
    command = UserSave.model_validate({**data.model_dump(exclude={"id", "ext"}), "extension": data.ext or data.extension})
    if data.id:
        existing = TenantRepository(db, p.tenant_id).get(User, data.id)
        if existing.archived:
            raise Problem(409, "user_archived")
    result = write_user(db, p, command, data.id or None)
    db.commit()
    return {"ok": True, **result}


class IdCommand(BaseModel):
    id: UUID


@router.post("/users/delete")
def delete_user(data: IdCommand, p=Depends(admin), db=Depends(session)):
    user = TenantRepository(db, p.tenant_id).get(User, data.id)
    from ats.models import Operator
    op = db.scalar(select(Operator).where(Operator.tenant_id == p.tenant_id, Operator.user_id == user.id))
    command = UserSave(login=user.login, role=user.role, active=False, name=op.name if op else "", extension=op.extension if op else "")
    write_user(db, p, command, user.id)  # Includes last-admin and busy-call guards.
    user.archived = True
    audit(db, p, "user.archived", user.id)
    db.commit()
    return {"ok": True}


def enqueue_check(db, p, provider, kind=None):
    job_id = uuid4()
    payload = {"provider": provider}
    if kind:
        payload["sync"] = kind
    TenantRepository(db, p.tenant_id).add(Outbox, id=job_id, kind="diagnostic", dedupe_key=f"diagnostic:{job_id}", payload=payload)
    audit(db, p, "provider.sync_requested" if kind else "provider.check_requested", job_id)
    db.commit()
    return {"ok": True, "queued": True, "job_id": job_id}


@router.post("/{provider}/check", status_code=202)
def check(provider: Literal["ami", "megafon", "multicom", "amocrm"], p=Depends(admin), db=Depends(session)):
    return enqueue_check(db, p, provider)


@router.post("/{provider}/{action}", status_code=202)
def sync(provider: Literal["megafon", "multicom", "amocrm"], action: Literal["pool-sync", "users-sync", "groups-sync"], p=Depends(admin), db=Depends(session)):
    kind = {"pool-sync": "numbers", "users-sync": "users", "groups-sync": "groups"}[action]
    if (provider == "amocrm" and kind != "users") or (provider == "multicom" and kind != "numbers"):
        raise Problem(422, "provider_directory_not_supported")
    return enqueue_check(db, p, provider, kind)


@router.get("/jobs/{job_id}")
def job(job_id: UUID, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Outbox, job_id)
    if row.kind != "diagnostic":
        raise Problem(404, "not_found")
    queued = row.status in {"pending", "leased"}
    return {"ok": queued or row.status == "done", "queued": queued, "job_id": row.id, "status": row.status, "connected": row.status == "done", "error": row.last_error or None, "report": {"ok": row.status == "done", "failed_stage": row.last_error or None}}


@router.get("/amocrm/users")
def amo_users(p=Depends(admin), db=Depends(session)):
    rows = db.scalars(select(DirectoryEntry).where(DirectoryEntry.tenant_id == p.tenant_id, DirectoryEntry.provider == "amocrm", DirectoryEntry.kind == "users").order_by(DirectoryEntry.name).limit(2000))
    return {"ok": True, "users": [{"id": row.external_id, "name": row.name, "login": row.phone} for row in rows]}


@router.get("/megafon/directory")
def megafon_directory(p=Depends(admin), db=Depends(session)):
    rows = db.scalars(select(DirectoryEntry).where(DirectoryEntry.tenant_id == p.tenant_id, DirectoryEntry.provider == "megafon").limit(6000)).all()
    return {"ok": True, "users": [{"id": row.external_id, "name": row.name, "ext": row.phone} for row in rows if row.kind == "users"], "groups": [{"id": row.external_id, "name": row.name} for row in rows if row.kind == "groups"]}


@router.get("/health")
@router.get("/health/details")
def health(p=Depends(principal), db=Depends(session)):
    return {"ok": True, "status": "ok", "live_calls": settings().live_calls_enabled and settings().telephony_tenant_id == p.tenant_id, "storage": "postgresql", "engine": "workers", "version": "0.3.0"}
