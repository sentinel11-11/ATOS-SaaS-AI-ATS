"""Tenant connection profiles; public configuration and encrypted credentials are separate.

Deployment switches and destination allowlists are deliberately not editable here. A saved
profile never enables live calls or grants a tenant access to another tenant's integration.
"""

import ipaddress
import json
import re
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from ats.config import Settings, settings
from ats.domain import Problem
from ats.models import ConnectionProfile

MASK = "********"
SECRETS = {
    "ami": {"secret"},
    "megafon_vats": {"api_key", "crm_token"},
    "multicom": {"api_key", "sip_secret", "webhook_secret"},
    "amocrm": {"access_token", "refresh_token", "client_secret", "webhook_token"},
    "uis": {"api_key"},
    "llm": {"api_key"},
    "bitrix24": {"webhook_url"},
    "records": {"link_secret"},
    "crm": set(),
}
FIELDS = {
    "ami": {"host", "port", "user", "secret", "tls", "trunk", "tech", "number_format",
            "caller_id_format", "context", "answer_context", "inbound_contexts", "record_calls", "play_message",
            "inbound_enabled", "dial_prefix", "caller_id"},
    "megafon_vats": {"base_url", "api_key", "crm_token", "api_key_env", "crm_token_env",
                     "default_user", "default_group", "timeout_sec"},
    "multicom": {"api_url", "api_key", "api_key_env", "account_id", "sip_host", "sip_user",
                 "sip_secret", "timeout_sec", "default_user", "webhook_secret", "record"},
    "amocrm": {"subdomain", "access_token", "access_token_env", "refresh_token", "client_id",
               "client_secret", "webhook_token", "responsible_user_id", "auto_create_contacts",
               "auto_create_tasks", "unsorted_on_new", "timeout_sec", "operator_user_map",
               "disabled", "task_due_hours"},
    "uis": {"api_url", "api_key", "number_pool_api"},
    "llm": {"enabled", "base_url", "api_key", "api_key_env", "model"},
    "bitrix24": {"webhook_url"},
    "records": {"base_url", "link_ttl_hours", "link_secret", "link_secret_env"},
    "crm": {"driver"},
}
BOOLEAN_FIELDS = {"tls", "record_calls", "play_message", "inbound_enabled", "record",
                  "auto_create_contacts", "auto_create_tasks", "unsorted_on_new", "disabled",
                  "enabled"}
ENV_FIELDS = {
    ("megafon_vats", "api_key_env"): "ATS_MEGAFON_API_KEY",
    ("megafon_vats", "crm_token_env"): "ATS_MEGAFON_CRM_TOKEN",
    ("multicom", "api_key_env"): "ATS_MULTICOM_API_KEY",
    ("amocrm", "access_token_env"): "ATS_AMOCRM_TOKEN",
    ("llm", "api_key_env"): "ATS_LLM_KEY",
    ("records", "link_secret_env"): "ATS_RECORDS_LINK_TOKEN",
}


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}:
        return value.strip().lower() in {"true", "1", "yes"}
    raise Problem(422, "invalid_boolean")


