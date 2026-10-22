"""Workspace routes: identity."""

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, func, select

from ats import workspace_schemas as commands
from ats.config import settings
from ats.db import session
from ats.domain import ACTIVE, Problem
from ats.models import (
    Attempt,
    Operator,
    QueueEntry,
    Session,
    Tenant,
    User,
    WorkspaceSettings,
)
from ats.repositories import TenantRepository
from ats.security import admin, hasher, principal, verify_password
from ats.workspace_service import (
    audit,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["identity"])


@router.get("/users")
def users(p=Depends(admin), db=Depends(session)):
    return [
        {
            "id": u.id,
            "login": u.login,
            "role": u.role,
            "active": u.active,
            "name": op.name if op else "",
            "extension": op.extension if op else "",
        }
        for u, op in db.execute(
            select(User, Operator)
            .outerjoin(
                Operator, (Operator.user_id == User.id) & (Operator.tenant_id == User.tenant_id)
            )
            .where(User.tenant_id == p.tenant_id)
            .order_by(User.login)
        )
    ]


def write_user(db, p, data, user_id=None):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    user = repo.get(User, user_id, lock=True) if user_id else None
    if user and user.role == "admin" and user.active and (data.role != "admin" or not data.active):
        admins = db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == p.tenant_id, User.role == "admin", User.active)
        )
        if admins <= 1:
            raise Problem(409, "last_admin_required")
    if user is None:
        if not data.password:
            raise Problem(422, "password_required")
        user = repo.add(
            User,
            login=data.login.lower(),
            role=data.role,
            active=data.active,
            password_hash=hasher.hash(data.password),
        )
        db.flush()
    else:
        user.login, user.role, user.active = data.login.lower(), data.role, data.active
        if data.password:
            user.password_hash = hasher.hash(data.password)
        db.execute(
            delete(Session).where(Session.tenant_id == p.tenant_id, Session.user_id == user.id)
        )
    operator = db.scalar(repo.query(Operator).where(Operator.user_id == user.id).with_for_update())
    if (
        operator
        and operator.status == "busy"
        and (data.role != "operator" or not data.active or operator.extension != data.extension)
    ):
        raise Problem(409, "finish_operator_calls_first")
    if data.role == "operator":
        if not operator:
            operator = repo.add(Operator, user_id=user.id)
        operator.name, operator.extension = data.name, data.extension
        if not data.active:
            operator.status = "offline"
    elif operator:
        operator.status = "offline"
    audit(db, p, "user.saved", user.id)
    return {"id": user.id, "login": user.login, "role": user.role, "active": user.active}


@router.post("/users", status_code=201)
def user_create(data: commands.UserSave, p=Depends(admin), db=Depends(session)):
    result = write_user(db, p, data)
    db.commit()
    return result


@router.put("/users/{user_id}")
def user_update(user_id: UUID, data: commands.UserSave, p=Depends(admin), db=Depends(session)):
    result = write_user(db, p, data, user_id)
    db.commit()
    return result


