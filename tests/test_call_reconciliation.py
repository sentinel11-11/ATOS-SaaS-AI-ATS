import os
import subprocess
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_mobile_callback import callback as callback_fixture
from test_widget_protocol import browser_agent as browser_agent_fixture
from test_widget_protocol import create, payload
from test_widget_protocol import widget as widget_fixture

from ats.adapters import Retryable
from ats.models import Attempt, CallLeg, Operator, Outbox, QueueEntry, now
from ats.workers import Worker

callback, widget, browser_agent = callback_fixture, widget_fixture, browser_agent_fixture


@pytest.fixture(params=["forward", "browser"])
def stuck(factory, widget, browser_agent, request):
    ident = create(widget, payload(mode=request.param, protocol_version=3))["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{ident}/start", headers=widget["headers"])
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        attempt.status, attempt.reason = "unknown", "callback_evidence_missing"
        attempt_id = attempt.id
        db.scalar(select(Outbox).where(Outbox.kind == "telephony")).available_at = now() - timedelta(minutes=2)
    return {**widget, "call_id": ident, "attempt_id": attempt_id, "worker": worker}


def cancel(stuck):
    result = stuck["client"].post(f"/api/v1/amo/widget/v2/calls/{stuck['call_id']}/hangup", headers=stuck["headers"])
    assert result.status_code == 202, result.text


def due(factory):
    with factory.begin() as db:
        for job in db.scalars(select(Outbox).where(Outbox.kind == "control")):
            job.available_at = now() - timedelta(seconds=1)


def test_cancel_without_recorded_channels_proves_absence_and_releases_once(factory, stuck):
    cancel(stuck)
    assert stuck["worker"].call_control()
    with factory() as db:
        call = db.get(Attempt, stuck["attempt_id"])
        assert call.status == "failed" and call.reason == "cancelled_by_user"
        assert db.get(Operator, stuck["operator_id"]).status == "free"
        assert db.scalar(select(QueueEntry)).status == "completed"
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm")) == 1
    cancel(stuck)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm")) == 1


def test_cancel_hangs_up_only_verified_call_channels_even_if_root_has_disappeared(factory, stuck):
    provider = stuck["provider"]
    target, other = "PJSIP/owned-call-00000001", "PJSIP/unrelated-call-00000002"
    provider.channels.update({
        target: {"UNIQUEID": "fixture.1", "ATS_ATTEMPT_ID": str(stuck["attempt_id"])},
        other: {"UNIQUEID": "fixture.2", "ATS_ATTEMPT_ID": str(uuid4())},
    })
    with factory.begin() as db:
        db.get(Attempt, stuck["attempt_id"]).channel = "Local/disappeared-root;1"
    cancel(stuck)
    assert stuck["worker"].call_control()
    assert provider.hangups == [target] and other in provider.channels
    with factory() as db:
        assert db.get(Operator, stuck["operator_id"]).status == "busy"
    due(factory)
    assert stuck["worker"].call_control()
    with factory() as db:
        assert db.get(Attempt, stuck["attempt_id"]).status == "failed"
        assert db.get(Operator, stuck["operator_id"]).status == "free"


def test_incomplete_ami_snapshot_keeps_operator_reserved_and_control_retryable(factory, stuck):
    stuck["provider"].list_items_override = 3
    cancel(stuck)
    assert stuck["worker"].call_control()
    with factory() as db:
        assert db.get(Attempt, stuck["attempt_id"]).status == "unknown"
        assert db.get(Operator, stuck["operator_id"]).status == "busy"
        control = db.scalar(select(Outbox).where(Outbox.kind == "control"))
        assert control.status == "pending" and control.last_error == "ami_channel_snapshot_incomplete"


