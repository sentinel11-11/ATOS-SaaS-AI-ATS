import base64
import importlib.util
import io
import json
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ats.config import settings
from ats.inbound import ingest_inbound
from ats.legacy_import import migrate_workspace
from ats.models import (
    Attempt,
    Campaign,
    Contact,
    HistoricalCall,
    InboundCall,
    Operator,
    Outbox,
    QueueEntry,
    User,
)
from ats.workers import Worker

BASE = "/api/v1/workspace"


def test_inbound_dedup_consent_and_crm(factory, tenants, logged):
    for event in ["ringing", "answered", "completed", "completed", "ringing"]:
        with factory.begin() as db:
            ingest_inbound(
                db, tenants[0], "multicom_ami", "123.456", "89990000202", "9690229926", event, 15
            )
    with factory() as db:
        call = db.scalar(select(InboundCall))
        assert call.status == "completed" and call.phone == "+79990000202"
        assert db.scalar(select(Contact)).consent is False
        assert db.scalar(select(func.count()).select_from(Outbox)) == 1
    assert logged.get(BASE + "/incoming").json()["total"] == 1


def test_inbound_ami_observer_requires_enabled_context(factory, tenants, monkeypatch):
    monkeypatch.setenv("ATS_TELEPHONY_TENANT_ID", str(tenants[0]))
    monkeypatch.setenv("ATS_INBOUND_ENABLED", "true")
    settings.cache_clear()
    worker = Worker(factory)
    worker.ami_event(
        {
            "Event": "Newchannel",
            "Context": "unrelated",
            "Uniqueid": "bad",
            "CallerIDNum": "89990000202",
        }
    )
    worker.ami_event(
        {
            "Event": "Newchannel",
            "Context": "from-mcm",
            "Uniqueid": "123",
            "CallerIDNum": "89990000202",
        }
    )
    worker.ami_event(
        {"Event": "Newstate", "Context": "from-mcm", "Uniqueid": "123", "ChannelStateDesc": "Up"}
    )
    worker.ami_event({"Event": "Hangup", "Uniqueid": "123"})
    with factory() as db:
        calls = db.scalars(select(InboundCall)).all()
        assert len(calls) == 1 and calls[0].status == "completed"


def test_request_log_correlates_validation_without_input(logged):
    response = logged.post(
        BASE + "/contacts", json={"name": "private-name", "phone": "wrong-number"}
    )
    assert response.status_code == 422
    logs = logged.get(BASE + "/logs/runtime", params={"q": "validation_failed"}).json()["items"]
    assert logs[0]["request_id"] == response.json()["request_id"]
    assert logs[0]["http_status"] == 422
    assert "private-name" not in json.dumps(logs) and "wrong-number" not in json.dumps(logs)


def test_prepared_call_can_be_cancelled_without_provider(logged, prepared, factory):
    worker = Worker(factory)
    worker.schedule()
    with factory() as db:
        aid = db.scalar(select(Attempt)).id
    assert logged.post(BASE + f"/calls/{aid}/hangup").status_code == 202
    worker.telephony()
    with factory() as db:
        assert db.get(Attempt, aid).status == "failed"
        assert db.get(Attempt, aid).reason == "cancelled_by_user"


def test_call_phone_snapshot_is_not_rewritten(logged, prepared, factory):
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    cid = prepared[1]
    logged.put(
        BASE + f"/contacts/{cid}",
        json={"name": "New", "phone": "+79990000999", "consent": True, "consent_source": "Written"},
    )
    assert logged.get(BASE + "/calls").json()["items"][0]["phone"] == "+79990000101"
    exported = logged.get(BASE + "/export/calls.csv").text
    assert "+79990000101" in exported and "+79990000999" not in exported


def test_voice_result_inbox_creates_one_queue_task(factory, tenants, prepared, monkeypatch):
    worker = Worker(factory)
    worker.schedule()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        attempt.external_id = str(attempt.id)
        attempt.route = "multicom_ami"
        aid = attempt.id
    monkeypatch.setenv("ATS_TELEPHONY_TENANT_ID", str(tenants[0]))
    settings.cache_clear()
    result = base64.b64encode(json.dumps({"qualified": True, "transcript": []}).encode()).decode()
    event = {
        "Event": "UserEvent",
        "UserEvent": "ATSCall",
        "AttemptID": str(aid),
        "State": "completed",
        "ResultB64": result,
    }
    worker.ami_event(event)
    worker.ami_event(event)
    worker.inbox()
    with factory() as db:
        assert db.get(Attempt, aid).agent_result["qualified"] is True
        assert len(db.scalars(select(QueueEntry)).all()) == 1