@router.post("/password", status_code=204)
def password_change(data: commands.PasswordChange, p=Depends(principal), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(User, p.user_id, lock=True)
    if not verify_password(data.current_password, row.password_hash):
        raise Problem(403, "current_password_incorrect")
    row.password_hash = hasher.hash(data.new_password)
    db.execute(
        delete(Session).where(Session.tenant_id == p.tenant_id, Session.user_id == p.user_id)
    )
    audit(db, p, "user.password_changed", row.id)
    db.commit()


@router.get("/operators")
def operators(p=Depends(principal), db=Depends(session)):
    return {
        "operators": [
            {**jsonable_encoder(op), "login": u.login}
            for op, u in db.execute(
                select(Operator, User)
                .join(User, (User.id == Operator.user_id) & (User.tenant_id == Operator.tenant_id))
                .where(Operator.tenant_id == p.tenant_id, User.active, User.role == "operator")
            )
        ],
        "queue": [
            {
                **jsonable_encoder(row),
                "contact_name": call.contact_name,
                "phone": call.phone,
                "call_status": call.status,
                "route": call.route,
                "can_hangup": call.status in ACTIVE
                and (
                    call.status == "prepared"
                    or call.route == "sim"
                    or (call.route == "multicom_ami" and bool(call.channel))
                ),
            }
            for row, call in db.execute(
                select(QueueEntry, Attempt)
                .join(
                    Attempt,
                    (Attempt.id == QueueEntry.attempt_id)
                    & (Attempt.tenant_id == QueueEntry.tenant_id),
                )
                .where(
                    QueueEntry.tenant_id == p.tenant_id,
                    QueueEntry.status.in_(["waiting", "accepted"]),
                )
                .order_by(QueueEntry.created_at)
                .limit(100)
            )
        ],
    }


@router.post("/operator-status")
def operator_status(data: commands.OperatorStatus, p=Depends(principal), db=Depends(session)):
    row = db.scalar(
        TenantRepository(db, p.tenant_id)
        .query(Operator)
        .where(Operator.user_id == p.user_id)
        .with_for_update()
    )
    if not row or p.role != "operator":
        raise Problem(403, "operator_required")
    if row.status == "busy":
        raise Problem(409, "complete_current_task_first")
    row.status = data.status
    audit(db, p, "operator.status_changed", row.id)
    db.commit()
    return row


@router.post("/queue/{entry_id}/accept")
def queue_accept(entry_id: UUID, p=Depends(principal), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    op = db.scalar(repo.query(Operator).where(Operator.user_id == p.user_id).with_for_update())
    if not op or p.role != "operator" or op.status != "free":
        raise Problem(409, "available_operator_required")
    row = repo.get(QueueEntry, entry_id, lock=True)
    if row.status != "waiting":
        raise Problem(409, "task_already_assigned")
    row.operator_id, row.status, op.status = op.id, "accepted", "busy"
    audit(db, p, "acd.accepted", row.id)
    db.commit()
    return row


@router.post("/queue/{entry_id}/complete")
def queue_complete(
    entry_id: UUID, data: commands.Outcome, p=Depends(principal), db=Depends(session)
):
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(QueueEntry, entry_id, lock=True)
    if not row.operator_id or row.status != "accepted":
        raise Problem(409, "task_not_assigned")
    op = repo.get(Operator, row.operator_id, lock=True)
    if op.user_id != p.user_id and p.role != "admin":
        raise Problem(403, "task_owner_required")
    attempt = repo.get(Attempt, row.attempt_id)
    if attempt.route != "sim" and attempt.status in ACTIVE:
        raise Problem(409, "finish_live_call_first")
    row.outcome, row.status, op.status = data.outcome, "completed", "free"
    audit(db, p, "acd.completed", row.id)
    db.commit()
    return row


@router.get("/settings")
def workspace_settings(p=Depends(admin), db=Depends(session)):
    config = settings()
    row = db.scalar(TenantRepository(db, p.tenant_id).query(WorkspaceSettings))
    return {
        "tenant": db.get(Tenant, p.tenant_id),
        "providers": row.values if row else commands.ProviderSettings().model_dump(),
        "revision": row.revision if row else 0,
        "connections": {
            "ami_host": config.ami_host,
            "ami_port": config.ami_port,
            "ami_user": config.ami_user,
            "ami_tls": config.ami_tls,
            "ami_secret_set": bool(config.ami_secret.get_secret_value()),
            "caller_id": config.ami_caller_id,
            "megafon_url": config.megafon_url,
            "megafon_key_set": bool(config.megafon_api_key.get_secret_value()),
            "amo_url": config.amo_url,
            "amo_token_set": bool(config.amo_access_token.get_secret_value()),
            "crm_enabled": config.crm_enabled and config.amo_tenant_id == p.tenant_id,
            "live_calls_enabled": config.live_calls_enabled
            and config.telephony_tenant_id == p.tenant_id,
            "test_destinations": sorted(config.destinations)
            if config.telephony_tenant_id == p.tenant_id
            else [],
        },
    }


@router.put("/settings/tenant")
def tenant_settings(data: commands.WorkspaceSave, p=Depends(admin), db=Depends(session)):
    row = db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    audit(db, p, "settings.tenant_updated", row.id)
    db.commit()
    return row


@router.put("/settings/providers")
def provider_settings(data: commands.ProviderSettings, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    row = db.scalar(repo.query(WorkspaceSettings).with_for_update())
    if not row:
        row = repo.add(WorkspaceSettings, values=data.model_dump())
    else:
        row.values, row.revision = data.model_dump(), row.revision + 1
    audit(db, p, "settings.providers_updated")
    db.commit()
    return row