def test_reused_channel_identity_is_never_hung_up_or_reconciled_as_absent(factory, stuck):
    name = "PJSIP/reused-00000001"
    with factory.begin() as db:
        leg = db.scalar(select(CallLeg).where(CallLeg.role == "operator"))
        leg.channel, leg.uniqueid = name, "old.1"
    stuck["provider"].channels[name] = {"UNIQUEID": "new.1", "ATS_ATTEMPT_ID": str(uuid4())}
    cancel(stuck)
    stuck["worker"].call_control()
    assert stuck["provider"].hangups == []
    with factory() as db:
        assert db.get(Operator, stuck["operator_id"]).status == "busy"


@pytest.mark.parametrize("status", ["leased", "pending", "recently_done"])
def test_empty_snapshot_during_or_just_after_originate_does_not_release_operator(factory, stuck, status):
    with factory.begin() as db:
        job = db.scalar(select(Outbox).where(Outbox.kind == "telephony"))
        job.status = "done" if status == "recently_done" else status
        if status == "recently_done":
            job.available_at = now()
    cancel(stuck)
    stuck["worker"].call_control()
    with factory() as db:
        assert db.get(Operator, stuck["operator_id"]).status == "busy"
        assert db.get(Attempt, stuck["attempt_id"]).status == "unknown"


@pytest.mark.parametrize("status", ["dead", "unknown", "done"])
def test_explicit_hangup_retry_requeues_existing_control_job(factory, stuck, status):
    cancel(stuck)
    with factory.begin() as db:
        control = db.scalar(select(Outbox).where(Outbox.kind == "control"))
        control.status, control.last_error = status, "ami_action_rejected"
        ident = control.id
    cancel(stuck)
    with factory() as db:
        control = db.scalar(select(Outbox).where(Outbox.kind == "control"))
        assert control.id == ident and control.status == "pending" and not control.last_error
    assert stuck["worker"].call_control()


def test_completed_scenario_without_bridge_proof_is_not_reported_as_success(factory, stuck):
    with factory.begin() as db:
        db.get(Attempt, stuck["attempt_id"]).pending_terminal = {"state": "completed", "reason": ""}
    cancel(stuck)
    stuck["worker"].call_control()
    with factory() as db:
        assert db.get(Attempt, stuck["attempt_id"]).status == "failed"


def test_lost_end_time_does_not_inflate_conversation_duration(factory, stuck):
    with factory.begin() as db:
        call = db.get(Attempt, stuck["attempt_id"])
        call.bridged_at = call.answered_at = now() - timedelta(hours=2)
        call.pending_terminal = {"state": "completed", "reason": ""}
    cancel(stuck)
    stuck["worker"].call_control()
    with factory() as db:
        call = db.get(Attempt, stuck["attempt_id"])
        assert call.status == "failed" and call.agent_result["duration_unconfirmed"]
    result = stuck["client"].get(f"/api/v1/amo/widget/v2/calls/{stuck['call_id']}", headers=stuck["headers"]).json()
    assert result["duration_sec"] == 0


def test_orphaned_channels_can_be_closed_after_manual_database_cleanup(factory, stuck):
    from ats.call_reconciliation import reconcile_callback

    with factory.begin() as db:
        call = db.get(Attempt, stuck["attempt_id"])
        call.status, call.reason = "failed", "manual_stale_reconcile"
    name = "PJSIP/orphaned-00000001"
    stuck["provider"].channels[name] = {"UNIQUEID": "orphan.1", "ATS_ATTEMPT_ID": str(stuck["attempt_id"])}
    driver = stuck["worker"].driver("multicom_ami", stuck["tenant_id"])
    assert reconcile_callback(factory, stuck["tenant_id"], stuck["attempt_id"], driver, hangup=True)["state"] == "ending"
    assert reconcile_callback(factory, stuck["tenant_id"], stuck["attempt_id"], driver, hangup=True)["state"] == "terminal"
    with factory() as db:
        assert db.get(Attempt, stuck["attempt_id"]).reason == "manual_stale_reconcile"


