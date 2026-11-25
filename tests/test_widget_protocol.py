import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select, text
from test_mobile_callback import callback as mobile_callback_fixture

from ats.adapters import AmoCRM
from ats.config import settings
from ats.connections import save_provider_config
from ats.models import (
    Attempt,
    Audit,
    BlacklistEntry,
    CallLeg,
    Campaign,
    CampaignItem,
    Contact,
    Operator,
    OperatorDevice,
    Outbox,
    QueueEntry,
    User,
    WidgetCall,
    now,
)
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

    raw_contact = {"id": 123, "responsible_user_id": 7654321, "name": "Client", "custom_fields_values": [
        {"field_code": "PHONE", "values": [{"value": "+79990000101"}]},
    ]}
    raw_card = {"responsible_user_id": 7654321, "_embedded": {"contacts": [{"id": 123}]}}

    class CRM(AmoCRM):
        def __init__(self, config):
            pass

        def read(self, path, params=None):
            if path == "/api/v4/contacts/123":
                return raw_contact
            return {"id": int(path.rsplit("/", 1)[-1]), **raw_card}

        def close(self):
            pass

    monkeypatch.setattr("ats.api_widget.AmoCRM", CRM)
    monkeypatch.setattr("ats.workers.AmoCRM", CRM)
    claims = {"exp": int((now() + timedelta(minutes=10)).timestamp()), "aud": "http://testserver",
              "client_uuid": CLIENT_ID, "account_id": 12345, "user_id": 7654321,
              "subdomain": "omkr", "jti": str(uuid4()), "iss": "amocrm.ru", "is_admin": False}
    headers = {"Origin": "https://omkr.amocrm.ru", "X-Auth-Token": jwt.encode(claims, SECRET, algorithm="HS256")}
    return {**callback, "client": client, "headers": headers, "claims": claims,
            "raw_contact": raw_contact, "raw_card": raw_card}


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


@pytest.mark.parametrize("legacy", [True, False])
def test_widget_migration_backfills_old_actor_and_preserves_new_separate_actor(factory, widget, legacy):
    created = create(widget)
    call_id = UUID(created["call_id"])
    with factory.begin() as db:
        call = db.get(WidgetCall, call_id)
        expected_user = call.initiator_user_id
        if legacy:
            call.crm_context = {"amo_user_id": 7654321, "contact_id": 123}
            expected_session = None
        else:
            user = User(tenant_id=widget["tenant_id"], login="other-initiator", role="operator", password_hash="fixture")
            db.add(user)
            db.flush()
            expected_user, expected_session = user.id, uuid4()
            call.initiator_user_id, call.browser_session_id = expected_user, expected_session
            call.crm_context = {**call.crm_context, "initiator_ats_user_id": str(user.id),
                                "browser_session_id": str(expected_session)}
    env = {**os.environ, "ATS_DATABASE_URL": os.environ["ATS_TEST_DATABASE_URL"]}
    try:
        subprocess.run([".venv/bin/alembic", "downgrade", "0013"], env=env, check=True, capture_output=True)
        with factory() as db:
            assert db.scalar(text("SELECT count(*) FROM widget_calls WHERE id=:id"), {"id": call_id}) == 1
    finally:
        subprocess.run([".venv/bin/alembic", "upgrade", "head"], env=env, check=True, capture_output=True)
    with factory() as db:
        call = db.get(WidgetCall, call_id)
        assert call.initiator_user_id == expected_user and call.browser_session_id == expected_session
        assert call.operator_id == widget["operator_id"]


@pytest.mark.parametrize("phase", ["before_start", "before_dispatch"])
def test_changed_responsible_prevents_network_call_and_releases_reserved_operator(factory, widget, phase):
    call_id = create(widget)["call_id"]
    worker = Worker(factory)
    if phase == "before_dispatch":
        assert widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"]).status_code == 202
        worker.schedule()
    widget["raw_card"]["responsible_user_id"] = 9999999
    if phase == "before_start":
        result = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
        assert result.status_code == 409
    else:
        worker.telephony()
    assert not widget["provider"].originates
    with factory() as db:
        assert db.get(Operator, widget["operator_id"]).status == "free"
        assert not db.scalar(select(QueueEntry).where(QueueEntry.status == "accepted"))


