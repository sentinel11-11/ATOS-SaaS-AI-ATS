"""Verify amoCRM $authorizedAjax X-Auth-Token using the official disposable-token contract.

Reference: amocrm/amocrm-api-php AmoCRMOAuth::parseDisposableToken,
DisposableTokenModel (a0072fec651b84500c6767f98bbab6d1c8df9ce4).
"""

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from fastapi import Depends, Request
from sqlalchemy import select

from ats.config import settings
from ats.connections import decrypt_profile, settings_for_tenant
from ats.db import session
from ats.domain import Problem
from ats.models import ConnectionProfile, Operator, User
from ats.security import Principal


@dataclass(frozen=True)
class WidgetPrincipal:
    p: Principal
    operator_id: UUID
    amo_user_id: int
    account_id: int


def widget_principal(request: Request, db=Depends(session)):
    tenant_id = settings().amo_tenant_id
    if not tenant_id:
        raise Problem(503, "crm_tenant_not_bound")
    config = settings_for_tenant(db, tenant_id)
    origin = request.headers.get("origin", "")
    if not config.amo_url or origin != config.amo_url:
        raise Problem(403, "amo_widget_origin_rejected")
    row = db.scalar(select(ConnectionProfile).where(
        ConnectionProfile.tenant_id == tenant_id, ConnectionProfile.provider == "amocrm",
    ))
    public = row.public_values if row else {}
    secret = decrypt_profile(row).get("client_secret", "") if row else ""
    client_id, account_id = public.get("client_id"), public.get("account_id")
    if not config.crm_enabled or public.get("disabled") or not client_id or not account_id or not secret:
        raise Problem(503, "widget_auth_not_configured", "Сохраните ID интеграции, секретный ключ и ID аккаунта amoCRM в ATOS.")
    token = request.headers.get("x-auth-token", "")
    if not token or len(token) > 16384:
        raise Problem(401, "authentication_required")
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], audience=config.public_origin.rstrip("/"),
            leeway=15,
            options={"require": ["exp", "aud", "client_uuid", "account_id", "user_id", "subdomain", "jti"]},
        )
        if (
            str(claims["client_uuid"]) != str(client_id)
            or type(claims["account_id"]) is not int or claims["account_id"] != int(account_id)
            or type(claims["user_id"]) is not int or claims["user_id"] <= 0
            or claims["subdomain"] != urlsplit(origin).hostname.split(".")[0]
        ):
            raise ValueError("binding")
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
        raise Problem(401, "amo_widget_token_invalid") from exc
    user_map = (public or {}).get("operator_user_map", {})
    extensions = [str(ext) for ext, ident in user_map.items() if str(ident) == str(claims["user_id"])]
    if len(extensions) != 1:
        raise Problem(409, "operator_mapping_missing")
    found = db.execute(select(Operator, User).join(User, (
        (User.id == Operator.user_id) & (User.tenant_id == Operator.tenant_id)
    )).where(
        Operator.tenant_id == tenant_id, Operator.extension == extensions[0],
        User.active, ~User.archived, User.role == "operator",
    )).all()
    if len(found) != 1:
        raise Problem(409, "operator_mapping_missing")
    operator, user = found[0]
    request.state.tenant_id = tenant_id
    return WidgetPrincipal(Principal(tenant_id, user.id, user.role, user.login),
                           operator.id, claims["user_id"], int(account_id))