def test_local_agi_script_has_no_shell_and_executes_branches():
    path = Path("deploy/asterisk/agi/ats_beta.py")
    spec = importlib.util.spec_from_file_location("ats_test_agi", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class AGI:
        def command(self, command):
            calls.append(command)
            return (1 if command.startswith("GET DATA") else 0, "")

    payload = {
        "name": "Тест",
        "mode": "agent",
        "scenario": {
            "greeting": "Здравствуйте",
            "questions": [
                {
                    "id": "q",
                    "text": "Интересно?",
                    "choices": {"1": {"next": "end", "qualified": True}},
                }
            ],
            "qualified_text": "Спасибо",
        },
    }
    result = module.execute(AGI(), payload, Path("/tmp"), renderer=lambda *_: "/tmp/safe-sound")
    assert result["qualified"] is True and any(c.startswith("GET DATA") for c in calls)
    agi = module.AGI(io.StringIO("200 result=1 (value)\n"), io.StringIO())
    assert agi.variable("ATS_ATTEMPT_ID") == "value"
    with pytest.raises(ValueError):
        agi.command("STREAM FILE test\nEXEC System bad")


def test_workspace_snapshot_dry_run_and_repeat(factory, tenants, tmp_path):
    path = tmp_path / "snapshot.db"
    source = sqlite3.connect(path)
    source.executescript("""
        CREATE TABLE contacts(id INTEGER, name TEXT, phone TEXT, consent INTEGER, consent_source TEXT, blacklisted INTEGER, database_id INTEGER);
        INSERT INTO contacts VALUES(1,'Legacy','89990000101',1,'written',0,1);
        CREATE TABLE templates(id INTEGER, name TEXT, text TEXT, scenario TEXT);
        INSERT INTO templates VALUES(1,'Template','Hello','{}');
        CREATE TABLE databases(id INTEGER,name TEXT,description TEXT);
        INSERT INTO databases VALUES(1,'Database','Test');
        CREATE TABLE campaigns(id INTEGER,name TEXT,template_id INTEGER,flow TEXT,status TEXT,max_channels INTEGER,schedule TEXT);
        INSERT INTO campaigns VALUES(1,'Old campaign',1,'message','running',1,'{}');
        CREATE TABLE calls(id INTEGER,contact_name TEXT,contact_phone TEXT,campaign_id INTEGER,provider TEXT,direction TEXT,status TEXT,result TEXT,duration_sec INTEGER,started_at TEXT);
        INSERT INTO calls VALUES(1,'Legacy','89990000101',1,'ami','out','done','ok',12,'2026-10-01 12:00:00');
        CREATE TABLE campaign_items(id INTEGER,campaign_id INTEGER,contact_id INTEGER,status TEXT,attempts INTEGER);
        INSERT INTO campaign_items VALUES(1,1,1,'done',1);
    """)
    source.commit()
    source.close()
    before = path.read_bytes()
    dry = migrate_workspace(factory, path, tenants[0], "legacy-main", False)
    assert dry["created"]["call"] == 1 and dry["created"]["item"] == 1
    with factory() as db:
        assert db.scalar(select(Contact)) is None
    result = migrate_workspace(factory, path, tenants[0], "legacy-main", True)
    assert result["created"]["call"] == 1
    repeat = migrate_workspace(factory, path, tenants[0], "legacy-main", True)
    assert repeat["skipped"]["call"] == 1
    with factory() as db:
        assert db.scalar(select(Campaign)).status == "paused"
        assert db.scalar(select(Campaign)).route == "sim"
        assert len(db.scalars(select(HistoricalCall)).all()) == 1
    assert before == path.read_bytes()


def test_live_operator_is_reserved_then_released(
    factory, prepared, tenants, monkeypatch, tmp_path, logged
):
    for key, value in {
        "ATS_LIVE_CALLS_ENABLED": "true",
        "ATS_TELEPHONY_TENANT_ID": str(tenants[0]),
        "ATS_TEST_DESTINATIONS": "+79990000101",
        "ATS_AMI_SECRET": "fixture",
        "ATS_AMI_CALLER_ID": "9690229926",
        "ATS_MEDIA_ENABLED": "true",
        "ATS_MEDIA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    settings.cache_clear()
    with factory.begin() as db:
        user = db.scalar(select(User).where(User.tenant_id == tenants[0]))
        user.role = "operator"
        db.add(Operator(tenant_id=tenants[0], user_id=user.id, status="free", extension="101"))
        campaign = db.scalar(select(Campaign))
        campaign.route = "multicom_ami"
        campaign.mode = "operator"
    captured = {}

    class Driver:
        def originate(self, attempt_id, phone, **kwargs):
            captured.update(kwargs)
            return str(attempt_id)

    worker = Worker(factory, drivers={"multicom_ami": Driver()})
    worker.schedule()
    worker.telephony()
    assert captured["operator_ext"] == "101" and captured["media"] is True
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        aid = attempt.id
        attempt.channel = "PJSIP/fixture-channel"
        assert db.scalar(select(Operator)).status == "busy"
        assert (tmp_path / f"{attempt.id}.json").is_file()
    queue = logged.get(BASE + "/operators").json()["queue"]
    assert queue[0]["phone"] == "+79990000101" and queue[0]["can_hangup"] is True
    assert logged.post(BASE + f"/calls/{aid}/hangup").status_code == 202
    with factory.begin() as db:
        from ats.services import finish_attempt

        finish_attempt(db, db.get(Attempt, aid), "completed")
    with factory() as db:
        assert db.scalar(select(Operator)).status == "free"
        assert len(db.scalars(select(QueueEntry)).all()) == 1


def test_recording_share_scope_revocation(logged, prepared, factory, monkeypatch, tmp_path):
    monkeypatch.setenv("ATS_RECORDINGS_DIR", str(tmp_path))
    settings.cache_clear()
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    aid = logged.get(BASE + "/calls").json()["items"][0]["id"]
    recording = logged.post(
        BASE + f"/calls/{aid}/recordings",
        files={"file": ("test.wav", b"RIFF\x00\x00\x00\x00WAVEtest")},
    ).json()
    result = logged.post(BASE + f"/recordings/{recording['id']}/share").json()
    from urllib.parse import urlsplit

    path = urlsplit(result["url"]).path
    logged.cookies.clear()
    logged.headers.pop("X-CSRF-Token", None)
    assert logged.get(path).status_code == 200
    assert logged.get("/api/v1/contacts").status_code == 401
    from ats.models import RecordingShare

    with factory.begin() as db:
        db.delete(db.get(RecordingShare, UUID(result["id"])))
    assert logged.get(path).status_code == 404


def test_delayed_retry_does_not_complete_campaign_early(factory, prepared):
    from ats.models import CampaignItem
    from ats.services import finish_attempt

    worker = Worker(factory)
    worker.schedule()
    with factory.begin() as db:
        campaign = db.scalar(select(Campaign))
        campaign.retry_max = 1
        campaign.retry_delay_minutes = 30
        attempt = db.scalar(select(Attempt))
        finish_attempt(db, attempt, "failed", "connect_failed")
    worker.schedule()
    with factory() as db:
        assert db.scalar(select(Campaign)).status == "running"
        assert db.scalar(select(CampaignItem)).status == "queued"
        assert len(db.scalars(select(Attempt)).all()) == 1


def test_imported_users_are_disabled_and_existing_admin_is_preserved(factory, tenants, tmp_path):
    path = tmp_path / "users.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE contacts(id INTEGER, name TEXT, phone TEXT); CREATE TABLE users(id INTEGER, login TEXT, role TEXT); INSERT INTO users VALUES(1,'admin','admin'),(2,'oldoperator','operator'); CREATE TABLE operators(id INTEGER,user_id INTEGER,name TEXT,ext TEXT); INSERT INTO operators VALUES(1,2,'Сотрудник','101');"
        )
    report = migrate_workspace(factory, path, tenants[0], "oldusers", True)
    assert report["created"]["user"] == 1
    with factory() as db:
        assert (
            db.scalar(
                select(User).where(User.tenant_id == tenants[0], User.login == "admin")
            ).active
            is True
        )
        user = db.scalar(select(User).where(User.login == "oldoperator"))
        assert user.active is False
        assert db.scalar(select(Operator).where(Operator.user_id == user.id)).status == "offline"