@pytest.fixture
def browser_agent(factory, widget, monkeypatch):
    from ats.webrtc import save_device

    for key, value in {"ATS_WEBRTC_ENABLED": "true", "ATS_WEBRTC_WSS_URL": "wss://ats.example.invalid/asterisk/ws",
                       "ATS_WEBRTC_SIP_DOMAIN": "ats.example.invalid"}.items():
        monkeypatch.setenv(key, value)
    settings.cache_clear()
    with factory.begin() as db:
        operator = db.get(Operator, widget["operator_id"])
        endpoint = "ats-web-" + operator.id.hex
        save_device(db, operator, endpoint, endpoint, "x" * 48)
    widget["provider"].endpoints.add(endpoint)
    ident = str(uuid4())
    for path, extra in [("lease", {}), ("heartbeat", {"registered": True})]:
        response = widget["client"].post("/api/v1/amo/widget/v2/agent/" + path,
            json={"session_id": ident, **extra}, headers=widget["headers"])
        assert response.status_code == 200, response.text
    return ident


def test_agent_lease_serializes_tabs_and_cannot_be_stolen_while_busy(factory, widget, browser_agent):
    other = str(uuid4())
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/lease", json={"session_id": other}, headers=widget["headers"])
    assert response.status_code == 409 and response.json()["error"] == "agent_in_another_tab"
    response = widget["client"].get("/api/v1/amo/widget/v2/webrtc/config", params={"session_id": other}, headers=widget["headers"])
    assert response.status_code == 409 and "sip_password" not in response.json()
    call_id = create(widget, payload(mode="browser", protocol_version=3))["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    Worker(factory).schedule()
    with factory.begin() as db:
        db.scalar(select(OperatorDevice)).agent_expires_at = now() - timedelta(seconds=1)
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/lease", json={"session_id": other}, headers=widget["headers"])
    assert response.status_code == 409
    with factory() as db:
        assert db.scalar(select(WidgetCall)).browser_session_id == UUID(browser_agent)
        assert db.get(Operator, widget["operator_id"]).status == "busy"


def test_browser_disconnected_after_allocation_never_dials_customer(factory, widget, browser_agent):
    call_id = create(widget, payload(mode="browser", protocol_version=3))["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    worker = Worker(factory)
    worker.schedule()
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/heartbeat",
        json={"session_id": browser_agent, "registered": False}, headers=widget["headers"])
    assert response.status_code == 200
    worker.telephony()
    assert not widget["provider"].originates
    with factory() as db:
        assert db.scalar(select(Attempt)).reason == "responsible_browser_offline"
        assert db.get(Operator, widget["operator_id"]).status == "free"


def test_browser_heartbeat_preserves_break_and_cannot_release_busy_operator(factory, widget, browser_agent):
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/availability",
        json={"status": "break"}, headers=widget["headers"])
    assert response.status_code == 200
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/heartbeat",
        json={"session_id": browser_agent, "registered": True}, headers=widget["headers"])
    assert response.json()["status"] == "break"
    widget["client"].post("/api/v1/amo/widget/v2/agent/availability", json={"status": "free"}, headers=widget["headers"])
    call_id = create(widget, payload(mode="browser", protocol_version=3))["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    Worker(factory).schedule()
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/availability", json={"status": "free"}, headers=widget["headers"])
    assert response.status_code == 409
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/heartbeat",
        json={"session_id": browser_agent, "registered": True}, headers=widget["headers"])
    assert response.json()["status"] == "busy"


def test_agent_lease_takeover_expires_queued_calls_and_blocks_old_page(factory, widget, browser_agent):
    call_id = create(widget, payload(mode="browser", protocol_version=3))["call_id"]
    with factory.begin() as db:
        db.scalar(select(OperatorDevice)).agent_expires_at = now() - timedelta(seconds=1)
    other = str(uuid4())
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/lease", json={"session_id": other}, headers=widget["headers"])
    assert response.status_code == 200
    state = widget["client"].get(f"/api/v1/amo/widget/v2/calls/{call_id}", headers=widget["headers"]).json()
    assert state["state"] == "failed" and state["reason"] == "responsible_browser_offline"
    response = widget["client"].post("/api/v1/amo/widget/v2/agent/heartbeat", json={"session_id": browser_agent}, headers=widget["headers"])
    assert response.status_code == 409


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


def test_widget_binds_an_admitted_manual_contact_after_crm_verification(factory, widget):
    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).crm_contact_id = None
    data = payload()
    first = create(widget, data)
    assert create(widget, data)["call_id"] == first["call_id"]
    with factory() as db:
        assert db.get(Contact, widget["contact_id"]).crm_contact_id == 123
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "contact.crm_linked")) == 1
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0


