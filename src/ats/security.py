import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ats.config import settings
from ats.db import session
from ats.domain import Problem
from ats.models import LoginBucket, Session, User, now

hasher = PasswordHasher()
_dummy = hasher.hash(secrets.token_urlsafe(32))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    role: str
    login: str


def rate_login(db, tenant_id, login, ip):
    # Shared database buckets, fixed 60-second window. Count all attempts, even successful.
    for raw, limit in ((f"ip:{ip}", 30), (f"user:{tenant_id}:{login.lower()}", 5)):
        key = digest(raw)
        db.execute(
            insert(LoginBucket).values(key=key, count=0, reset_at=now()).on_conflict_do_nothing()
        )
        row = db.scalar(select(LoginBucket).where(LoginBucket.key == key).with_for_update())
        if row.reset_at <= now():
            row.count = 0
            row.reset_at = now() + timedelta(seconds=60)
        if row.count >= limit:
            db.commit()
            raise Problem(429, "login_rate_limited")
        row.count += 1
    db.commit()


def verify_password(password, password_hash):
    try:
        return hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def authenticate(db, data, ip):
    rate_login(db, data.tenant_id, data.login, ip)
    user = db.scalar(
        select(User).where(User.tenant_id == data.tenant_id, User.login == data.login.lower())
    )
    valid = verify_password(data.password, user.password_hash if user else _dummy)
    if not valid or not user or not user.active or user.archived:
        raise Problem(401, "invalid_credentials")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        Session(
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=digest(token),
            csrf_hash=digest(csrf),
            expires_at=now() + timedelta(hours=settings().session_hours),
        )
    )
    db.commit()
    return token, csrf


def principal(request: Request, db=Depends(session)) -> Principal:
    token = request.cookies.get("ats_session", "")
    if not token:
        raise Problem(401, "authentication_required")
    row = db.execute(
        select(Session, User)
        .join(User, (Session.user_id == User.id) & (Session.tenant_id == User.tenant_id))
        .where(Session.token_hash == digest(token), Session.expires_at > now(), User.active, ~User.archived)
    ).first()
    if row is None:
        raise Problem(401, "session_expired")
    session_row, user = row
    request.state.tenant_id = user.tenant_id
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not secrets.compare_digest(digest(csrf), session_row.csrf_hash):
            raise Problem(403, "csrf_failed")
    return Principal(user.tenant_id, user.id, user.role, user.login)


def admin(p=Depends(principal)) -> Principal:
    if p.role != "admin":
        raise Problem(403, "admin_required")
    return p
