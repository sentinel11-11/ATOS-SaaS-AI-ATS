"""Workspace routes: operations."""

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast, func, or_, select

from ats import workspace_schemas as commands
from ats.db import session
from ats.domain import ACTIVE, Problem
from ats.models import (
    Attempt,
    Audit,
    Campaign,
    Contact,
    DirectoryEntry,
    Inbox,
    Outbox,
    RuntimeLog,
    Tenant,
    now,
)
from ats.repositories import TenantRepository
from ats.security import admin, principal
from ats.workspace_queries import call_dict, calls_query, dated, pagination
from ats.workspace_service import (
    audit,
    page,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["operations"])


@router.get("/dashboard")
def dashboard(p=Depends(principal), db=Depends(session)):
    from ats.connections import settings_for_tenant

    config = settings_for_tenant(db, p.tenant_id)
    counts = {}
    for model, key in [(Contact, "contacts"), (Campaign, "campaigns"), (Attempt, "attempts")]:
        query = select(func.count()).select_from(model).where(model.tenant_id == p.tenant_id)
        if model in {Contact, Campaign}:
            query = query.where(~model.archived)
        counts[key] = db.scalar(query)
    counts["active"] = db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.tenant_id == p.tenant_id, Attempt.status.in_(ACTIVE))
    )
    counts["unknown"] = db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.tenant_id == p.tenant_id, Attempt.status == "unknown")
    )
    counts["callable"] = db.scalar(
        select(func.count())
        .select_from(Contact)
        .where(
            Contact.tenant_id == p.tenant_id, Contact.consent, ~Contact.blocked, ~Contact.archived
        )
    )
    tenant = db.get(Tenant, p.tenant_id)
    return {
        "counts": counts,
        "tenant": tenant,
        "live_calls": config.live_calls_enabled and config.telephony_tenant_id == p.tenant_id,
        "active_calls": [
            call_dict(*row)
            for row in db.execute(
                calls_query(p)
                .where(Attempt.status.in_(ACTIVE))
                .order_by(Attempt.created_at.desc())
                .limit(100)
            )
        ],
    }


@router.get("/directory")
def directory(p=Depends(admin), db=Depends(session)):
    return db.scalars(
        TenantRepository(db, p.tenant_id)
        .query(DirectoryEntry)
        .order_by(DirectoryEntry.provider, DirectoryEntry.kind, DirectoryEntry.name)
    ).all()


@router.get("/reports")
def reports(
    since: datetime | None = None,
    until: datetime | None = None,
    p=Depends(principal),
    db=Depends(session),
):
    query = dated(
        select(Attempt).where(Attempt.tenant_id == p.tenant_id), Attempt.created_at, since, until
    ).subquery()
    statuses = dict(db.execute(select(query.c.status, func.count()).group_by(query.c.status)).all())
    days = db.execute(
        select(
            func.date_trunc(
                "day", func.timezone(db.get(Tenant, p.tenant_id).timezone, query.c.created_at)
            ).label("day"),
            func.count().label("total"),
            func.count().filter(query.c.status == "completed").label("completed"),
        )
        .group_by("day")
        .order_by("day")
    ).all()
    total = sum(statuses.values())
    return {
        "total": total,
        "statuses": statuses,
        "completion_percent": round(statuses.get("completed", 0) * 100 / total, 1) if total else 0,
        "days": [dict(r._mapping) for r in days],
    }


@router.get("/logs/{kind}")
def logs(
    kind: str,
    q: str = Query("", max_length=120),
    since: datetime | None = None,
    until: datetime | None = None,
    pg=Depends(pagination),
    p=Depends(admin),
    db=Depends(session),
):
    models = {
        "audit": (Audit, Audit.action),
        "runtime": (RuntimeLog, RuntimeLog.code),
        "outbox": (Outbox, Outbox.last_error),
        "inbox": (Inbox, Inbox.event),
    }
    if kind not in models:
        raise Problem(404, "not_found")
    model, column = models[kind]
    search = column.contains(q)
    if model == RuntimeLog:
        search = or_(
            search,
            cast(RuntimeLog.request_id, String).contains(q),
            RuntimeLog.component.contains(q),
        )
    elif model == Outbox:
        search = or_(search, Outbox.status.contains(q), Outbox.dedupe_key.contains(q))
    elif model == Inbox:
        search = or_(search, Inbox.external_id.contains(q), Inbox.status.contains(q))
    created = (
        model.received_at
        if model == Inbox
        else model.available_at
        if model == Outbox
        else model.created_at
    )
    return page(
        db,
        dated(
            TenantRepository(db, p.tenant_id).query(model).where(search),
            created,
            since,
            until,
        ).order_by(created.desc(), model.id),
        *pg,
    )


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: UUID, data: commands.RetryJob, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Outbox, job_id, lock=True)
    if row.kind not in {"crm", "diagnostic"} or row.status != "dead":
        raise Problem(409, "only_definitively_failed_jobs_can_retry")
    row.status, row.available_at, row.attempts = "pending", now(), 0
    audit(db, p, "job.retry_requested", row.id)
    db.commit()
    return row


@router.post("/diagnostics/{provider}", status_code=202)
def diagnostic(provider: str, p=Depends(admin), db=Depends(session)):
    if provider not in {"ami", "megafon", "amocrm"}:
        raise Problem(404, "not_found")
    repo = TenantRepository(db, p.tenant_id)
    row = repo.add(
        Outbox,
        kind="diagnostic",
        dedupe_key=f"diagnostic:{uuid4()}",
        payload={"provider": provider},
    )
    db.flush()
    audit(db, p, "diagnostic.queued", row.id)
    db.commit()
    return {"job_id": row.id}


@router.post("/sync/{provider}/{kind}", status_code=202)
def directory_sync(provider: str, kind: str, p=Depends(admin), db=Depends(session)):
    if (provider, kind) not in {
        ("amocrm", "users"),
        ("megafon", "users"),
        ("megafon", "groups"),
        ("megafon", "numbers"),
    }:
        raise Problem(404, "not_found")
    row = TenantRepository(db, p.tenant_id).add(
        Outbox,
        kind="diagnostic",
        dedupe_key=f"sync:{uuid4()}",
        payload={"provider": provider, "sync": kind},
    )
    db.flush()
    audit(db, p, "directory.sync_queued", row.id)
    db.commit()
    return {"job_id": row.id}
