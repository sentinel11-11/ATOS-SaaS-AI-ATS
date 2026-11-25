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
from ats.domain import ACTIVE, Problem
from ats.models import Attempt, Campaign, CampaignItem, OperatorDevice, QueueEntry, WidgetCall

SESSION_SLOTS = 8


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


def devices_for(db, tenant_id, operator_id):
    return db.scalars(select(OperatorDevice).where(
        OperatorDevice.tenant_id == tenant_id, OperatorDevice.operator_id == operator_id,
        OperatorDevice.enabled,
    ).order_by(OperatorDevice.slot).execution_options(populate_existing=True)).all()


def device_for(db, tenant_id, operator_id, session_id=None):
    devices = devices_for(db, tenant_id, operator_id)
    return next((device for device in devices if session_id is None or device.agent_session_id == session_id), None)


def pending_calls(db, operator):
    return db.scalars(select(WidgetCall).join(Campaign, Campaign.id == WidgetCall.campaign_id).where(
        WidgetCall.tenant_id == operator.tenant_id, WidgetCall.operator_id == operator.id,
        Campaign.status.in_(["draft", "running"]), ~Campaign.archived,
    )).all()


def browser_operator_available(db, operator):
    if operator.status == "free":
        return True
    if operator.status != "busy":
        return False
    # Share only private browser devices. Physical phones and ordinary ACD calls
    # still reserve the whole operator, including attempts awaiting reconciliation.
    calls = pending_calls(db, operator)
    if not calls or any(call.mode != "browser" or not call.browser_session_id for call in calls):
        return False
    active = db.execute(select(WidgetCall.id, WidgetCall.mode, WidgetCall.browser_session_id).select_from(QueueEntry)
        .join(Attempt, Attempt.id == QueueEntry.attempt_id)
        .join(CampaignItem, CampaignItem.id == Attempt.item_id)
        .outerjoin(WidgetCall, WidgetCall.campaign_id == CampaignItem.campaign_id).where(
            QueueEntry.tenant_id == operator.tenant_id, QueueEntry.operator_id == operator.id,
            Attempt.status.in_(ACTIVE),
        )).all()
    return bool(active) and all(row.id and row.mode == "browser" and row.browser_session_id for row in active)


def check_device(db, config, operator, session_id=None):
    device = device_for(db, operator.tenant_id, operator.id, session_id)
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


def save_device(db, operator, endpoint, username, password, slot=0):
    if not re.fullmatch(r"ats-web-[a-f0-9]{12,32}", endpoint) or username != endpoint:
        raise Problem(422, "invalid_browser_endpoint")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
        raise Problem(422, "invalid_browser_secret")
    if not 0 <= slot < SESSION_SLOTS:
        raise Problem(422, "invalid_browser_slot")
    device = db.scalar(select(OperatorDevice).where(OperatorDevice.operator_id == operator.id, OperatorDevice.slot == slot))
    if not device:
        device = OperatorDevice(tenant_id=operator.tenant_id, operator_id=operator.id, slot=slot)
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
