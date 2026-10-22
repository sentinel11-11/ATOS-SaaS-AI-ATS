"""Workspace routes: calls."""

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select

from ats.config import settings
from ats.db import session
from ats.domain import ACTIVE, Problem
from ats.models import (
    Attempt,
    CallEvent,
    Contact,
    HistoricalCall,
    InboundCall,
    Operator,
    Outbox,
    QueueEntry,
    Recording,
)
from ats.repositories import TenantRepository
from ats.security import admin, principal
from ats.workspace_queries import call_dict, calls_query, pagination
from ats.workspace_service import (
    audit,
    csv_document,
    page,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["calls"])


@router.get("/calls")
def calls(
    q: str = Query("", max_length=120),
    status: str = "",
    campaign_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    pg=Depends(pagination),
    p=Depends(principal),
    db=Depends(session),
):
    query = calls_query(p, q, status, campaign_id, since, until)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {
        "items": [
            call_dict(*r)
            for r in db.execute(
                query.order_by(Attempt.created_at.desc(), Attempt.id).offset(pg[0]).limit(pg[1])
            )
        ],
        "total": total,
    }


@router.get("/incoming")
def incoming(q: str = "", pg=Depends(pagination), p=Depends(principal), db=Depends(session)):
    return page(
        db,
        TenantRepository(db, p.tenant_id)
        .query(InboundCall)
        .where(InboundCall.phone.contains(q))
        .order_by(InboundCall.created_at.desc()),
        *pg,
    )


@router.get("/history")
def history(q: str = "", pg=Depends(pagination), p=Depends(principal), db=Depends(session)):
    return page(
        db,
        TenantRepository(db, p.tenant_id)
        .query(HistoricalCall)
        .where(or_(HistoricalCall.phone.contains(q), HistoricalCall.contact_name.ilike(f"%{q}%")))
        .order_by(HistoricalCall.created_at.desc()),
        *pg,
    )


@router.get("/calls/{attempt_id}")
def call_detail(attempt_id: UUID, p=Depends(principal), db=Depends(session)):
    row = db.execute(calls_query(p).where(Attempt.id == attempt_id)).first()
    if not row:
        raise Problem(404, "not_found")
    repo = TenantRepository(db, p.tenant_id)
    return {
        "call": call_dict(*row),
        "events": db.scalars(
            repo.query(CallEvent)
            .where(CallEvent.attempt_id == attempt_id)
            .order_by(CallEvent.created_at)
        ).all(),
        "crm": db.scalars(repo.query(Outbox).where(Outbox.dedupe_key == f"crm:{attempt_id}")).all(),
        "recordings": db.scalars(
            repo.query(Recording).where(Recording.attempt_id == attempt_id)
        ).all(),
    }


@router.post("/calls/{attempt_id}/hangup", status_code=202)
def call_hangup(attempt_id: UUID, p=Depends(principal), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(Attempt, attempt_id, lock=True)
    if p.role != "admin":
        op = db.scalar(repo.query(Operator).where(Operator.user_id == p.user_id))
        if not op or not db.scalar(
            repo.query(QueueEntry).where(
                QueueEntry.attempt_id == row.id, QueueEntry.operator_id == op.id
            )
        ):
            raise Problem(403, "call_owner_required")
    if row.status not in ACTIVE:
        raise Problem(409, "call_already_ended")
    if row.route == "sim" or row.status == "prepared":
        from ats.services import finish_attempt

        finish_attempt(db, row, "failed", "cancelled_by_user")
        result = {"status": "cancelled"}
    else:
        if (
            row.route != "multicom_ami"
            or settings().telephony_tenant_id != p.tenant_id
            or not row.channel
        ):
            raise Problem(409, "provider_hangup_unavailable")
        job = db.scalar(repo.query(Outbox).where(Outbox.dedupe_key == f"hangup:{row.id}"))
        if not job:
            job = repo.add(
                Outbox,
                kind="control",
                dedupe_key=f"hangup:{row.id}",
                payload={"attempt_id": str(row.id), "command": "hangup"},
            )
            db.flush()
        result = {"status": "queued", "job_id": job.id}
    audit(db, p, "call.hangup_requested", row.id)
    db.commit()
    return result


@router.get("/export/{kind}")
def export(
    kind: str,
    q: str = "",
    status: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    p=Depends(admin),
    db=Depends(session),
):
    if kind == "contacts.csv":
        rows = db.scalars(
            TenantRepository(db, p.tenant_id)
            .query(Contact)
            .where(~Contact.archived)
            .order_by(Contact.id)
            .limit(100000)
        )
        content = csv_document(
            ["name", "phone", "group", "email", "consent", "consent_source", "blocked"],
            (
                [c.name, c.phone, c.group, c.email, c.consent, c.consent_source, c.blocked]
                for c in rows
            ),
        )
    elif kind == "calls.csv":
        rows = db.execute(
            calls_query(p, q=q, status=status, since=since, until=until)
            .order_by(Attempt.created_at.desc())
            .limit(100000)
        )
        content = csv_document(
            [
                "id",
                "time",
                "contact",
                "phone",
                "campaign",
                "route",
                "status",
                "reason",
                "caller_id",
            ],
            (
                [
                    a.id,
                    a.created_at.isoformat(),
                    a.contact_name or c.name,
                    a.phone or c.phone,
                    camp.name,
                    a.route,
                    a.status,
                    a.reason,
                    a.caller_number,
                ]
                for a, c, camp in rows
            ),
        )
    else:
        raise Problem(404, "not_found")
    audit(db, p, "export." + kind)
    db.commit()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}"'},
    )


@router.post("/calls/{attempt_id}/recordings", status_code=201)
async def recording_upload(
    attempt_id: UUID, request: Request, file: UploadFile, p=Depends(admin), db=Depends(session)
):
    repo = TenantRepository(db, p.tenant_id)
    repo.get(Attempt, attempt_id)
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise Problem(413, "recording_too_large")
    kind = (
        "audio/wav"
        if content[:4] == b"RIFF" and content[8:12] == b"WAVE"
        else "audio/ogg"
        if content[:4] == b"OggS"
        else None
    )
    if not kind:
        raise Problem(422, "recording_wav_or_ogg_required")
    directory = Path(settings().recordings_dir) / str(p.tenant_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = uuid4().hex
    path = directory / key
    try:
        with path.open("xb") as stream:
            stream.write(content)
        path.chmod(0o600)
        row = repo.add(
            Recording,
            attempt_id=attempt_id,
            storage_key=key,
            content_type=kind,
            size_bytes=len(content),
        )
        db.flush()
        audit(db, p, "recording.uploaded", row.id)
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return row


@router.get("/recordings/{recording_id}")
def recording_download(recording_id: UUID, p=Depends(principal), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Recording, recording_id)
    path = Path(settings().recordings_dir) / str(p.tenant_id) / row.storage_key
    if not path.is_file():
        raise Problem(404, "recording_file_missing")
    return FileResponse(
        path, media_type=row.content_type, headers={"Content-Disposition": "inline"}
    )