@pytest.mark.parametrize("entity_type,entity_id", [("leads", 101), ("companies", 202), ("contacts", 123)])
def test_widget_imports_a_missing_contact_and_retries_without_duplicates(factory, widget, entity_type, entity_id):
    with factory.begin() as db:
        previous = db.get(Contact, widget["contact_id"])
        previous.phone, previous.crm_contact_id = "+79990000999", 456
    widget["raw_contact"]["custom_fields_values"].append({
        "field_code": "EMAIL", "values": [{"value": "client@example.invalid"}],
    })
    widget["raw_contact"]["_embedded"] = {"tags": [{"name": "CRM group"}]}
    command = payload(entity_type=entity_type, entity_id=entity_id)
    first = create(widget, command)
    assert create(widget, command)["call_id"] == first["call_id"]
    Worker(factory).schedule()
    with factory() as db:
        contact = db.scalar(select(Contact).where(Contact.phone == command["phone"]))
        assert contact.tenant_id == widget["tenant_id"] and contact.crm_contact_id == 123
        assert contact.name == "Client" and contact.email == "client@example.invalid"
        assert contact.source == "amoCRM" and contact.group == "CRM group"
        assert contact.tags == ["CRM group"]
        assert contact.consent and contact.consent_source == "amoCRM" and not contact.blocked
        call = db.scalar(select(WidgetCall))
        item = db.scalar(select(CampaignItem).where(CampaignItem.campaign_id == call.campaign_id))
        assert item.contact_id == contact.id and call.crm_context["contact_id"] == 123
        assert db.scalar(select(func.count()).select_from(Contact)) == 2
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
        assert db.scalar(select(func.count()).select_from(Audit).where(
            Audit.action == "contact.created", Audit.entity_id == contact.id,
        )) == 1


def test_widget_imports_the_selected_crm_phone_instead_of_the_first_phone(factory, widget):
    phone = "+79990000102"
    widget["raw_contact"]["custom_fields_values"][0]["values"].append({"value": phone})
    create(widget, payload(phone=phone))
    with factory() as db:
        assert db.scalar(select(Contact).where(Contact.phone == phone)).crm_contact_id == 123
        assert db.get(Contact, widget["contact_id"]).phone == "+79990000101"


def test_concurrent_widget_retries_import_one_contact_and_prepare_one_call(factory, widget, monkeypatch):
    from ats.api_widget import AmoCRM as WidgetCRM

    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).phone = "+79990000999"
    command = payload()
    reads = Barrier(2)
    original_read = WidgetCRM.read

    def simultaneous_read(self, path, params=None):
        if path == "/api/v4/contacts/123":
            reads.wait(timeout=5)
        return original_read(self, path, params)

    monkeypatch.setattr(WidgetCRM, "read", simultaneous_read)

    def prepare(_):
        return widget["client"].post("/api/v1/amo/widget/v2/calls", json=command, headers=widget["headers"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(prepare, range(2)))
    assert all(response.status_code == 202 for response in responses)
    assert responses[0].json()["call_id"] == responses[1].json()["call_id"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Contact)) == 2
        assert db.scalar(select(func.count()).select_from(WidgetCall)) == 1


@pytest.mark.parametrize("restriction", ["archived", "blocked", "no_consent", "blacklist"])
def test_widget_active_crm_contact_has_consent_after_a_phone_change(factory, widget, restriction):
    with factory.begin() as db:
        contact = db.get(Contact, widget["contact_id"])
        contact.phone = "+79990000999"
        if restriction == "no_consent":
            contact.consent = False
        elif restriction == "blacklist":
            db.add(BlacklistEntry(tenant_id=contact.tenant_id, phone=payload()["phone"], reason="Test opt-out"))
        else:
            setattr(contact, restriction, True)
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(), headers=widget["headers"])
    if restriction == "blacklist":
        assert response.status_code == 409 and response.json()["error"] == "contact_blacklisted"
    else:
        assert response.status_code == 202, response.text
        with factory() as db:
            current = db.scalar(select(Contact).where(Contact.phone == payload()["phone"]))
            assert current.consent and current.consent_source == "amoCRM"
            assert not current.blocked and not current.archived
            assert db.get(Contact, widget["contact_id"]).phone == "+79990000999"


