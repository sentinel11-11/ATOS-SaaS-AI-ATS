import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_mobile_callback import leg_event, start
from test_widget_protocol import browser_agent as browser_fixture
from test_widget_protocol import callback as callback_fixture
from test_widget_protocol import widget as widget_fixture

from ats.callback_affinity import AFFINITY_TTL, remember_outbound
from ats.config import settings
from ats.db import session
from ats.models import Attempt, CallbackAffinity, InboundCall, Operator, Outbox, now
from ats.return_call_agi import app
from ats.services import finish_attempt

callback, widget, browser_agent = callback_fixture, widget_fixture, browser_fixture
PHONE = "+79990000101"
WIDGET = "/api/v1/amo/widget/v2"


@pytest.fixture
def agi(factory, callback, monkeypatch):
    monkeypatch.setenv("ATS_INBOUND_ENABLED", "true")
    settings.cache_clear()

    def sessions():
        with factory() as db:
            yield db

    app.dependency_overrides[session] = sessions
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(session)


def lookup(agi, external_id="fixture.1", phone="8 (999) 000-01-01"):
    response = agi.post("/resolve", json={
        "external_id": external_id, "phone": phone, "destination": "79990000101",
    })
    assert response.status_code == 200, response.text
    return response.json()


def seed(db, callback, flow, dialed_at):
    db.add(CallbackAffinity(
        tenant_id=callback["tenant_id"], phone=PHONE, last_operator_id=callback["operator_id"],
        call_flow=flow, dialed_at=dialed_at, expires_at=dialed_at + AFFINITY_TTL,
    ))


def test_missed_outbound_stays_with_same_mobile_operator(factory, callback, agi):
    worker, attempt_id = start(factory, callback)
    leg_event(worker, attempt_id, "operator", "created")
    with factory() as db:
        assert db.get(CallbackAffinity, (callback["tenant_id"], PHONE)) is None
    leg_event(worker, attempt_id, "customer", "created")
    with factory.begin() as db:
        attempt = db.get(Attempt, UUID(attempt_id))
        affinity = db.get(CallbackAffinity, (callback["tenant_id"], PHONE))
        dialed_at = affinity.dialed_at
        remember_outbound(db, attempt, dialed_at - timedelta(seconds=30))
        finish_attempt(db, attempt, "failed", "customer_no_answer")
        assert affinity.dialed_at == dialed_at

    with ThreadPoolExecutor(max_workers=2) as callers:
        offers = list(callers.map(lambda ident: lookup(agi, ident), ["fixture.1", "fixture.2"]))
    offers = [offer for offer in offers if offer]
    assert len(offers) == 1
    offer = offers[0]
    assert offer["dial"] == "PJSIP/89990001000@mcm-cli-9990000101"
    assert agi.post(f"/answer/{offer['id']}").json() == {"answered": True}
    for _ in range(2):
        assert agi.post(f"/release/{offer['id']}?hangup=true").status_code == 200
    with factory() as db:
        assert db.get(Operator, callback["operator_id"]).status == "free"
        assert db.get(InboundCall, UUID(offer["id"])).status == "completed"
        assert db.scalar(select(Outbox).where(Outbox.dedupe_key == f"crm-inbound:{offer['id']}"))
        assert db.get(CallbackAffinity, (callback["tenant_id"], PHONE)).dialed_at == dialed_at


def test_expired_busy_and_unanswered_offers_leave_normal_ingress_in_charge(factory, callback, agi):
    with factory.begin() as db:
        seed(db, callback, "standard", now() - timedelta(days=8))
    assert lookup(agi) == {}
    with factory.begin() as db:
        db.get(CallbackAffinity, (callback["tenant_id"], PHONE)).expires_at = now() + timedelta(days=1)
        operator = db.get(Operator, callback["operator_id"])
        operator.connection_method, operator.status = "sip", "break"
    assert lookup(agi) == {}
    with factory.begin() as db:
        db.get(Operator, callback["operator_id"]).status = "free"
    offer = lookup(agi)
    assert offer["dial"] == "PJSIP/704"
    assert agi.post(f"/release/{offer['id']}").status_code == 200
    assert lookup(agi) == {}
    with factory() as db:
        assert db.get(InboundCall, UUID(offer["id"])).status == "ringing"
        assert db.get(Operator, callback["operator_id"]).status == "free"


def test_migration_restores_affinity_from_an_existing_customer_leg(factory, callback):
    worker, attempt_id = start(factory, callback)
    leg_event(worker, attempt_id, "customer", "created")
    with factory.begin() as db:
        finish_attempt(db, db.get(Attempt, UUID(attempt_id)), "failed", "customer_no_answer")
    try:
        subprocess.run([".venv/bin/alembic", "downgrade", "0016"], check=True, capture_output=True)
    finally:
        subprocess.run([".venv/bin/alembic", "upgrade", "head"], check=True, capture_output=True)
    with factory() as db:
        affinity = db.get(CallbackAffinity, (callback["tenant_id"], PHONE))
        assert affinity.last_operator_id == callback["operator_id"]
        assert affinity.expires_at - affinity.dialed_at == AFFINITY_TTL


def test_browser_return_call_uses_existing_signed_widget_protocol(factory, widget, browser_agent, agi):
    with factory.begin() as db:
        seed(db, widget, "browser_callback", now())
    offer = lookup(agi)
    ident = offer["id"]
    assert offer["dial"] == "PJSIP/ats-web-" + widget["operator_id"].hex
    client, headers = widget["client"], widget["headers"]
    invite = {"session_id": browser_agent, "token": offer["token"]}
    assert client.post(WIDGET + "/agent/invite", json={**invite, "token": "0" * 48}, headers=headers).status_code == 403
    assert client.post(WIDGET + "/agent/invite", json={**invite, "session_id": str(uuid4())}, headers=headers).status_code == 409
    incoming = client.post(WIDGET + "/agent/invite", json=invite, headers=headers).json()
    assert incoming["call_id"] == ident and incoming["state"] == "ringing_operator"
    assert incoming["context"]["phone"] == PHONE
    assert agi.post(f"/answer/{ident}").json() == {"answered": True}
    assert client.post(f"{WIDGET}/calls/{ident}/ready", json=invite, headers=headers).status_code == 200
    assert client.get(f"{WIDGET}/calls/{ident}", headers=headers).json()["state"] == "connected"
    assert client.post(f"{WIDGET}/calls/{ident}/hangup", headers=headers).status_code == 202
    assert agi.post(f"/release/{ident}?hangup=true").status_code == 200
    assert client.get(f"{WIDGET}/calls/{ident}", headers=headers).json()["state"] == "ended"
