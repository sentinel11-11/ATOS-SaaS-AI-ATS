from datetime import timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_mobile_callback import callback as mobile_callback_fixture

from ats.config import settings
from ats.connections import save_provider_config
from ats.models import Attempt, CallLeg, Campaign, Operator, Outbox, User, WidgetCall, now
from ats.workers import Worker

CLIENT_ID = "b0b44684-39c0-4631-9a45-ab0625b136be"
SECRET = "fixture-widget-secret-only-32-characters"
callback = mobile_callback_fixture


@pytest.fixture
def widget(factory, callback, client, monkeypatch):
    for name, value in {
        "ATS_AMO_TENANT_ID": str(callback["tenant_id"]), "ATS_AMO_URL": "https://omkr.amocrm.ru",
        "ATS_CRM_ENABLED": "true", "ATS_AMO_ACCESS_TOKEN": "fixture-read-token",
        "ATS_CONNECTION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    with factory.begin() as db:
        db.get(Campaign, callback["campaign_id"]).status = "paused"
        save_provider_config(db, callback["tenant_id"], {"amocrm": {
            "subdomain": "omkr", "client_id": CLIENT_ID, "account_id": 12345,
            "client_secret": SECRET, "operator_user_map": {"704": 7654321},
        }})

    class CRM:
        def __init__(self, config):
            pass

        def read(self, path, params=None):
            if path == "/api/v4/contacts/123":
                return {"id": 123, "name": "Client", "custom_fields_values": [
                    {"field_code": "PHONE", "values": [{"value": "+79990000101"}]},
                ]}
            return {"_embedded": {"contacts": [{"id": 123}]}}

        def close(self):
            pass

    monkeypatch.setattr("ats.api_widget.AmoCRM", CRM)
    claims = {"exp": int((now() + timedelta(minutes=10)).timestamp()), "aud": "http://testserver",
              "client_uuid": CLIENT_ID, "account_id": 12345, "user_id": 7654321,
              "subdomain": "omkr", "jti": str(uuid4()), "iss": "amocrm.ru", "is_admin": False}
    headers = {"Origin": "https://omkr.amocrm.ru", "X-Auth-Token": jwt.encode(claims, SECRET, algorithm="HS256")}
    return {**callback, "client": client, "headers": headers, "claims": claims}


def payload(**updates):
    return {"phone": "+79990000101", "crm_contact_id": 123, "mode": "forward",
            "client_request_id": str(uuid4()), "amo_account": "omkr",
            "entity_type": "leads", "entity_id": 101, **updates}


def create(widget, data=None):
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=data or payload(), headers=widget["headers"])
    assert response.status_code == 202, response.text
    return response.json()


def test_moderated_widget_signed_identity_and_cors(widget):
    response = widget["client"].get("/api/v1/amo/widget/v2/me", headers=widget["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["operator_id"] == str(widget["operator_id"])
    assert response.json()["forward_number_masked"] == "+7***1000"
    preflight = widget["client"].options("/api/v1/amo/widget/v2/calls", headers={
        "Origin": "https://omkr.amocrm.ru", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Auth-Token,Content-Type",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://omkr.amocrm.ru"


@pytest.mark.parametrize("change,value", [
    ("aud", "https://foreign.invalid"), ("account_id", 12346), ("user_id", 1),
    ("client_uuid", str(uuid4())), ("subdomain", "foreign"), ("exp", 1),
])
def test_signed_widget_rejects_wrong_binding_and_expiry(widget, change, value):
    claims = {**widget["claims"], change: value}
    headers = {**widget["headers"], "X-Auth-Token": jwt.encode(claims, SECRET, algorithm="HS256")}
    response = widget["client"].get("/api/v1/amo/widget/v2/me", headers=headers)
    assert response.status_code in {401, 409}, response.text


def test_widget_rejects_unsigned_and_forged_identity(widget):
    for token in ("", jwt.encode(widget["claims"], "other-secret-fixture-only-32-char", algorithm="HS256")):
        response = widget["client"].get("/api/v1/amo/widget/v2/me", headers={**widget["headers"], "X-Auth-Token": token})
        assert response.status_code == 401
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(operator_id=str(uuid4())), headers=widget["headers"])
    assert response.status_code == 422


def test_double_click_creates_one_pending_call_and_no_network_before_ack(factory, widget):
    data = payload()
    first, second = create(widget, data), create(widget, data)
    assert first["call_id"] == second["call_id"]
    worker = Worker(factory)
    worker.schedule()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(WidgetCall)) == 1
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
    call_id = first["call_id"]
    assert widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"]).status_code == 202
    worker.schedule()
    worker.telephony()
    assert len(widget["provider"].originates) == 1
    response = widget["client"].get(f"/api/v1/amo/widget/v2/calls/{call_id}", headers=widget["headers"])
    assert response.json()["state"] == "ringing_operator"


def test_second_request_same_operator_is_blocked_and_changed_retry_is_conflict(widget):
    data = payload()
    create(widget, data)
    result = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(), headers=widget["headers"])
    assert result.status_code == 409 and result.json()["error"] == "operator_unavailable"
    result = widget["client"].post("/api/v1/amo/widget/v2/calls", json={**data, "mode": "browser"}, headers=widget["headers"])
    assert result.status_code == 409 and result.json()["error"] == "idempotency_key_reused"


def test_widget_queued_hangup_prevents_allocation(factory, widget):
    call_id = create(widget)["call_id"]
    result = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/hangup", headers=widget["headers"])
    assert result.status_code == 202
    Worker(factory).schedule()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
        assert db.get(Operator, widget["operator_id"]).status == "free"


def test_widget_checks_card_phone_and_does_not_import_consent(widget):
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(phone="+79990001001"), headers=widget["headers"])
    assert response.status_code == 409 and response.json()["error"] == "contact_phone_mismatch"


