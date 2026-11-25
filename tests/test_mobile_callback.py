from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ats.config import settings
from ats.domain import Problem
from ats.models import (
    Attempt,
    CallerNumber,
    CallLeg,
    Campaign,
    Contact,
    Operator,
    Outbox,
    Template,
    Tenant,
    User,
    now,
)
from ats.services import schedule_one, validate_route
from ats.workers import Worker

MANAGER = "+79990001000"
ROOT = f"Local/{MANAGER}@ats-beta-callback-operator-00000001;1"
CHANNELS = {role: f"PJSIP/mcm-cli-9990000101-0000000{index}"
            for index, role in enumerate(["operator", "customer"], 1)}


@pytest.fixture
def callback(factory, prepared, tenants, monkeypatch, provider_config):
    for key, value in {
        "ATS_CALLBACK_ENABLED": "true", "ATS_AMI_CLI_ENDPOINT_PREFIX": "mcm-cli-",
        "ATS_AMI_RECORD_CALLS": "false",
    }.items():
        monkeypatch.setenv(key, value)
    settings.cache_clear()
    with factory.begin() as db:
        user = User(tenant_id=tenants[0], login="callback-manager", role="operator", password_hash="fixture-only")
        db.add(user)
        db.flush()
        operator = Operator(
            tenant_id=tenants[0], user_id=user.id, name="Test manager", extension="704",
            connection_method="mobile", mobile_phone=MANAGER, status="free",
        )
        db.add(operator)
        db.flush()
        db.add(CallerNumber(
            tenant_id=tenants[0], phone="9990000101", provider="multicom_ami", verified=True,
            enabled=True, cooldown_seconds=0,
        ))
        campaign = db.get(Campaign, UUID(prepared[0]))
        campaign.mode, campaign.schedule = "operator", {"operator_id": str(operator.id)}
        operator_id = operator.id
    return {"operator_id": operator_id, "tenant_id": tenants[0], "campaign_id": UUID(prepared[0]),
            "contact_id": UUID(prepared[1]), "provider": provider_config}


def start(factory, callback):
    worker = Worker(factory)
    worker.schedule()
    with factory() as db:
        attempt = db.scalar(select(Attempt))
        assert attempt and attempt.call_flow == "mobile_callback" and attempt.channel_units == 2
        attempt_id = str(attempt.id)
    assert worker.telephony()
    return worker, attempt_id


def leg_event(worker, attempt_id, role, state):
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCallback", "AttemptID": attempt_id,
        "Leg": role, "State": state, "Channel": CHANNELS[role],
        "Uniqueid": f"fixture.{role}", "Linkedid": "fixture.root",
    })


def confirmed(worker, attempt_id):
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCallback", "AttemptID": attempt_id,
        "Leg": "operator", "State": "confirmed", "Channel": ROOT,
    })


def native(worker, role, kind):
    worker.ami_event({
        "Event": kind, "Channel": CHANNELS[role], "Uniqueid": f"fixture.{role}",
        "Linkedid": "fixture.root", "BridgeUniqueid": f"bridge.{role}", "Cause": "16",
    })


def result(worker, attempt_id, state="completed", reason="", role="customer"):
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCallback", "AttemptID": attempt_id,
        "Leg": role, "State": state, "ReasonCode": reason, "Channel": ROOT,
        "DialStatus": "ANSWER" if state == "completed" else "NOANSWER",
    })


def test_callback_originates_manager_first_with_two_reserved_channels(factory, callback, logged):
    worker, attempt_id = start(factory, callback)
    frame = callback["provider"].originates[-1]
    assert frame["Channel"] == [f"Local/{MANAGER}@ats-beta-callback-operator/n"]
    assert frame["Context"] == ["ats-beta-callback-customer"]
    assert "__ATS_CUSTOMER_PHONE=+79990000101" in frame["Variable"]
    journal = logged.post("/api/v2/calls", json={}).json()
    assert journal["total"] == 1
    assert journal["calls"][0]["operator_name"] == "Test manager"
    leg_event(worker, attempt_id, "operator", "created")
    leg_event(worker, attempt_id, "operator", "answered")
    native(worker, "operator", "BridgeEnter")
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).answered_at is None
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm")) == 0
    confirmed(worker, attempt_id)
    assert worker.call_control()
    assert callback["provider"].setvars[-1]["Value"] == ["allow"]
    with factory() as db:
        assert db.scalar(select(CallerNumber)).last_used_at
        assert db.get(Operator, callback["operator_id"]).status == "busy"