def test_widget_auto_import_requires_the_contact_to_belong_to_the_card(factory, widget):
    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).phone = "+79990000999"
    widget["raw_card"]["_embedded"]["contacts"] = [{"id": 456}]
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(), headers=widget["headers"])
    assert response.status_code == 403 and response.json()["error"] == "contact_not_in_card"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Contact)) == 1


def test_widget_auto_import_keeps_another_tenants_contact_unchanged(factory, widget, tenants):
    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).phone = "+79990000999"
        foreign = Contact(tenant_id=tenants[1], name="Foreign fixture", phone=payload()["phone"],
                          crm_contact_id=456, blocked=True, consent=False)
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    create(widget)
    with factory() as db:
        own = db.scalar(select(Contact).where(
            Contact.tenant_id == widget["tenant_id"], Contact.phone == payload()["phone"],
        ))
        assert own.crm_contact_id == 123 and own.id != foreign_id
        assert db.get(Contact, foreign_id).blocked
        assert db.get(Contact, foreign_id).crm_contact_id == 456


@pytest.mark.parametrize("restriction", ["archived", "blocked", "no_consent", "blacklist"])
def test_verified_crm_call_restores_local_contact_and_consent_except_blacklist(factory, widget, restriction):
    with factory.begin() as db:
        contact = db.get(Contact, widget["contact_id"])
        contact.crm_contact_id = None
        if restriction == "no_consent":
            contact.consent = False
        elif restriction == "blacklist":
            db.add(BlacklistEntry(tenant_id=contact.tenant_id, phone=contact.phone, reason="Test opt-out"))
        else:
            setattr(contact, restriction, True)
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(), headers=widget["headers"])
    if restriction == "blacklist":
        assert response.status_code == 409 and response.json()["error"] == "contact_blacklisted"
    else:
        assert response.status_code == 202, response.text
        with factory() as db:
            contact = db.get(Contact, widget["contact_id"])
            assert contact.consent and contact.consent_source
            assert not contact.blocked and not contact.archived
            assert contact.crm_contact_id == 123
            assert db.scalar(select(func.count()).select_from(WidgetCall)) == 1


def test_widget_reuses_an_admitted_phone_and_delivers_to_the_verified_crm_contact(factory, widget):
    from ats.services import finish_attempt

    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).crm_contact_id = 456
    call_id = create(widget)["call_id"]
    response = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    assert response.status_code == 202
    deliveries = []

    class CRMDelivery:
        def send(self, attempt_id, contact_id, status):
            deliveries.append((contact_id, status))

    worker = Worker(factory, crm=CRMDelivery())
    worker.schedule()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        assert attempt.contact_name == widget["raw_contact"]["name"]
        finish_attempt(db, attempt, "failed", "fixture_finished")
    assert worker.crm_job()
    assert deliveries == [(123, "failed")]
    with factory() as db:
        assert db.get(Contact, widget["contact_id"]).crm_contact_id == 456
        assert db.scalar(select(WidgetCall)).crm_context["contact_id"] == 123
        assert db.scalar(select(func.count()).select_from(Contact)) == 1


