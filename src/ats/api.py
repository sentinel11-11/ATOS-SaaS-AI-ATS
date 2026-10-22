import hashlib
import logging
import secrets
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ats import schemas
from ats.config import settings
from ats.db import session
from ats.domain import Problem
from ats.models import Attempt, Audit, Campaign, Contact, Inbox, Outbox, Session, Tenant
from ats.repositories import TenantRepository
from ats.security import admin, authenticate, digest, principal
from ats.services import create_campaign, finish_attempt, ingest, validate_route

log = logging.getLogger("ats.api")
app = FastAPI(title="ATS Beta", version="0.1.0")


@app.middleware("http")
async def request_guard(request: Request, call_next):
    request.state.request_id = str(uuid4())
    unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
    if unsafe and not request.url.path.startswith("/webhooks/"):
        if request.headers.get("origin") != settings().public_origin:
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
    # Enforce the limit for both chunked and Content-Length requests before parsing.
    if unsafe:
        size = 0
        parts = []
        async for part in request.stream():
            size += len(part)
            if size > 256 * 1024:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            parts.append(part)
        request._body = b"".join(parts)
    response = await call_next(request)
    response.headers.update(
        {
            "X-Request-ID": request.state.request_id,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; connect-src 'self'; style-src 'self'; frame-ancestors 'none'",
        }
    )
    return response


