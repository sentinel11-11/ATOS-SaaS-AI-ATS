"""Bounded read-only directory synchronisation; provider JSON is projected to public fields."""

import httpx
from sqlalchemy import select

from ats.adapters import Rejected, Retryable, https_origin
from ats.models import CallerNumber, DirectoryEntry, now


def _caller_phone(value):
    """Normalize a provider number to the ten-digit Russian caller-id form."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 10:
        return digits
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return digits[1:]
    return ""


def fetch_directory(config, provider, kind):
    if provider == "amocrm" and kind == "users":
        base, path = https_origin(config.amo_url), "/api/v4/users"
        headers = {"Authorization": "Bearer " + config.amo_access_token.get_secret_value()}
    elif provider == "megafon" and kind in {"users", "groups", "numbers"}:
        base = https_origin(config.megafon_url)
        path = "/crmapi/v1/" + ("telnums" if kind == "numbers" else kind)
        headers = {"X-API-KEY": config.megafon_api_key.get_secret_value()}
    else:
        raise Rejected("unsupported_directory")
    output = []
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        for page in range(20):
            params = (
                {"page": page + 1, "limit": 100}
                if provider == "amocrm"
                else {"start": page * 100, "limit": 100}
            )
            try:
                response = client.get(base + path, headers=headers, params=params)
            except httpx.TransportError as exc:
                raise Retryable("directory_unreachable") from exc
            if response.status_code == 204:
                break
            if response.status_code != 200:
                raise Rejected(f"directory_http_{response.status_code}")
            try:
                raw = response.json()
                items = (
                    raw.get("_embedded", {}).get("users", [])
                    if provider == "amocrm"
                    else raw.get("items", [])
                )
                if not isinstance(items, list):
                    raise ValueError("directory shape")
                for item in items:
                    ident = str(
                        item.get("id")
                        or item.get("login")
                        or item.get("telnum")
                        or item.get("number")
                        or ""
                    )[:120]
                    if ident:
                        phone_value = item.get("telnum") or item.get("number") or item.get("ext") or ""
                        output.append(
                            {
                                "external_id": ident,
                                "name": str(item.get("name") or ident)[:120],
                                "phone": str(phone_value)[:32],
                            }
                        )
            except (ValueError, TypeError, AttributeError) as exc:
                raise Rejected("invalid_directory_response") from exc
            if len(items) < 100:
                break
        else:
            raise Rejected("directory_limit_2000")
    return output


def store_directory(db, tenant_id, provider, kind, values):
    for value in values:
        row = db.scalar(
            select(DirectoryEntry)
            .where(
                DirectoryEntry.tenant_id == tenant_id,
                DirectoryEntry.provider == provider,
                DirectoryEntry.kind == kind,
                DirectoryEntry.external_id == value["external_id"],
            )
            .with_for_update()
        )
        if row:
            row.name, row.phone, row.updated_at = value["name"], value["phone"], now()
        else:
            db.add(DirectoryEntry(tenant_id=tenant_id, provider=provider, kind=kind, **value))
        if provider == "megafon" and kind == "numbers":
            phone = _caller_phone(value.get("phone"))
            if not phone:
                continue
            number = db.scalar(
                select(CallerNumber)
                .where(CallerNumber.tenant_id == tenant_id, CallerNumber.phone == phone)
                .with_for_update()
            )
            if number is None:
                db.add(
                    CallerNumber(
                        tenant_id=tenant_id,
                        phone=phone,
                        label=value["name"],
                        provider="megafon_vats",
                        provider_ref=value["external_id"],
                        verified=True,
                        enabled=True,
                        enabled_outgoing=True,
                    )
                )
            elif number.provider == "sim":
                # Adopt legacy/imported rows without overwriting administrator settings.
                number.provider = "megafon_vats"
                number.provider_ref = value["external_id"]
                number.verified = True
    # Entries absent from a bounded snapshot are retained; transient failures cannot erase mappings.