def test_callback_finishes_only_after_bridge_and_both_leg_hangups(factory, callback, monkeypatch, logged):
    clock = [now()]
    monkeypatch.setattr("ats.callbacks.now", lambda: clock[0])
    monkeypatch.setattr("ats.services.now", lambda: clock[0])
    worker, attempt_id = start(factory, callback)
    leg_event(worker, attempt_id, "operator", "created")
    leg_event(worker, attempt_id, "operator", "answered")
    native(worker, "operator", "BridgeEnter")
    confirmed(worker, attempt_id)
    worker.call_control()
    clock[0] += timedelta(seconds=5)
    leg_event(worker, attempt_id, "customer", "created")
    leg_event(worker, attempt_id, "customer", "answered")
    native(worker, "customer", "BridgeEnter")
    with factory() as db:
        row = db.get(Attempt, UUID(attempt_id))
        assert row.status == "answered" and row.bridged_at == clock[0]
    clock[0] += timedelta(seconds=20)
    result(worker, attempt_id)
    native(worker, "customer", "BridgeLeave")
    native(worker, "customer", "Hangup")
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).status == "answered"
        assert db.get(Operator, callback["operator_id"]).status == "busy"
    clock[0] += timedelta(seconds=2)
    native(worker, "operator", "BridgeLeave")
    native(worker, "operator", "Hangup")
    result(worker, attempt_id)
    with factory() as db:
        row = db.get(Attempt, UUID(attempt_id))
        assert row.status == "completed" and row.reason == ""
        assert db.get(Operator, callback["operator_id"]).status == "free"
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm")) == 1
    timeline = logged.get(f"/api/v2/calls/{attempt_id}/timeline").json()
    assert timeline["call"]["duration_sec"] == 20
    assert {leg["role"] for leg in timeline["legs"]} == {"operator", "customer"}
    pool = logged.get("/api/v2/numbers").json()["numbers"][0]
    assert pool["daily_count"] == 2 and pool["dialed_total"] == 1


@pytest.mark.parametrize("change", ["consent", "pause", "phone", "operator", "number", "quota", "mapping"])
def test_customer_permission_rechecks_dispatch_gates(factory, callback, change, monkeypatch):
    worker, attempt_id = start(factory, callback)
    leg_event(worker, attempt_id, "operator", "created")
    leg_event(worker, attempt_id, "operator", "answered")
    confirmed(worker, attempt_id)
    with factory.begin() as db:
        if change == "consent":
            db.get(Contact, callback["contact_id"]).consent = False
        elif change == "pause":
            db.get(Campaign, callback["campaign_id"]).status = "paused"
        elif change == "phone":
            db.get(Contact, callback["contact_id"]).phone = "+79990001001"
        elif change == "operator":
            db.get(Operator, callback["operator_id"]).mobile_phone = "+79990001002"
        elif change == "number":
            db.scalar(select(CallerNumber)).quarantined = True
        elif change == "quota":
            db.scalar(select(CallerNumber)).daily_limit = 1
        else:
            monkeypatch.setenv("ATS_AMI_OPERATOR_NUMBER_MAP", '{"704":["9990000102"]}')
            settings.cache_clear()
    worker.call_control()
    assert callback["provider"].setvars[-1]["Value"] == ["deny"]


def test_no_confirmation_does_not_start_customer_leg(factory, callback):
    worker, attempt_id = start(factory, callback)
    leg_event(worker, attempt_id, "operator", "created")
    leg_event(worker, attempt_id, "operator", "answered")
    result(worker, attempt_id, "failed", "operator_not_confirmed")
    native(worker, "operator", "Hangup")
    with factory() as db:
        row = db.get(Attempt, UUID(attempt_id))
        assert row.status == "failed" and row.reason == "operator_not_confirmed"
        customer = db.scalar(select(CallLeg).where(CallLeg.role == "customer"))
        assert customer.channel == "" and customer.status == "cancelled"
    assert callback["provider"].setvars == []


