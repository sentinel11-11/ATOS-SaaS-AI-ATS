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
from starlette.concurrency import run_in_threadpool

from ats import schemas
from ats.api_recording_links import router as recording_links_router
from ats.compat_auth import router as compat_auth_router
from ats.compat_campaigns import router as compat_campaigns_router
from ats.compat_contacts import router as compat_contacts_router
from ats.compat_observability import router as compat_observability_router
from ats.compat_resources import router as compat_resources_router
from ats.compat_settings import router as compat_settings_router
from ats.config import settings
from ats.db import session
from ats.domain import Problem
from ats.events import router as events_router
from ats.models import Attempt, Audit, Campaign, Contact, Inbox, Outbox, Session, Tenant
from ats.repositories import TenantRepository
from ats.security import admin, authenticate, digest, principal
from ats.services import create_campaign, finish_attempt, ingest, validate_route
from ats.workspace_api import router as workspace_router

log = logging.getLogger("ats.api")
app = FastAPI(title="ATS Beta", version="0.3.0")


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
            limit = (
                11 * 1024 * 1024
                if request.url.path.startswith("/api/v1/workspace/calls/")
                and request.url.path.endswith("/recordings")
                else 6 * 1024 * 1024
                if request.url.path == "/api/v1/workspace/contacts-import-file"
                else 256 * 1024
            )
            if size > limit:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            parts.append(part)
        request._body = b"".join(parts)
    response = await call_next(request)
    route_path = getattr(request.scope.get("route"), "path", "unmatched")
    if unsafe or response.status_code >= 400:
        log.info(
            "http_request request_id=%s method=%s path=%s status=%s",
            request.state.request_id,
            request.method,
            route_path,
            response.status_code,
        )
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id and response.status_code < 500:
            from ats.db import session_factory
            from ats.models import RuntimeLog

            def persist_request_log():
                try:
                    with session_factory().begin() as db:
                        db.add(
                            RuntimeLog(
                                tenant_id=tenant_id,
                                level="WARNING" if response.status_code >= 400 else "INFO",
                                component="api",
                                code=getattr(request.state, "error_code", "http_request"),
                                request_id=UUID(request.state.request_id),
                                method=request.method,
                                path=route_path[:200],
                                http_status=response.status_code,
                            )
                        )
                except SQLAlchemyError:
                    log.error("request_log_unavailable request_id=%s", request.state.request_id)

            await run_in_threadpool(persist_request_log)
    response.headers.update(
        {
            "X-Request-ID": request.state.request_id,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; style-src-attr 'unsafe-inline'; frame-ancestors 'none'",
        }
    )
    return response


@app.exception_handler(Problem)
async def problem_handler(request, exc):
    request.state.error_code = exc.code
    return JSONResponse(
        {"error": exc.code, "request_id": request.state.request_id}, status_code=exc.status
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    request.state.error_code = "validation_failed"
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
    if version != "0005":
        raise Problem(503, "schema_migration_required")
    db.scalar(select(func.count()).select_from(Tenant))
    return {"status": "ready", "live_calls": settings().live_calls_enabled, "version": app.version}


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
        .where(~Contact.archived)
        .order_by(Contact.created_at.desc())
        .offset(max(0, offset))
        .limit(100)
    ).all()


@app.post("/api/v1/contacts", status_code=201)
def add_contact(data: schemas.ContactCreate, p=Depends(admin), db=Depends(session)):
    from ats.models import BlacklistEntry

    repo = TenantRepository(db, p.tenant_id)
    blocked = (
        db.scalar(repo.query(BlacklistEntry).where(BlacklistEntry.phone == data.phone)) is not None
    )
    row = repo.add(Contact, blocked=blocked, **data.model_dump())
    db.flush()
    repo.add(Audit, actor_id=p.user_id, action="contact.created", entity_id=row.id)
    db.commit()
    return row


@app.patch("/api/v1/contacts/{contact_id}")
def patch_contact(
    contact_id: UUID, data: schemas.ContactPatch, p=Depends(admin), db=Depends(session)
):
    row = TenantRepository(db, p.tenant_id).get(Contact, contact_id, lock=True)
    from ats.models import BlacklistEntry

    if not data.blocked and db.scalar(
        TenantRepository(db, p.tenant_id)
        .query(BlacklistEntry)
        .where(BlacklistEntry.phone == row.phone)
    ):
        raise Problem(409, "phone_blacklisted")
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
        .where(~Campaign.archived)
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
    if row.archived:
        raise Problem(409, "campaign_archived")
    if row.status == "completed":
        raise Problem(409, "campaign_completed")
    if data.status == "running":
        validate_route(p.tenant_id, row.route, db)
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
    if cmd == "contact":
        from ats.inbound import normalize_phone

        contact = db.scalar(
            select(Contact).where(
                Contact.tenant_id == tenant_id,
                Contact.phone == normalize_phone(form.get("phone", "")),
                ~Contact.archived,
            )
        )
        return {"contact_name": contact.name if contact else ""}
    direction = (
        str(form.get("direction", "")).lower()
        if cmd == "event"
        else str(form.get("type", "")).lower()
    )
    if direction == "in" and config.inbound_enabled:
        from ats.inbound import ingest_inbound

        if cmd == "history" and str(form.get("status", "")).lower() not in {
            "success",
            "missed",
            "cancel",
            "busy",
            "notavailable",
            "notallowed",
            "notfound",
        }:
            raise Problem(422, "unsupported_history_status")

        state = (
            {"INCOMING": "ringing", "ACCEPTED": "answered"}.get(str(form.get("type", "")).upper())
            if cmd == "event"
            else "completed"
            if str(form.get("status", "")).lower() == "success"
            else "failed"
            if cmd == "history"
            else None
        )
        if not state:
            raise Problem(422, "unsupported_inbound_event")
        try:
            duration = int(form.get("duration", "0"))
        except (ValueError, TypeError):
            raise Problem(422, "invalid_duration") from None
        ingest_inbound(
            db,
            tenant_id,
            "megafon_vats",
            str(form.get("callid", "")),
            form.get("phone", ""),
            form.get("telnum", ""),
            state,
            duration,
        )
        db.commit()
        return {"result": "success"}
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


app.include_router(workspace_router)
app.include_router(compat_auth_router)
app.include_router(compat_contacts_router)
app.include_router(compat_campaigns_router)
app.include_router(compat_resources_router)
app.include_router(compat_observability_router)
app.include_router(compat_settings_router)
app.include_router(events_router)
app.include_router(recording_links_router)

web = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
