"""Persist account identity returned by amoCRM, without overwriting changed settings."""

from sqlalchemy import select

from ats.domain import Problem
from ats.models import ConnectionProfile, Tenant


def profile_version(db, tenant_id):
    row = db.execute(select(ConnectionProfile.id, ConnectionProfile.revision).where(
        ConnectionProfile.tenant_id == tenant_id, ConnectionProfile.provider == "amocrm",
    )).first()
    return tuple(row) if row else None


def store_account(db, tenant_id, account, subdomain, expected_version):
    if not isinstance(account, dict) or type(account.get("id")) is not int or not 0 < account["id"] <= 2147483647:
        raise Problem(502, "crm_account_response_invalid", "amoCRM не вернула корректный числовой ID аккаунта.")
    if account.get("subdomain") and account["subdomain"] != subdomain:
        raise Problem(409, "crm_account_domain_mismatch", "Токен amoCRM принадлежит другому аккаунту.")
    # Settings forms already use this mutex; do not race their read/merge/write.
    db.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    row = db.scalar(select(ConnectionProfile).where(
        ConnectionProfile.tenant_id == tenant_id, ConnectionProfile.provider == "amocrm",
    ).with_for_update().execution_options(populate_existing=True))
    version = (row.id, row.revision) if row else None
    if version != expected_version:
        raise Problem(409, "crm_settings_changed", "Настройки изменились во время проверки. Повторите проверку связи.")
    public = dict(row.public_values or {}) if row else {}
    if public.get("account_id") and str(public["account_id"]) != str(account["id"]):
        raise Problem(409, "crm_account_id_mismatch", "Введённый ID аккаунта отличается от ID, полученного от amoCRM. Очистите это поле и повторите проверку.")
    if not row:
        row = ConnectionProfile(tenant_id=tenant_id, provider="amocrm", public_values={})
        db.add(row)
    if public.get("account_id") != account["id"] or public.get("subdomain") != subdomain:
        row.public_values = {**public, "account_id": account["id"], "subdomain": subdomain}
        row.revision = (row.revision or 0) + 1
    db.flush()
    return account["id"]
