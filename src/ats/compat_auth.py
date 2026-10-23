"""Original workspace authentication contract over revocable HttpOnly sessions."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ats.config import settings
from ats.db import session
from ats.domain import Problem
from ats.models import Session, Tenant, User
from ats.schemas import Login
from ats.security import authenticate, digest, principal, rate_login

router = APIRouter(prefix="/api/v2", tags=["workspace session"])


class Credentials(BaseModel):
    tenant_id: UUID | None = None
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/login")
def login(data: Credentials, request: Request, response: Response, db=Depends(session)):
    tenant_id = data.tenant_id
    if tenant_id is None:
        # Never discover tenants from a supplied login. A dedicated single-tenant
        # installation retains the original two-field login without tenant leaks.
        tenants = db.scalars(select(Tenant.id).limit(2)).all()
        if len(tenants) != 1:
            rate_login(db, None, data.login, request.client.host)
            raise Problem(422, "tenant_required")
        tenant_id = tenants[0]
    token, csrf = authenticate(
        db, Login(tenant_id=tenant_id, login=data.login, password=data.password), request.client.host
    )
    user = db.scalar(select(User).where(User.tenant_id == tenant_id, User.login == data.login.lower()))
    options = dict(secure=settings().secure_cookies, samesite="strict", path="/", max_age=settings().session_hours * 3600)
    response.set_cookie("ats_session", token, httponly=True, **options)
    response.set_cookie("ats_csrf", csrf, httponly=False, **options)
    return {"ok": True, "token": "cookie-session", "user_id": user.id, "tenant_id": tenant_id, "login": user.login, "role": user.role}


@router.get("/auth/me")
def me(p=Depends(principal)):
    return {"ok": True, "user_id": p.user_id, "tenant_id": p.tenant_id, "login": p.login, "role": p.role}


@router.post("/auth/logout")
def logout(request: Request, response: Response, p=Depends(principal), db=Depends(session)):
    db.execute(delete(Session).where(Session.token_hash == digest(request.cookies.get("ats_session", "")), Session.tenant_id == p.tenant_id))
    db.commit()
    response.delete_cookie("ats_session", path="/")
    response.delete_cookie("ats_csrf", path="/")
    return {"ok": True}


class TicketPurpose(BaseModel):
    purpose: Literal["sse", "export", "recording"] = "sse"
    resource: str = Field(default="", max_length=200)


@router.post("/auth/ticket")
def ticket(data: TicketPurpose, p=Depends(principal)):
    # Browser downloads and SSE now use the same-origin cookie. This marker is
    # explicitly NOT a bearer credential and cannot authenticate another request.
    return {"ok": True, "ticket": "cookie-session", "authentication": "cookie"}