def test_widget_foreign_operator_cannot_read_or_hangup(factory, widget):
    call_id = create(widget)["call_id"]
    with factory.begin() as db:
        save_provider_config(db, widget["tenant_id"], {"amocrm": {"operator_user_map": {"999": 7654321}}})
    response = widget["client"].get(f"/api/v1/amo/widget/v2/calls/{call_id}", headers=widget["headers"])
    assert response.status_code == 409


def test_other_authenticated_operator_cannot_control_call(factory, widget):
    call_id = create(widget)["call_id"]
    with factory.begin() as db:
        user = User(tenant_id=widget["tenant_id"], login="another-manager", role="operator", password_hash="fixture")
        db.add(user)
        db.flush()
        db.add(Operator(tenant_id=widget["tenant_id"], user_id=user.id, extension="705", status="free"))
        save_provider_config(db, widget["tenant_id"], {"amocrm": {"operator_user_map": {"704": 7654321, "705": 7654322}}})
    token = jwt.encode({**widget["claims"], "user_id": 7654322}, SECRET, algorithm="HS256")
    headers = {**widget["headers"], "X-Auth-Token": token}
    for method, suffix in [("get", ""), ("post", "/hangup"), ("post", "/start"), ("post", "/ready")]:
        response = getattr(widget["client"], method)(f"/api/v1/amo/widget/v2/calls/{call_id}{suffix}", headers=headers)
        assert response.status_code == 404


def test_browser_customer_gate_waits_for_real_operator_answer_and_media(factory, widget, monkeypatch):
    from ats.callbacks import authorize_customer
    from ats.connections import settings_for_tenant
    from ats.webrtc import save_device

    for key, value in {"ATS_WEBRTC_ENABLED": "true", "ATS_WEBRTC_WSS_URL": "wss://ats.example.invalid/asterisk/ws",
                       "ATS_WEBRTC_SIP_DOMAIN": "ats.example.invalid", "ATS_WEBRTC_TURN_URLS": '["turn:turn.example.invalid:3478?transport=tcp"]',
                       "ATS_WEBRTC_TURN_SECRET": "fixture-turn-secret"}.items():
        monkeypatch.setenv(key, value)
    settings.cache_clear()
    with factory.begin() as db:
        op = db.get(Operator, widget["operator_id"])
        endpoint = "ats-web-" + op.id.hex
        save_device(db, op, endpoint, endpoint, "x" * 48)
    config = widget["client"].get("/api/v1/amo/widget/v2/webrtc/config", headers=widget["headers"])
    assert config.status_code == 200, config.text
    assert config.headers["cache-control"] == "no-store"
    assert config.json()["sip_password"] == "x" * 48
    assert config.json()["ice_servers"][0]["credential"] != "fixture-turn-secret"
    call_id = create(widget, payload(mode="browser"))["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    Worker(factory).schedule()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        assert attempt.call_flow == "browser_callback" and attempt.channel_units == 1
        attempt.status = "accepted"
        attempt.manager_confirmed_at = now()
        cfg = settings_for_tenant(db, widget["tenant_id"])
        assert not authorize_customer(db, attempt, cfg)
    result = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/ready", headers=widget["headers"])
    assert result.status_code == 200
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        assert not authorize_customer(db, attempt, cfg)
        operator = db.scalar(select(CallLeg).where(CallLeg.role == "operator"))
        operator.answered_at = now()
        assert authorize_customer(db, attempt, cfg)
        attempt.status = "unknown"
        assert not authorize_customer(db, attempt, cfg)


def test_hangup_while_manager_ringing_targets_recorded_leg(factory, widget):
    call_id = create(widget)["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    Worker(factory).schedule()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        attempt.status = "dispatching"
        leg = db.scalar(select(CallLeg).where(CallLeg.role == "operator"))
        leg.channel = "PJSIP/mcm-cli-9990000101-fixture"
    result = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/hangup", headers=widget["headers"])
    assert result.status_code == 202
    with factory() as db:
        control = db.scalar(select(Outbox).where(Outbox.kind == "control"))
        assert control.payload["channel"] == "PJSIP/mcm-cli-9990000101-fixture"


@pytest.mark.parametrize("origin", ["https://foreign.amocrm.ru", "https://omkr.amocrm.ru.evil.invalid"])
def test_widget_rejects_wrong_origin(widget, origin):
    response = widget["client"].get("/api/v1/amo/widget/v2/me", headers={**widget["headers"], "Origin": origin})
    assert response.status_code == 403