def test_reconciliation_does_not_free_operator_reserved_by_another_call(factory, stuck):
    from ats.callbacks import queue_hangup

    with factory.begin() as db:
        original = db.get(Attempt, stuck["attempt_id"])
        other = Attempt(tenant_id=original.tenant_id, item_id=original.item_id, ordinal=2,
                        route=original.route, status="accepted")
        db.add(other)
        db.flush()
        db.add(QueueEntry(tenant_id=original.tenant_id, attempt_id=other.id,
                          operator_id=stuck["operator_id"], status="accepted"))
        other_id = other.id
        queue_hangup(db, original)
    stuck["worker"].call_control()
    with factory() as db:
        assert db.get(Attempt, stuck["attempt_id"]).status == "failed"
        assert db.get(Attempt, other_id).status == "accepted"
        assert db.get(Operator, stuck["operator_id"]).status == "busy"


def test_management_helper_inspects_without_writes_then_cancels_exact_call(factory, stuck, tmp_path):
    command = [".venv/bin/python", "scripts/reconcile-widget-call.py", "--widget-call-id", stuck["call_id"][:8],
               "--env-file", str(tmp_path / "absent.env")]
    inspected = subprocess.run(command, env={**os.environ}, capture_output=True, text=True, check=True)
    assert '"live_channels": 0' in inspected.stdout
    with factory() as db:
        assert db.get(Attempt, stuck["attempt_id"]).status == "unknown"
    cancelled = subprocess.run([*command, "--hangup"], capture_output=True, text=True, check=True)
    assert '"state": "terminal"' in cancelled.stdout
    with factory() as db:
        assert db.get(Attempt, stuck["attempt_id"]).status == "failed"


def test_channel_snapshot_checks_action_identity_and_complete_event(factory, stuck):
    driver = stuck["worker"].driver("multicom_ami", stuck["tenant_id"])
    assert driver.channels() == []
    stuck["provider"].list_items_override = 1
    with pytest.raises(Retryable) as error:
        driver.channels()
    assert error.value.code == "ami_channel_snapshot_incomplete"


def scenario_failure(factory, stuck):
    with factory.begin() as db:
        call = db.get(Attempt, stuck["attempt_id"])
        if call.call_flow == "browser_callback":
            root = f"Local/{stuck['operator_id']}@ats-beta-callback-browser-00000001;2"
        else:
            root = "Local/+79990001000@ats-beta-callback-operator-00000001;2"
        db.scalar(select(Outbox).where(Outbox.kind == "telephony")).available_at = now()
    stuck["worker"].ami_event({"Event": "UserEvent", "UserEvent": "ATSCallback",
        "AttemptID": str(stuck["attempt_id"]), "Channel": root, "Leg": "operator",
        "State": "failed", "DialStatus": "CHANUNAVAIL", "ReasonCode": "operator_unavailable"})


def test_immediate_chanunavail_is_reconciled_without_a_hangup_click_or_timeout(factory, stuck):
    scenario_failure(factory, stuck)
    with factory() as db:
        job = db.scalar(select(Outbox).where(Outbox.kind == "control"))
        assert job.payload["command"] == "callback_reconcile"
    stuck["worker"].call_control()
    with factory() as db:
        call = db.get(Attempt, stuck["attempt_id"])
        assert call.status == "failed" and call.reason == "operator_unavailable"
        assert db.get(Operator, stuck["operator_id"]).status == "free"
    assert stuck["provider"].hangups == []


def test_automatic_reconciliation_never_hangs_up_a_still_live_call(factory, stuck):
    scenario_failure(factory, stuck)
    stuck["provider"].channels["PJSIP/still-live-00000001"] = {
        "UNIQUEID": "live.1", "ATS_ATTEMPT_ID": str(stuck["attempt_id"])}
    stuck["worker"].call_control()
    assert stuck["provider"].hangups == []
    with factory() as db:
        assert db.get(Operator, stuck["operator_id"]).status == "busy"
        assert db.get(Attempt, stuck["attempt_id"]).status == "unknown"