def test_callback_needs_bridge_evidence_and_ignores_legacy_terminal_event(factory, callback):
    worker, attempt_id = start(factory, callback)
    for role in ("operator", "customer"):
        leg_event(worker, attempt_id, role, "created")
        leg_event(worker, attempt_id, role, "answered")
    confirmed(worker, attempt_id)
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id, "State": "completed",
    })
    worker.inbox()
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).status == "accepted"
    result(worker, attempt_id)
    for role in ("operator", "customer"):
        native(worker, role, "Hangup")
    with factory() as db:
        row = db.get(Attempt, UUID(attempt_id))
        assert row.status == "unknown" and row.reason == "callback_bridge_unconfirmed"
        assert db.get(Operator, callback["operator_id"]).status == "busy"


def test_callback_reserves_two_daily_units_and_respects_capacity(factory, callback):
    with factory.begin() as db:
        db.scalar(select(CallerNumber)).daily_limit = 1
        assert not schedule_one(db, callback["tenant_id"])
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
    with factory.begin() as db:
        db.scalar(select(CallerNumber)).daily_limit = 30
        db.get(Tenant, callback["tenant_id"]).max_channels = 1
    with factory.begin() as db:
        with pytest.raises(Problem) as error:
            schedule_one(db, callback["tenant_id"])
        assert error.value.code == "callback_needs_two_channels"


def test_operator_connection_is_validated_and_cannot_change_while_busy(logged, factory, callback):
    path = f"/api/v2/operators/{callback['operator_id']}/connection"
    assert logged.post(path, json={"connection_method": "mobile", "mobile_phone": "bad"}).status_code == 422
    saved = logged.post(path, json={"connection_method": "mobile", "mobile_phone": "8 (999) 000-10-00"})
    assert saved.status_code == 200, saved.text
    start(factory, callback)
    assert logged.post(path, json={"connection_method": "sip", "mobile_phone": ""}).status_code == 409


def test_callback_watchdog_keeps_operator_and_channels_until_legs_are_reconciled(factory, callback):
    worker, attempt_id = start(factory, callback)
    with factory.begin() as db:
        db.get(Attempt, UUID(attempt_id)).updated_at = now() - timedelta(hours=3)
    worker.watchdog()
    worker.watchdog()
    with factory() as db:
        call = db.get(Attempt, UUID(attempt_id))
        assert (call.status, call.reason) == ("unknown", "callback_evidence_missing")
        assert db.get(Operator, callback["operator_id"]).status == "busy"
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm")) == 0


def test_bulk_reconciliation_cannot_release_unobserved_callback_legs(factory, callback, logged):
    _, attempt_id = start(factory, callback)
    with factory.begin() as db:
        db.get(Attempt, UUID(attempt_id)).updated_at = now() - timedelta(hours=3)
    response = logged.post("/api/v2/calls/reconcile-stale")
    assert response.status_code == 200 and response.json()["requires_evidence"] == 1
    assert response.json()["reconciled"] == 0
    with factory() as db:
        assert db.get(Operator, callback["operator_id"]).status == "busy"
        assert db.get(Attempt, UUID(attempt_id)).status == "unknown"


@pytest.mark.parametrize("case,code", [
    ("disabled", "callback_disabled"), ("template", "callback_template_unsupported"),
])
def test_mobile_route_fails_before_allocation_for_unsupported_setup(factory, callback, case, code, monkeypatch):
    with factory.begin() as db:
        template_id = None
        if case == "template":
            template = Template(tenant_id=callback["tenant_id"], name="Template")
            db.add(template)
            db.flush()
            template_id = template.id
        else:
            monkeypatch.setenv("ATS_CALLBACK_ENABLED", "false")
            settings.cache_clear()
        with pytest.raises(Problem) as error:
            validate_route(callback["tenant_id"], "multicom_ami", db, mode="operator",
                           template_id=template_id, operator_id=callback["operator_id"])
        assert error.value.code == code
