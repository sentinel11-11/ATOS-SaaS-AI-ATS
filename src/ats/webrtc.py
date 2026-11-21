"""Private, per-operator browser devices. No provider credentials reach the widget."""

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from ats.connections import cipher
from ats.domain import Problem
from ats.models import OperatorDevice


def ice_servers(config, operator):
    servers = list(config.webrtc_ice_servers)
    if config.webrtc_turn_urls:
        secret = config.webrtc_turn_secret.get_secret_value()
        if not secret or not all(re.fullmatch(r"turns?:[A-Za-z0-9.:[\]-]+(?::[0-9]+)?(?:\?transport=(?:tcp|udp))?", url)
                                 for url in config.webrtc_turn_urls):
            raise Problem(503, "browser_turn_not_configured")
        # Coturn REST authentication. The shared secret never leaves the server.
        username = f"{int(time.time()) + config.callback_max_seconds + 300}:{operator.id.hex}"
        password = base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
        servers.append({"urls": config.webrtc_turn_urls, "username": username, "credential": password})
    return servers


def device_for(db, tenant_id, operator_id):
    return db.scalar(select(OperatorDevice).where(
        OperatorDevice.tenant_id == tenant_id, OperatorDevice.operator_id == operator_id,
        OperatorDevice.enabled,
    ))


def check_device(db, config, operator):
    device = device_for(db, operator.tenant_id, operator.id)
    if (
        not config.webrtc_enabled or not device
        or config.ami_tech != "PJSIP" or config.ami_cli_endpoint_prefix != "mcm-cli-"
        or not re.fullmatch(r"ats-web-[a-f0-9]{12,32}", device.endpoint)
        or not re.fullmatch(r"[A-Za-z0-9.-]{1,200}", config.webrtc_sip_domain)
    ):
        raise Problem(409, "browser_endpoint_not_ready")
    parsed = urlsplit(config.webrtc_wss_url)
    if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Problem(409, "browser_endpoint_not_ready")
    return device


def save_device(db, operator, endpoint, username, password):
    if not re.fullmatch(r"ats-web-[a-f0-9]{12,32}", endpoint) or username != endpoint:
        raise Problem(422, "invalid_browser_endpoint")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
        raise Problem(422, "invalid_browser_secret")
    device = db.scalar(select(OperatorDevice).where(OperatorDevice.operator_id == operator.id))
    if not device:
        device = OperatorDevice(tenant_id=operator.tenant_id, operator_id=operator.id)
        db.add(device)
    raw = json.dumps({"tenant": str(operator.tenant_id), "operator": str(operator.id), "password": password})
    device.endpoint, device.username = endpoint, username
    device.encrypted_secret, device.enabled = cipher().encrypt(raw.encode()).decode(), True
    db.flush()
    return device


def password_for(device):
    try:
        raw = json.loads(cipher().decrypt(device.encrypted_secret.encode()))
        if raw["tenant"] != str(device.tenant_id) or raw["operator"] != str(device.operator_id):
            raise ValueError("binding")
        return raw["password"]
    except (InvalidToken, ValueError, KeyError) as exc:
        raise Problem(503, "browser_credentials_unavailable") from exc