@pytest.mark.parametrize("entity_type,entity_id", [("leads", 101), ("contacts", 123), ("companies", 202)])
def test_widget_result_task_uses_source_entity_and_actual_participant(factory, widget, entity_type, entity_id):
    from ats.models import WorkspaceSettings
    from ats.services import finish_attempt

    with factory.begin() as db:
        row = db.scalar(select(WorkspaceSettings).where(WorkspaceSettings.tenant_id == widget["tenant_id"]))
        if row:
            row.values = {**row.values, "amo_auto_create_tasks": True}
        else:
            db.add(WorkspaceSettings(tenant_id=widget["tenant_id"], values={"amo_auto_create_tasks": True}))
    call_id = create(widget, payload(entity_type=entity_type, entity_id=entity_id))["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    worker = Worker(factory)
    worker.schedule()
    with factory.begin() as db:
        finish_attempt(db, db.scalar(select(Attempt)), "failed", "fixture_finished")
    tasks, registrations = [], []

    class Delivery:
        def send(self, *args):
            pass

        def register_call(self, *args, **kwargs):
            registrations.append(args)

        def create_task(self, *args, **kwargs):
            tasks.append((args, kwargs))

    worker.crm = Delivery()
    assert worker.crm_job()
    assert registrations[0][5] == 7654321
    assert tasks[0][0][1] == entity_id and tasks[0][0][3] == 7654321
    assert tasks[0][1] == {"entity_type": entity_type}


def test_widget_refuses_a_phone_changed_while_queued_before_reserving_resources(factory, widget):
    call_id = create(widget)["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).phone = "+79990000999"
    Worker(factory).schedule()
    state = widget["client"].get(f"/api/v1/amo/widget/v2/calls/{call_id}", headers=widget["headers"])
    assert state.status_code == 200 and state.json()["state"] == "failed"
    assert state.json()["reason"] == "contact_phone_mismatch"
    repeated = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    assert repeated.status_code == 202 and repeated.json()["state"] == "failed"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
        assert db.get(Operator, widget["operator_id"]).status == "free"
    assert not widget["provider"].originates


def test_crm_deletion_blocks_a_phone_used_via_a_different_local_crm_mapping(factory, widget):
    from ats.workspace_service import block_crm_contacts

    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).crm_contact_id = 456
    create(widget)
    with factory.begin() as db:
        affected = block_crm_contacts(db, widget["tenant_id"], 123)
        assert [row.id for row in affected] == [widget["contact_id"]]
    with factory() as db:
        contact = db.get(Contact, widget["contact_id"])
        assert contact.blocked and not contact.consent
        assert contact.crm_contact_id == 456


def test_verified_crm_call_restores_an_archived_phone_without_rewriting_its_old_crm_link(factory, widget):
    with factory.begin() as db:
        contact = db.get(Contact, widget["contact_id"])
        contact.crm_contact_id, contact.archived, contact.blocked = 456, True, True
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(), headers=widget["headers"])
    assert response.status_code == 202, response.text
    with factory() as db:
        assert db.get(Contact, widget["contact_id"]).crm_contact_id == 456
        assert db.scalar(select(func.count()).select_from(WidgetCall)) == 1
        contact = db.get(Contact, widget["contact_id"])
        assert contact.consent and not contact.blocked and not contact.archived


def test_widget_cannot_bind_a_contact_outside_the_requested_card(factory, widget):
    with factory.begin() as db:
        db.get(Contact, widget["contact_id"]).crm_contact_id = None
    response = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(
        entity_type="contacts", entity_id=456,
    ), headers=widget["headers"])
    assert response.status_code == 403 and response.json()["error"] == "contact_not_in_card"
    with factory() as db:
        assert db.get(Contact, widget["contact_id"]).crm_contact_id is None


def test_widget_follows_changed_crm_phone_without_rewriting_old_contacts(factory, widget):
    phones = ["+79990000101", "+79990000102", "+79990000103"]
    for phone in phones:
        widget["raw_contact"]["custom_fields_values"][0]["values"][0]["value"] = phone
        call_id = create(widget, payload(phone=phone))["call_id"]
        result = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/hangup", headers=widget["headers"])
        assert result.status_code == 202
    with factory() as db:
        contacts = db.scalars(select(Contact).order_by(Contact.phone)).all()
        assert [c.phone for c in contacts] == phones
        assert all(c.crm_contact_id == 123 for c in contacts)
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0


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


