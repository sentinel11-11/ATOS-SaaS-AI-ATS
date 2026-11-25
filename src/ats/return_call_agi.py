"""Private API on the Asterisk-only Unix socket."""

import logging
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from ats.config import settings
from ats.db import session
from ats.inbound_callback import answer_route, release_route, reserve_route
from ats.models import InboundCallback, Tenant

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
log = logging.getLogger("ats.return_call")


@app.exception_handler(SQLAlchemyError)
async def storage_unavailable(_request: Request, exc: SQLAlchemyError):
    log.warning("return_call_storage_unavailable type=%s", type(exc).__name__)
    return JSONResponse({"error": "storage_unavailable"}, status_code=503)


class IncomingCall(BaseModel):
    external_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    phone: str = Field(max_length=32, pattern=r"^[+0-9 ()-]*$")
    destination: str = Field(max_length=32)


def limited_session(db=Depends(session)):
    db.execute(text("SET LOCAL lock_timeout = '350ms'"))
    db.execute(text("SET LOCAL statement_timeout = '700ms'"))
    return db


@app.post("/resolve")
def resolve(data: IncomingCall, db=Depends(limited_session)):
    tenant_id = settings().telephony_tenant_id
    result = reserve_route(db, tenant_id, data.external_id, data.phone, data.destination) if tenant_id else {}
    db.commit()
    return result


def get_route(db, ident):
    tenant_id = settings().telephony_tenant_id
    if tenant_id:
        db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        return db.scalar(select(InboundCallback).where(
            InboundCallback.inbound_id == ident, InboundCallback.tenant_id == tenant_id,
        ))
    return None


@app.post("/answer/{ident}")
def answer(ident: UUID, db=Depends(limited_session)):
    route = get_route(db, ident)
    result = bool(route and answer_route(db, route, settings()))
    db.commit()
    return {"answered": result}


@app.post("/release/{ident}")
def release(ident: UUID, hangup: bool = False, db=Depends(limited_session)):
    route = get_route(db, ident)
    if route:
        release_route(db, route, hangup=hangup)
    db.commit()
    return {}
