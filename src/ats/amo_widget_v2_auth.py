"""Validate amoCRM widget requests automatically via the SDK's signed X-Auth-Token.

This endpoint identifies the user but deliberately does not originate calls:
an operator-first Asterisk bridge and call lifecycle are separate requirements.
"""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select

from ats.config import settings
from ats.connections import settings_for_tenant
from ats.db import session
from ats.domain import Problem
from ats.models import ConnectionProfile, Operator, User, WorkspaceSettings
from ats.repositories import TenantRepository

router = APIRouter(prefix="/api/v1/amo/widget/v2", tags=["amoCRM widget v2"])


def _b64(encoded: str) -> bytes:
    if not encoded or len(encoded) > 8192:
        raise Problem(401, "widget_token_invalid")
    try:
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise Problem(401, "widget_token_invalid") from exc


def verify_amo_widget_token(
    raw_token: str, *, secret: str, account_id: int, client_uuid: str,
    audience: str, issuer: str, origin: str, clock: int | None = None,
) -> dict:
    """HS256, lifetime, issuer, audience, account, integration and user checks."""
    if not secret or not audience or account_id <= 0 or not client_uuid or not issuer:
        raise Problem(503, "amo_widget_auth_not_configured")
    if not isinstance(raw_token, str) or len(raw_token) > 8192:
        raise Problem(401, "widget_token_invalid")
    segments = raw_token.split(".")
    if len(segments) != 3:
        raise Problem(401, "widget_token_invalid")
    header_raw, payload_raw, signature_raw = segments
    try:
        header = json.loads(_b64(header_raw))
        claims = json.loads(_b64(payload_raw))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise Problem(401, "widget_token_invalid") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256" or not isinstance(claims, dict):
        raise Problem(401, "widget_token_invalid")
    message = (header_raw + "." + payload_raw).encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    if not hmac.compare_digest(_b64(signature_raw), expected):
        raise Problem(401, "widget_token_invalid")
    now = int(time.time()) if clock is None else clock
    if any(type(claims.get(k)) is not int for k in ("iat", "nbf", "exp", "account_id", "user_id")):
        raise Problem(401, "widget_token_invalid")
    if not (
        claims["iat"] <= now + 30 and claims["nbf"] <= now + 30
        and now < claims["exp"] and 0 < claims["exp"] - claims["iat"] <= 3600
    ):
        raise Problem(401, "widget_token_expired")
    if not claims.get("jti") or not isinstance(claims["jti"], str):
        raise Problem(401, "widget_token_invalid")
    if (
        claims["account_id"] != account_id or claims["user_id"] <= 0
        or claims.get("client_uuid") != client_uuid or claims.get("aud") != audience
        or claims.get("iss") != issuer or origin != issuer
    ):
        raise Problem(403, "amo_widget_identity_mismatch")
    return claims


@router.get("/me")
def widget_me(
    request: Request,
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
    db=Depends(session),
):
    base = settings()
    if not base.amo_tenant_id:
        raise Problem(503, "crm_tenant_not_bound")
    config = settings_for_tenant(db, base.amo_tenant_id)
    if not config.crm_enabled or config.amo_tenant_id != base.amo_tenant_id:
        raise Problem(503, "crm_tenant_not_bound")
    if not x_auth_token:
        raise Problem(401, "widget_token_required")
    issuer = config.amo_url.removesuffix("/").lower()
    origin = request.headers.get("origin", "")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or parsed.netloc.lower() != urlsplit(issuer).netloc.lower():
        raise Problem(403, "widget_origin_rejected")
    claims = verify_amo_widget_token(
        x_auth_token,
        secret=base.amo_widget_signing_secret.get_secret_value(),
        account_id=base.amo_widget_account_id,
        client_uuid=base.amo_widget_client_uuid,
        audience=base.amo_widget_audience,
        issuer=issuer,
        origin=origin.lower(),
    )
    repo = TenantRepository(db, base.amo_tenant_id)
    workspace = db.scalar(repo.query(WorkspaceSettings))
    mapping = dict((workspace.values if workspace else {}).get("operator_user_map") or {})
    profile = db.scalar(
        select(ConnectionProfile).where(
            ConnectionProfile.tenant_id == base.amo_tenant_id,
            ConnectionProfile.provider == "amocrm",
        )
    )
    if profile:
        mapping.update((profile.public_values or {}).get("operator_user_map") or {})
    matches = [
        str(ext) for ext, amo_id in mapping.items() if str(amo_id) == str(claims["user_id"])
    ]
    if len(matches) != 1:
        raise Problem(409, "amo_user_operator_mapping_missing")
    operators = db.execute(
        select(Operator, User)
        .join(User, (User.id == Operator.user_id) & (User.tenant_id == Operator.tenant_id))
        .where(
            Operator.tenant_id == base.amo_tenant_id,
            Operator.extension == matches[0],
            User.active,
            ~User.archived,
            User.role == "operator",
        )
    ).all()
    if len(operators) != 1:
        raise Problem(409, "operator_selection_required")
    op, _user = operators[0]
    return {
        "ok": True,
        "operator_id": str(op.id),
        "operator_name": op.name,
        "operator_extension": op.extension,
        "operator_status": op.status,
        "forward_number_masked": None,
        "call_modes": {"browser": False, "forward": False},
        "telephony_ready": False,
        "reason": "operator_bridge_not_ready",
    }