@app.exception_handler(Problem)
async def problem_handler(request, exc):
    return JSONResponse(
        {"error": exc.code, "request_id": request.state.request_id}, status_code=exc.status
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    # Never echo passwords, tokens or raw webhook input in error responses.
    return JSONResponse(
        {
            "error": "validation_failed",
            "fields": [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
            "request_id": request.state.request_id,
        },
        status_code=422,
    )


@app.exception_handler(IntegrityError)
async def conflict_handler(request, exc):
    return JSONResponse(
        {"error": "conflict", "request_id": request.state.request_id}, status_code=409
    )


@app.exception_handler(SQLAlchemyError)
async def database_handler(request, exc):
    log.error(
        "database_unavailable request_id=%s type=%s", request.state.request_id, type(exc).__name__
    )
    return JSONResponse(
        {"error": "storage_unavailable", "request_id": request.state.request_id}, status_code=503
    )


@app.exception_handler(Exception)
async def unexpected_handler(request, exc):
    log.error("internal_error request_id=%s type=%s", request.state.request_id, type(exc).__name__)
    return JSONResponse(
        {"error": "internal_error", "request_id": request.state.request_id}, status_code=500
    )


@app.get("/health/live")
def live():
    return {"status": "alive"}


@app.get("/health/ready")
def ready(db=Depends(session)):
    # Verify required schema, not merely TCP/Postgres availability.
    version = db.scalar(text("SELECT version_num FROM alembic_version"))
    if version != "0001":
        raise Problem(503, "schema_migration_required")
    db.scalar(select(func.count()).select_from(Tenant))
    return {"status": "ready", "live_calls": settings().live_calls_enabled}


@app.post("/api/v1/auth/login")
def login(data: schemas.Login, request: Request, response: Response, db=Depends(session)):
    token, csrf = authenticate(db, data, request.client.host)
    options = dict(
        secure=settings().secure_cookies,
        samesite="strict",
        path="/",
        max_age=settings().session_hours * 3600,
    )
    response.set_cookie("ats_session", token, httponly=True, **options)
    response.set_cookie("ats_csrf", csrf, httponly=False, **options)
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(p=Depends(principal)):
    return {"tenant_id": p.tenant_id, "user_id": p.user_id, "login": p.login, "role": p.role}


@app.post("/api/v1/auth/logout", status_code=204)
def logout(request: Request, response: Response, p=Depends(principal), db=Depends(session)):
    db.execute(
        delete(Session).where(
            Session.token_hash == digest(request.cookies.get("ats_session", "")),
            Session.tenant_id == p.tenant_id,
        )
    )
    db.commit()  # Fail closed: no successful response if revocation fails.
    response.delete_cookie("ats_session", path="/")
    response.delete_cookie("ats_csrf", path="/")


@app.get("/api/v1/contacts")
def contacts(p=Depends(principal), db=Depends(session), offset: int = 0):
    return db.scalars(
        TenantRepository(db, p.tenant_id)
        .query(Contact)
        .order_by(Contact.created_at.desc())
        .offset(max(0, offset))
        .limit(100)
    ).all()


@app.post("/api/v1/contacts", status_code=201)
def add_contact(data: schemas.ContactCreate, p=Depends(admin), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    row = repo.add(Contact, **data.model_dump())
    db.flush()
    repo.add(Audit, actor_id=p.user_id, action="contact.created", entity_id=row.id)
    db.commit()
    return row


@app.patch("/api/v1/contacts/{contact_id}")
def patch_contact(
    contact_id: UUID, data: schemas.ContactPatch, p=Depends(admin), db=Depends(session)
):
    row = TenantRepository(db, p.tenant_id).get(Contact, contact_id, lock=True)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    db.add(
        Audit(
            tenant_id=p.tenant_id,
            actor_id=p.user_id,
            action="contact.consent_changed",
            entity_id=row.id,
        )
    )
    db.commit()
    return row


@app.get("/api/v1/campaigns")
def campaigns(p=Depends(principal), db=Depends(session), offset: int = 0):
    return db.scalars(
        TenantRepository(db, p.tenant_id)
        .query(Campaign)
        .order_by(Campaign.created_at.desc())
        .offset(max(0, offset))
        .limit(100)
    ).all()


@app.post("/api/v1/campaigns", status_code=201)
def add_campaign(
    data: schemas.CampaignCreate,
    response: Response,
    idempotency_key: str = Header(min_length=8, max_length=128),
    p=Depends(admin),
    db=Depends(session),
):
    row, created = create_campaign(db, p, data, idempotency_key)
    db.commit()
    response.status_code = 201 if created else 200
    return row


@app.post("/api/v1/campaigns/{campaign_id}/state")
def campaign_state(
    campaign_id: UUID, data: schemas.CampaignState, p=Depends(admin), db=Depends(session)
):
    # Same tenant mutex as scheduler: pause has a clear allocation boundary.
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(Campaign, campaign_id, lock=True)
    if row.status == "completed":
        raise Problem(409, "campaign_completed")
    if data.status == "running":
        validate_route(p.tenant_id, row.route)
    row.status = data.status
    repo.add(Audit, actor_id=p.user_id, action=f"campaign.{data.status}", entity_id=row.id)
    db.commit()
    return row


@app.get("/api/v1/attempts")
def attempts(p=Depends(principal), db=Depends(session), offset: int = 0):
    return db.scalars(
        TenantRepository(db, p.tenant_id)
        .query(Attempt)
        .order_by(Attempt.created_at.desc())
        .offset(max(0, offset))
        .limit(100)
    ).all()


@app.post("/api/v1/attempts/{attempt_id}/resolve")
def resolve(attempt_id: UUID, data: schemas.ResolveAttempt, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Attempt, attempt_id, lock=True)
    if row.status != "unknown":
        raise Problem(409, "only_unknown_can_be_resolved")
    finish_attempt(db, row, data.status)
    row.reason = data.evidence
    db.add(
        Audit(
            tenant_id=p.tenant_id,
            actor_id=p.user_id,
            action="call.manual_reconciliation",
            entity_id=row.id,
        )
    )
    db.commit()
    return row


@app.get("/api/v1/operations")
def operations(p=Depends(admin), db=Depends(session)):
    def counts(model):
        return dict(
            db.execute(
                select(model.status, func.count())
                .where(model.tenant_id == p.tenant_id)
                .group_by(model.status)
            ).all()
        )

    return {
        "outbox": counts(Outbox),
        "inbox": counts(Inbox),
        "attempts": counts(Attempt),
        "live_calls_enabled": settings().live_calls_enabled,
    }


@app.post("/webhooks/megafon/{tenant_id}")
async def megafon_webhook(tenant_id: UUID, request: Request, db=Depends(session)):
    config = settings()
    form = dict(await request.form())
    expected = config.megafon_crm_token.get_secret_value()
    if (
        not expected
        or tenant_id != config.telephony_tenant_id
        or not secrets.compare_digest(str(form.get("crm_token", "")), expected)
    ):
        raise Problem(403, "webhook_rejected")
    # Never store crm_token or the raw form. History is authoritative for callback outcome.
    cmd = str(form.get("cmd", ""))
    if cmd == "event":
        event = {"RINGING": "accepted"}.get(str(form.get("type", "")).upper())
    elif cmd == "history":
        if str(form.get("status", "")).lower() not in {
            "success",
            "missed",
            "cancel",
            "busy",
            "notavailable",
            "notallowed",
            "notfound",
        }:
            raise Problem(422, "unsupported_history_status")
        event = "completed" if str(form.get("status", "")).lower() == "success" else "failed"
    else:
        raise Problem(422, "unsupported_webhook_command")
    if event is None:
        # ACCEPTED can refer to the operator leg. Do not claim client answered.
        return {"result": "accepted_without_state_change"}
    external_id = str(form.get("callid", ""))
    if not external_id or len(external_id) > 120:
        raise Problem(422, "invalid_callid")
    event_key = hashlib.sha256(f"{external_id}:{cmd}:{event}".encode()).hexdigest()
    ingest(db, tenant_id, "megafon_vats", event_key, external_id, event)
    db.commit()  # Acknowledge only after durable storage.
    return {"result": "success"}


web = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