@pytest.mark.parametrize("mode", ["forward", "browser"])
def test_card_responsible_receives_both_modes_and_owns_resources_and_result(
    factory, widget, monkeypatch, mode
):
    from ats.services import finish_attempt
    from ats.webrtc import save_device

    for name, value in {
        "ATS_WEBRTC_ENABLED": "true", "ATS_WEBRTC_WSS_URL": "wss://ats.example.invalid/asterisk/ws",
        "ATS_WEBRTC_SIP_DOMAIN": "ats.example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    with factory.begin() as db:
        own = db.get(Operator, widget["operator_id"])
        own.name = "Ilya"
        user = User(tenant_id=widget["tenant_id"], login="alim", role="operator", password_hash="fixture")
        db.add(user)
        db.flush()
        other = Operator(tenant_id=widget["tenant_id"], user_id=user.id, name="Alim", extension="705",
                         status="free", connection_method="mobile", mobile_phone="+79990001001")
        db.add(other)
        db.flush()
        other_id = other.id
        own_endpoint, other_endpoint = "ats-web-" + own.id.hex, "ats-web-" + other.id.hex
        save_device(db, own, own_endpoint, own_endpoint, "x" * 48)
        save_device(db, other, other_endpoint, other_endpoint, "y" * 48)
        save_provider_config(db, widget["tenant_id"], {"amocrm": {
            "operator_user_map": {"704": 7654321, "705": 7654322},
        }})
    widget["provider"].endpoints.update([own_endpoint, other_endpoint])
    widget["raw_card"]["responsible_user_id"] = 7654322
    widget["raw_contact"]["responsible_user_id"] = 7654322
    browser = widget["client"].get("/api/v1/amo/widget/v2/webrtc/config", headers=widget["headers"])
    assert browser.status_code == 200 and browser.json()["auth_user"] == own_endpoint
    assert browser.json()["sip_password"] == "x" * 48
    other_headers = {**widget["headers"], "X-Auth-Token": jwt.encode(
        {**widget["claims"], "user_id": 7654322}, SECRET, algorithm="HS256",
    )}
    session_id = str(uuid4())
    if mode == "browser":
        for path, extra in [("lease", {}), ("heartbeat", {"registered": True})]:
            response = widget["client"].post("/api/v1/amo/widget/v2/agent/" + path,
                json={"session_id": session_id, **extra}, headers=other_headers)
            assert response.status_code == 200, response.text
    context = widget["client"].post("/api/v1/amo/widget/v2/context",
        json={"entity_type": "leads", "entity_id": 101}, headers=widget["headers"]).json()
    assert context["responsible"]["operator_name"] == "Alim"
    assert context["responsible"]["forward_number_masked"] == "+7***1001"
    legacy = widget["client"].post("/api/v1/amo/widget/v2/calls", json=payload(mode=mode), headers=widget["headers"])
    assert legacy.status_code == 409 and legacy.json()["error"] == "widget_upgrade_required"
    created = create(widget, payload(mode=mode, protocol_version=3))
    assert not created["is_participant"] and "operator_call_token" not in created
    call_id = created["call_id"]
    denied = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/ready", headers=widget["headers"])
    assert denied.status_code == 404
    assert widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=other_headers).status_code == 404
    result = widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/start", headers=widget["headers"])
    assert result.status_code == 202
    worker = Worker(factory)
    worker.schedule()
    with factory() as db:
        attempt = db.scalar(select(Attempt))
        call = db.scalar(select(WidgetCall))
        assert call.operator_id == other_id
        assert call.initiator_user_id == db.get(Operator, widget["operator_id"]).user_id
        assert db.scalar(select(QueueEntry)).operator_id == other_id
        assert db.get(Operator, widget["operator_id"]).status == "free"
        assert db.get(Operator, other_id).status == "busy"
        assert attempt.channel_units == (2 if mode == "forward" else 1)
        token = call.operator_token
    if mode == "browser":
        invite = {"session_id": session_id, "token": token}
        assert widget["client"].post("/api/v1/amo/widget/v2/agent/invite", json=invite, headers=widget["headers"]).status_code == 409
        assert widget["client"].post("/api/v1/amo/widget/v2/agent/invite", json=invite, headers=other_headers).status_code == 200
        bad = {**invite, "token": "f" * 48}
        assert widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/ready", json=bad, headers=other_headers).status_code == 403
        assert widget["client"].post(f"/api/v1/amo/widget/v2/calls/{call_id}/ready", json=invite, headers=other_headers).status_code == 200
    assert worker.telephony()
    frame = widget["provider"].originates[-1]
    if mode == "forward":
        assert frame["Channel"] == ["Local/+79990001001@ats-beta-callback-operator/n"]
    else:
        assert frame["Channel"] == [f"Local/{other_id}@ats-beta-callback-browser/n"]
        assert f"__ATS_OPERATOR_ENDPOINT={other_endpoint}" in frame["Variable"]
        assert f"__ATS_OPERATOR_ENDPOINT={own_endpoint}" not in frame["Variable"]
    with factory.begin() as db:
        finish_attempt(db, db.scalar(select(Attempt)), "failed", "fixture_finished")
    with factory() as db:
        delivery = db.scalar(select(Outbox).where(Outbox.kind == "crm"))
        assert delivery.payload["amo_responsible_user_id"] == 7654322
        assert db.get(Operator, widget["operator_id"]).status == "free"
        assert db.get(Operator, other_id).status == "free"


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


def test_unset_account_id_does_not_authorize_widget(factory, widget):
    with factory.begin() as db:
        save_provider_config(db, widget["tenant_id"], {"amocrm": {"account_id": ""}})
    response = widget["client"].get("/api/v1/amo/widget/v2/me", headers=widget["headers"])
    assert response.status_code == 503
    assert response.json()["error"] == "widget_auth_not_configured"
