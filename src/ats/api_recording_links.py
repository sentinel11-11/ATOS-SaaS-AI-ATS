"""Revocable links scoped to a single recording. Tokens are stored only as hashes."""

import secrets
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import select

from ats.config import settings
from ats.db import session
from ats.domain import Problem
from ats.models import Recording, RecordingShare, now
from ats.repositories import TenantRepository
from ats.security import admin, digest
from ats.workspace_service import audit

router = APIRouter(tags=["recording links"])


@router.post("/api/v1/workspace/recordings/{recording_id}/share", status_code=201)
def share(
    recording_id: UUID, hours: int = Query(24, ge=1, le=72), p=Depends(admin), db=Depends(session)
):
    TenantRepository(db, p.tenant_id).get(Recording, recording_id)
    token = secrets.token_urlsafe(32)
    row = TenantRepository(db, p.tenant_id).add(
        RecordingShare,
        recording_id=recording_id,
        token_hash=digest(token),
        expires_at=now() + timedelta(hours=hours),
    )
    db.flush()
    audit(db, p, "recording.share_created", row.id)
    db.commit()
    return {
        "id": row.id,
        "url": settings().public_origin + "/media/" + token,
        "expires_at": row.expires_at,
    }


@router.delete("/api/v1/workspace/recording-shares/{share_id}", status_code=204)
def revoke(share_id: UUID, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(RecordingShare, share_id)
    db.delete(row)
    audit(db, p, "recording.share_revoked", share_id)
    db.commit()


@router.get("/media/{token}")
def media(token: str, db=Depends(session)):
    if len(token) > 100:
        raise Problem(404, "recording_link_expired_or_invalid")
    row = db.execute(
        select(RecordingShare, Recording)
        .join(
            Recording,
            (Recording.id == RecordingShare.recording_id)
            & (Recording.tenant_id == RecordingShare.tenant_id),
        )
        .where(RecordingShare.token_hash == digest(token), RecordingShare.expires_at > now())
    ).first()
    if not row:
        raise Problem(404, "recording_link_expired_or_invalid")
    _, record = row
    path = Path(settings().recordings_dir) / str(record.tenant_id) / record.storage_key
    if not path.is_file():
        raise Problem(404, "recording_file_missing")
    return FileResponse(
        path, media_type=record.content_type, headers={"Content-Disposition": "inline"}
    )
