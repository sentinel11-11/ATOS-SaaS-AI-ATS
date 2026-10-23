"""The unchanged legacy forms must perform real, tenant-scoped operations."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from ats.models import Attempt, CallerNumber, Contact, Operator, QueueEntry, Tenant, User, now
from ats.security import Principal
from ats.workers import Worker

PASSWORD = "FixturePasswordOnly.123"

BASE = "/api/v2"


def saved(logged, path, data):
    response = logged.post(BASE + path, json=data)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    return response.json()


def test_legacy_template_editor_accepts_json_and_preserves_graph(logged, tenants, factory):
    graph = {
        "greeting": "Здравствуйте, {name}",
        "questions": [{"id": "q1", "text": "Да?", "choices": {
            "да": {"next": "end", "qualified": True},
            "нет": {"next": "end", "qualified": False},
        }}],
    }
    tid = saved(logged, "/templates/save", {
        "id": 0, "name": "Оригинальный редактор", "text": "Сообщение", "scenario": graph,
    })["id"]
    rows = logged.get(BASE + "/templates").json()["templates"]
    assert rows[0]["id"] == tid
    assert rows[0]["scenario"]["questions"][0]["choices"]["1"]["qualified"] is True
    saved(logged, "/templates/save", {**rows[0], "name": "Изменён"})
    assert logged.get(BASE + "/templates").json()["templates"][0]["name"] == "Изменён"
    invalid = logged.post(BASE + "/templates/save", json={
        "name": "Invalid", "scenario": {"questions": [
            {"id": "q1", "text": "Question", "choices": {"1": {"next": "missing"}}},
        ]},
    })
    assert invalid.status_code == 422
    saved(logged, "/templates/delete", {"id": tid})
    assert logged.get(BASE + "/templates").json()["templates"] == []
    with factory() as db:
        assert db.get(__import__("ats.models", fromlist=["Template"]).Template, UUID(tid)).archived


def test_legacy_caller_pool_partial_edits_reset_actual_counters(logged, prepared, factory):
    nid = saved(logged, "/numbers/save", {
        "number": "+7 (999) 000-00-20", "label": "MCM", "provider": "ami", "kind": "sip",
        "daily_limit": 5, "weight": 4,
    })["id"]
    saved(logged, "/numbers/save", {"id": nid, "active": False})
    number = logged.get(BASE + "/numbers").json()["numbers"][0]
    assert (number["number"], number["provider"], number["weight"], number["active"]) == (
        "9990000020", "ami", 4, False,
    )
    saved(logged, "/numbers/save", {**number, "active": True, "enabled_outgoing": False})
    saved(logged, "/numbers/quarantine", {"id": nid, "on": True})
    Worker(factory).schedule()
    with factory.begin() as db:
        call = db.scalar(select(Attempt))
        call.caller_number, call.answered_at = "9990000020", now()
        row = db.get(CallerNumber, UUID(nid))
        row.last_used_at = now() + timedelta(seconds=10)
        before_quota = db.get(Tenant, row.tenant_id).daily_limit
    number = logged.get(BASE + "/numbers").json()["numbers"][0]
    assert (number["daily_count"], number["dialed_total"], number["answered_total"]) == (1, 1, 1)
    assert number["cooling"] and number["quarantined"]
    saved(logged, "/numbers/reset", {})
    number = logged.get(BASE + "/numbers").json()["numbers"][0]
    assert number["daily_count"] == 0 and number["dialed_total"] == 1
    assert not number["cooling"] and not number["quarantined"]
    assert number["enabled_outgoing"] is False
    with factory() as db:
        assert db.scalar(select(Attempt)) is not None
        assert db.get(Tenant, db.get(CallerNumber, UUID(nid)).tenant_id).daily_limit == before_quota


def test_legacy_blacklist_normalizes_and_never_recreates_consent(logged, prepared, factory):
    entry = saved(logged, "/blacklist/add", {"phone": "89990000101", "reason": "Отказ"})
    rows = logged.get(BASE + "/blacklist").json()["blacklist"]
    assert rows[0]["phone"] == "+79990000101"
    with factory() as db:
        row = db.get(Contact, UUID(prepared[1]))
        assert row.blocked and not row.consent
    saved(logged, "/blacklist/delete", {"id": entry["id"]})
    assert logged.get(BASE + "/blacklist").json()["blacklist"] == []
    with factory() as db:
        row = db.get(Contact, UUID(prepared[1]))
        assert row.blocked and not row.consent


def test_legacy_pool_cross_tenant_edits_fail(logged, tenants, factory):
    with factory.begin() as db:
        other = CallerNumber(tenant_id=tenants[1], phone="9990000033", provider="ami")
        db.add(other)
        db.flush()
        other_id = str(other.id)
    assert logged.get(BASE + "/numbers").json()["numbers"] == []
    for path, data in (
        ("/numbers/save", {"id": other_id, "label": "stolen"}),
        ("/numbers/quarantine", {"id": other_id, "on": True}),
        ("/numbers/delete", {"id": other_id}),
    ):
        assert logged.post(BASE + path, json=data).status_code == 404


def test_legacy_acd_projects_real_queue_and_admin_assigns_existing_operator(
    logged, tenants, prepared, factory, password_hash,
):
    Worker(factory).schedule()
    with factory.begin() as db:
        user = User(tenant_id=tenants[0], login="operator1", role="operator",
                    password_hash=password_hash)
        db.add(user)
        db.flush()
        op = Operator(tenant_id=tenants[0], user_id=user.id, name="Оператор", extension="101",
                      status="free")
        db.add(op)
        call = db.scalar(select(Attempt))
        row = QueueEntry(tenant_id=tenants[0], attempt_id=call.id)
        db.add(row)
        db.flush()
        queue_id, op_id = str(row.id), str(op.id)
    state = logged.get(BASE + "/acd").json()
    assert state["acd"][0]["id"] == queue_id
    assert state["operators"][0]["ext"] == "101"
    result = saved(logged, "/acd/accept", {"id": queue_id, "operator_id": op_id})
    assert result["operator_id"] == op_id
    assert logged.get(BASE + "/acd").json()["acd"] == []
    assert logged.post(BASE + "/acd/accept", json={"id": queue_id}).status_code == 409
    with factory() as db:
        assert db.get(Operator, UUID(op_id)).status == "busy"
        assert db.get(QueueEntry, UUID(queue_id)).status == "accepted"


def test_legacy_operator_cannot_assign_colleague_or_bypass_call_ownership(
    logged, tenants, prepared, factory, password_hash,
):
    import pytest

    from ats.compat_resources import CompleteCommand, complete_operator_call
    from ats.domain import Problem

    Worker(factory).schedule()
    with factory.begin() as db:
        users, ops = [], []
        for index in range(2):
            user = User(tenant_id=tenants[0], login=f"operator{index}", role="operator",
                        password_hash=password_hash)
            db.add(user)
            db.flush()
            op = Operator(tenant_id=tenants[0], user_id=user.id, name=f"Op {index}",
                          extension=str(100 + index), status="free")
            db.add(op)
            users.append(user)
            ops.append(op)
        call = db.scalar(select(Attempt))
        row = QueueEntry(tenant_id=tenants[0], attempt_id=call.id)
        db.add(row)
        db.flush()
        queue_id, op_ids = str(row.id), [str(op.id) for op in ops]
        call_id, second_uid = call.id, users[1].id
    response = logged.post("/api/v1/auth/login", json={
        "tenant_id": str(tenants[0]), "login": "operator0", "password": PASSWORD,
    })
    assert response.status_code == 200
    logged.headers["X-CSRF-Token"] = logged.cookies["ats_csrf"]
    result = saved(logged, "/acd/accept", {"id": queue_id, "operator_id": op_ids[1]})
    assert result["operator_id"] == op_ids[0]
    with factory() as db:
        with pytest.raises(Problem) as caught:
            complete_operator_call(
                CompleteCommand(call_id=call_id),
                Principal(tenants[0], second_uid, "operator", "operator1"), db,
            )
        assert caught.value.status == 403
