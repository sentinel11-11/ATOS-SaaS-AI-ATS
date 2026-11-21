from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ats.models import Attempt, CallEvent, Inbox, Operator, Outbox, QueueEntry, User, now
from ats.workers import Worker


def answered_call(factory, prepared, tenants, *, emit_answer=True):
    worker = Worker(factory)
    worker.schedule()
    assert worker.telephony()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        attempt.channel = "Local/fixture-completion;1"
        attempt_id = str(attempt.id)
        user = User(
            tenant_id=tenants[0], login="completion-operator", role="operator",
            password_hash="unused-fixture-hash",
        )
        db.add(user)
        db.flush()
        operator = Operator(tenant_id=tenants[0], user_id=user.id, extension="704", status="busy")
        db.add(operator)
        db.flush()
        db.add(QueueEntry(
            tenant_id=tenants[0], attempt_id=attempt.id, operator_id=operator.id, status="accepted",
        ))
        operator_id = operator.id
    if emit_answer:
        worker.ami_event({
            "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id,
            "State": "answered",
        })
        worker.inbox()
    return worker, attempt_id, operator_id


def hangup(worker):
    worker.ami_event({
        "Event": "Hangup", "Channel": "Local/fixture-completion;1",
        "Uniqueid": "fixture-completion", "Cause": "16",
    })


def expire_observation(factory, attempt_id):
    with factory.begin() as db:
        for row in db.scalars(select(Inbox).where(Inbox.external_id == attempt_id)):
            row.available_at = now() - timedelta(seconds=1)


def crm_count(db):
    return db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm"))


@pytest.mark.parametrize("result,reason", [("failed", "operator_unavailable"), ("completed", "")])
def test_channel_hangup_does_not_hide_scenario_result(factory, prepared, tenants, result, reason):
    worker, attempt_id, operator_id = answered_call(factory, prepared, tenants)
    hangup(worker)
    worker.inbox()
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).status == "answered"
        assert db.get(Operator, operator_id).status == "busy"
        assert crm_count(db) == 0
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id,
        "State": result, "ReasonCode": reason,
    })
    worker.inbox()
    expire_observation(factory, attempt_id)
    worker.inbox()
    with factory() as db:
        row = db.get(Attempt, UUID(attempt_id))
        assert row.status == result and row.reason == reason
        assert db.get(Operator, operator_id).status == "free"
        assert crm_count(db) == 1


def test_unconfirmed_hangup_keeps_resources_until_evidence_or_scenario_result(
    factory, prepared, tenants, logged
):
    worker, attempt_id, operator_id = answered_call(factory, prepared, tenants)
    hangup(worker)
    expire_observation(factory, attempt_id)
    worker.inbox()
    with factory.begin() as db:
        row = db.get(Attempt, UUID(attempt_id))
        assert row.status == "unknown" and row.reason == "ami_terminal_event_missing"
        assert db.get(Operator, operator_id).status == "busy"
        assert crm_count(db) == 0
        row.updated_at = now() - timedelta(hours=1)
    worker.watchdog()
    response = logged.post("/api/v2/calls/reconcile-stale")
    assert response.status_code == 200, response.text
    assert response.json()["requires_evidence"] == 1
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).status == "unknown"
        assert db.get(Operator, operator_id).status == "busy"
        assert crm_count(db) == 0
    no_evidence = logged.post(f"/api/v1/attempts/{attempt_id}/resolve", json={"status": "failed"})
    assert no_evidence.status_code == 422
    resolved = logged.post(f"/api/v1/attempts/{attempt_id}/resolve", json={
        "status": "failed", "evidence": "CDR fixture confirms the call ended",
    })
    assert resolved.status_code == 200, resolved.text
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id,
        "State": "failed", "ReasonCode": "operator_unavailable",
    })
    worker.inbox()
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).reason == "CDR fixture confirms the call ended"
        assert db.get(Operator, operator_id).status == "free"
        assert crm_count(db) == 1
        final_event = db.scalar(select(CallEvent).where(
            CallEvent.attempt_id == UUID(attempt_id), CallEvent.event == "failed"
        ))
        assert final_event.detail == "CDR fixture confirms the call ended"


def test_late_scenario_completion_resolves_observation_once(factory, prepared, tenants):
    worker, attempt_id, operator_id = answered_call(factory, prepared, tenants, emit_answer=False)
    hangup(worker)
    expire_observation(factory, attempt_id)
    worker.inbox()
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id, "State": "answered",
    })
    worker.inbox()
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).status == "unknown"
        assert db.get(Attempt, UUID(attempt_id)).answered_at is not None
        assert db.get(Operator, operator_id).status == "busy"
    for _ in range(2):
        worker.ami_event({
            "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id,
            "State": "completed",
        })
        worker.inbox()
    with factory() as db:
        row = db.get(Attempt, UUID(attempt_id))
        assert row.status == "completed" and row.reason == ""
        assert db.get(Operator, operator_id).status == "free"
        assert crm_count(db) == 1


def test_unrelated_channel_hangup_does_not_finish_call(factory, prepared, tenants):
    worker, attempt_id, operator_id = answered_call(factory, prepared, tenants)
    worker.ami_event({
        "Event": "Hangup", "Channel": "PJSIP/unrelated-0001", "Uniqueid": "other", "Cause": "16",
    })
    worker.inbox()
    with factory() as db:
        assert db.get(Attempt, UUID(attempt_id)).status == "answered"
        assert db.get(Operator, operator_id).status == "busy"
        assert crm_count(db) == 0