def masked(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[\*•]+", value.strip()))


def cipher():
    key = settings().connection_encryption_key.get_secret_value()
    if not key:
        raise Problem(503, "connection_encryption_key_required")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise Problem(503, "connection_encryption_key_invalid") from exc


def decrypt_profile(row):
    if not row.encrypted_secrets:
        return {}
    try:
        payload = json.loads(cipher().decrypt(row.encrypted_secrets.encode("ascii")))
        if payload.get("tenant") != str(row.tenant_id) or payload.get("provider") != row.provider:
            raise ValueError("context")
        values = payload["values"]
        if not isinstance(values, dict) or set(values) - SECRETS[row.provider]:
            raise ValueError("shape")
        return values
    except (InvalidToken, ValueError, KeyError, UnicodeError) as exc:
        raise Problem(503, "connection_credentials_unavailable") from exc


def _encrypt(tenant_id, provider, values):
    payload = {"tenant": str(tenant_id), "provider": provider, "values": values}
    return cipher().encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode("ascii")


def _public_url(value, suffixes=None):
    if not value:
        return ""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
        raise Problem(422, "provider_url_requires_https")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise Problem(422, "provider_url_requires_public_domain")
    if suffixes and not any(host == s or host.endswith("." + s) for s in suffixes):
        raise Problem(422, "provider_domain_rejected")
    if host in {"localhost", "localhost.localdomain"} or "." not in host:
        raise Problem(422, "provider_url_requires_public_domain")
    return value.rstrip("/")


def _validate(provider, field, value):
    if field in BOOLEAN_FIELDS:
        return truthy(value)
    if field == "operator_user_map":
        if not isinstance(value, dict) or len(value) > 1000:
            raise Problem(422, "invalid_operator_user_map")
        try:
            result = {str(k): int(v) for k, v in value.items()}
        except (TypeError, ValueError) as exc:
            raise Problem(422, "invalid_operator_user_map") from exc
        if any(not re.fullmatch(r"[a-zA-Z0-9_.@-]{1,120}", k) or v <= 0 for k, v in result.items()):
            raise Problem(422, "invalid_operator_user_map")
        return result
    if field in {"port", "timeout_sec", "responsible_user_id", "link_ttl_hours", "task_due_hours"}:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise Problem(422, "invalid_provider_number") from exc
        low, high = {"port": (1, 65535), "timeout_sec": (1, 120),
                     "responsible_user_id": (1, 2147483647), "link_ttl_hours": (0, 8760),
                     "task_due_hours": (1, 168)}[field]
        if not low <= number <= high:
            raise Problem(422, "invalid_provider_number")
        return number
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise Problem(422, "invalid_provider_value")
    value = value.strip()
    if field in SECRETS[provider]:
        return value
    if (provider, field) in ENV_FIELDS:
        if value not in {"", ENV_FIELDS[provider, field]}:
            raise Problem(422, "secret_environment_reference_rejected")
    if field in {"base_url", "api_url", "number_pool_api"}:
        suffixes = {"megafon_vats": ("megapbx.ru", "megafon.ru"),
                    "multicom": ("multicom.ru", "mcm.ru"),
                    "uis": ("uiscom.ru", "comagic.ru")}.get(provider)
        value = _public_url(value, suffixes)
    if field == "subdomain" and value and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise Problem(422, "invalid_amocrm_subdomain")
    if provider == "ami" and field in {"host", "user", "trunk", "context", "answer_context", "dial_prefix", "inbound_contexts"}:
        if value and not re.fullmatch(r"[a-zA-Z0-9_.:,\-]{1,200}", value):
            raise Problem(422, "invalid_ami_identifier")
    if provider == "ami" and field == "tech" and value not in {"PJSIP", "SIP"}:
        raise Problem(422, "invalid_ami_technology")
    if provider == "ami" and field == "number_format" and value not in {"ru8", "e164", "digits", "ru10"}:
        raise Problem(422, "invalid_number_format")
    if provider == "ami" and field == "caller_id_format" and value not in {"d10", "ru8", "e164", "digits"}:
        raise Problem(422, "invalid_caller_id_format")
    if provider == "crm" and field == "driver" and value not in {"amocrm", "bitrix24", "", "none"}:
        raise Problem(422, "invalid_crm_driver")
    return value


def _profiles(db, tenant_id):
    return db.scalars(select(ConnectionProfile).where(ConnectionProfile.tenant_id == tenant_id)).all()


def _defaults(config, tenant_id):
    phone_bound = config.telephony_tenant_id == tenant_id
    crm_bound = config.amo_tenant_id == tenant_id
    return {
        "ami": {"host": config.ami_host if phone_bound else "127.0.0.1",
                "port": config.ami_port if phone_bound else 5038,
                "user": config.ami_user if phone_bound else "ats-beta", "tls": config.ami_tls if phone_bound else False,
                "secret": MASK if phone_bound and config.ami_secret.get_secret_value() else "",
                "trunk": getattr(config, "ami_trunk", "mcm"), "tech": "PJSIP", "number_format": "ru8",
                "caller_id_format": "d10", "context": getattr(config, "ami_context", "ats-beta"),
                "answer_context": getattr(config, "ami_answer_context", "ats-beta-answer"),
                "inbound_contexts": config.inbound_contexts, "record_calls": True, "play_message": True,
                "inbound_enabled": config.inbound_enabled if phone_bound else False, "dial_prefix": ""},
        "megafon_vats": {"base_url": config.megafon_url if phone_bound else "", "api_key": MASK if phone_bound and config.megafon_api_key.get_secret_value() else "",
                         "crm_token": MASK if phone_bound and config.megafon_crm_token.get_secret_value() else "",
                         "api_key_env": "ATS_MEGAFON_API_KEY", "crm_token_env": "ATS_MEGAFON_CRM_TOKEN",
                         "default_user": config.megafon_user if phone_bound else "", "default_group": "", "timeout_sec": 15},
        "amocrm": {"subdomain": urlsplit(config.amo_url).hostname.split(".")[0] if crm_bound and urlsplit(config.amo_url).hostname else "",
                   "access_token": MASK if crm_bound and config.amo_access_token.get_secret_value() else "",
                   "access_token_env": "ATS_AMOCRM_TOKEN", "responsible_user_id": 1,
                   "auto_create_contacts": False, "auto_create_tasks": False,
                   "unsorted_on_new": False, "timeout_sec": 15, "operator_user_map": {}},
        "multicom": {"api_url": "https://api.multicom.ru/v1", "api_key": "", "api_key_env": "ATS_MULTICOM_API_KEY",
                     "account_id": "", "sip_host": "sip.multicom.ru", "sip_user": "", "sip_secret": "", "timeout_sec": 15},
        "uis": {"api_url": "", "api_key": "", "number_pool_api": ""},
        "llm": {"enabled": False, "base_url": "", "api_key_env": "ATS_LLM_KEY", "model": ""},
        "bitrix24": {"webhook_url": ""},
        "crm": {"driver": "amocrm"},
        "records": {"base_url": "", "link_ttl_hours": 72, "link_secret": "", "link_secret_env": "ATS_RECORDS_LINK_TOKEN"},
    }


def public_provider_config(db, tenant_id):
    result = _defaults(settings(), tenant_id)
    for row in _profiles(db, tenant_id):
        if row.provider not in FIELDS:
            continue
        result[row.provider].update(row.public_values or {})
        # Returning flags does not require decryption. Key loss must not expose credentials.
        for field in (row.public_values or {}).get("_secret_fields", []):
            result[row.provider][field] = MASK
        result[row.provider].pop("_secret_fields", None)
    return result


def save_provider_config(db, tenant_id, incoming):
    if not isinstance(incoming, dict) or set(incoming) - set(FIELDS):
        raise Problem(422, "invalid_provider_sections")
    existing = {row.provider: row for row in _profiles(db, tenant_id)}
    public = public_provider_config(db, tenant_id)
    for provider, values in incoming.items():
        if not isinstance(values, dict) or set(values) - FIELDS[provider]:
            raise Problem(422, "invalid_provider_fields")
        row = existing.get(provider)
        saved_public = dict(row.public_values or {}) if row else {}
        secrets = None
        for field, raw in values.items():
            value = _validate(provider, field, raw)
            if field in SECRETS[provider]:
                if masked(value):
                    if not public[provider].get(field):
                        raise Problem(422, "secret_missing_" + field)
                    continue
                if not value and not public[provider].get(field):
                    continue
                if secrets is None:
                    secrets = decrypt_profile(row) if row else {}
                secrets[field] = value
            else:
                saved_public[field] = value
        # Remote AMI is permitted only with TLS. This is checked after merging partial edits.
        merged = {**public[provider], **saved_public}
        if provider == "ami" and merged.get("host") not in {"localhost", "127.0.0.1", "::1"} and not merged.get("tls", False):
            raise Problem(422, "remote_ami_requires_tls")
        if row is None:
            row = ConnectionProfile(tenant_id=tenant_id, provider=provider, public_values={})
            db.add(row)
        if secrets is not None:
            row.encrypted_secrets = _encrypt(tenant_id, provider, secrets)
            saved_public["_secret_fields"] = sorted(k for k, v in secrets.items() if v)
        row.public_values = saved_public
        row.revision = (row.revision or 0) + 1
    db.flush()


def settings_for_tenant(db, tenant_id):
    """Resolve credentials at job boundaries; callers must not hold locks during network I/O."""
    base = settings()
    profiles = {row.provider: row for row in _profiles(db, tenant_id)}
    updates = {}
    mappings = {
        "ami": {"host": "ami_host", "port": "ami_port", "user": "ami_user", "secret": "ami_secret",
                "tls": "ami_tls", "caller_id": "ami_caller_id", "trunk": "ami_trunk", "context": "ami_context",
                "answer_context": "ami_answer_context",
                "tech": "ami_tech", "number_format": "ami_number_format", "caller_id_format": "ami_caller_id_format",
                "dial_prefix": "ami_dial_prefix", "play_message": "ami_play_message", "record_calls": "ami_record_calls",
                "inbound_contexts": "inbound_contexts", "inbound_enabled": "inbound_enabled"},
        "megafon_vats": {"base_url": "megafon_url", "api_key": "megafon_api_key", "crm_token": "megafon_crm_token", "default_user": "megafon_user"},
        "amocrm": {"access_token": "amo_access_token"},
    }
    for provider, row in profiles.items():
        values = {**(row.public_values or {}), **decrypt_profile(row)}
        for source, target in mappings.get(provider, {}).items():
            if source in values and target in Settings.model_fields:
                value = values[source]
                updates[target] = SecretStr(value) if source in SECRETS[provider] else value
        if provider == "amocrm" and values.get("subdomain"):
            updates["amo_url"] = "https://" + values["subdomain"] + ".amocrm.ru"
    try:
        # model_validate rechecks remote TLS and the deployment guard after the overlay.
        return Settings.model_validate({**base.model_dump(), **updates})
    except ValidationError as exc:
        raise Problem(503, "connection_profile_invalid") from exc
