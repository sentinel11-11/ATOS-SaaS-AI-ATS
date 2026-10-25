from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from ats.config import settings
from ats.models import (
    Attempt,
    BlacklistEntry,
    CallerNumber,
    Campaign,
    CampaignItem,
    Contact,
    Outbox,
    Tenant,
    User,
    now,
)
from ats.workers import Worker

BASE = "/api/v1/workspace"


def contact(logged, **changes):
    response = logged.post(
        BASE + "/contacts",
        json={
            "name": "Тест",
            "phone": "+79990000001",
            "consent": True,
            "consent_source": "Test consent",
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_contacts_search_edit_archive_preserves_history(logged, prepared, factory):
    _, cid, _ = prepared
    assert logged.get(BASE + "/contacts", params={"q": "9990000101"}).json()["total"] == 1
    result = logged.put(
        BASE + f"/contacts/{cid}",
        json={
            "name": "Изменён",
            "phone": "+79990000101",
            "group": "VIP",
            "consent": True,
            "consent_source": "Test",
        },
    )
    assert result.status_code == 200
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    assert logged.delete(BASE + f"/contacts/{cid}").status_code == 204
    assert logged.get(BASE + "/contacts").json()["total"] == 0
    assert len(logged.get(BASE + f"/contacts/{cid}/history").json()["calls"]) == 1


def test_import_lists_duplicates_and_consent(logged):
    c = contact(logged, consent=False)
    group = logged.post(BASE + "/lists", json={"name": "База"}).json()
    response = logged.post(
        BASE + "/contacts-import",
        json={
            "text": "phone;name\n89990000001;Существующий\n89990000002;Новый\nошибка;Плохой",
            "list_id": group["id"],
            "consent": True,
            "consent_source": "Written",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1 and response.json()["existing"] == 1
    assert response.json()["rejected"] == [{"line": 4, "code": "invalid_contact"}]
    rows = logged.get(BASE + "/contacts", params={"list_id": group["id"]}).json()
    assert rows["total"] == 2
    assert next(r for r in rows["items"] if r["id"] == c["id"])["consent"] is False
    assert logged.delete(BASE + f"/lists/{group['id']}").status_code == 204
    assert logged.get(BASE + "/contacts").json()["total"] == 2


def test_blacklist_blocks_new_contacts_and_dispatch(logged, prepared, factory):
    worker = Worker(factory)
    worker.schedule()
    blocked = logged.post(BASE + "/blacklist", json={"phone": "+79990000101", "reason": "Отзыв"})
    assert blocked.status_code == 201
    worker.telephony()
    with factory() as db:
        assert db.scalar(select(Attempt)).reason == "consent_withdrawn"
    cid = prepared[1]
    assert (
        logged.patch(
            f"/api/v1/contacts/{cid}",
            json={"consent": True, "consent_source": "test", "blocked": False},
        ).status_code
        == 409
    )
    assert logged.delete(BASE + f"/blacklist/{blocked.json()['id']}").status_code == 204
    assert logged.get(BASE + "/contacts").json()["items"][0]["consent"] is False


def test_blacklist_cannot_be_bypassed_through_edit(logged):
    c = contact(logged)
    logged.post(BASE + "/blacklist", json={"phone": c["phone"], "reason": "Не звонить"})
    response = logged.put(
        BASE + f"/contacts/{c['id']}",
        json={
            "name": "Тест",
            "phone": c["phone"],
            "consent": True,
            "consent_source": "test",
            "blocked": False,
        },
    )
    assert response.json()["blocked"] is True


def test_template_branching_and_cycle_limit(logged):
    template = logged.post(
        BASE + "/templates",
        json={
            "name": "Диалог",
            "scenario": {
                "questions": [
                    {
                        "id": "q",
                        "text": "Интересно?",
                        "choices": {
                            "1": {"next": "end", "qualified": True},
                            "2": {"next": "end", "qualified": False},
                        },
                    }
                ]
            },
        },
    )
    assert template.status_code == 201, template.text
    path = BASE + f"/templates/{template.json()['id']}/test"
    assert logged.post(path, json={"answers": {"q": "да"}}).json()["qualified"] is True
    assert logged.post(path, json={"answers": {"q": "мусор"}}).json()["qualified"] is False
    result = logged.put(
        BASE + f"/templates/{template.json()['id']}",
        json={
            "name": "Цикл",
            "scenario": {"questions": [{"id": "q", "text": "?", "choices": {"1": {"next": "q"}}}]},
        },
    )
    assert result.status_code == 200
    assert logged.post(path, json={"answers": {"q": "1"}}).status_code == 422


def test_caller_pool_limits_and_quarantine(logged, prepared, factory, monkeypatch):
    monkeypatch.setenv("ATS_LIVE_CALLS_ENABLED", "true")
    with factory.begin() as db:
        tenant = db.scalar(select(Tenant))
        tid = tenant.id
        db.scalar(select(Campaign)).route = "multicom_ami"
    monkeypatch.setenv("ATS_TELEPHONY_TENANT_ID", str(tid))
    monkeypatch.setenv("ATS_TEST_DESTINATIONS", "+79990000101")
    monkeypatch.setenv("ATS_AMI_SECRET", "fixture")
    monkeypatch.setenv("ATS_AMI_CALLER_ID", "9990000101")
    settings.cache_clear()
    row = logged.post(
        BASE + "/numbers", json={"phone": "9690229926", "verified": True, "quarantined": True}
    ).json()
    worker = Worker(factory)
    worker.schedule()
    with factory() as db:
        assert db.scalar(select(Attempt)) is None
    assert (
        logged.put(
            BASE + f"/numbers/{row['id']}",
            json={"phone": "9690229926", "verified": True, "quarantined": False},
        ).status_code
        == 200
    )
    worker.schedule()
    with factory() as db:
        assert db.scalar(select(Attempt)).caller_number == "9690229926"


def test_campaign_retry_preserves_attempts_and_unknown_is_not_retried(logged, prepared, factory):
    campid = prepared[0]
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        attempt.status = "failed"
        db.get(CampaignItem, attempt.item_id).status = "failed"
        db.get(Campaign, UUID(campid)).status = "paused"
    assert logged.post(BASE + f"/campaigns/{campid}/retry-failed").json()["queued"] == 1
    logged.post(f"/api/v1/campaigns/{campid}/state", json={"status": "running"})
    worker.schedule()
    worker.telephony()
    with factory.begin() as db:
        attempts = db.scalars(select(Attempt).order_by(Attempt.ordinal)).all()
        assert [a.ordinal for a in attempts] == [1, 2]
        attempts[1].status = "unknown"
        db.get(Campaign, UUID(campid)).status = "paused"
    assert logged.post(BASE + f"/campaigns/{campid}/retry-failed").status_code == 409


def test_campaign_edit_clear_and_archive(logged, prepared, factory):
    campid = prepared[0]
    assert logged.delete(BASE + f"/campaigns/{campid}").status_code == 409
    logged.post(f"/api/v1/campaigns/{campid}/state", json={"status": "paused"})
    assert (
        logged.put(
            BASE + f"/campaigns/{campid}",
            json={"name": "Новое", "window_start": 0, "window_end": 24},
        ).status_code
        == 200
    )
    assert logged.post(BASE + f"/campaigns/{campid}/clear-queue").json()["cancelled"] == 1
    assert logged.delete(BASE + f"/campaigns/{campid}").status_code == 204
    assert logged.get("/api/v1/campaigns").json() == []
    assert (
        logged.post(f"/api/v1/campaigns/{campid}/state", json={"status": "running"}).status_code
        == 409
    )


def test_calls_reports_timeline_and_csv(logged, prepared, factory):
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    result = logged.get(BASE + "/calls").json()
    assert result["total"] == 1 and result["items"][0]["contact_name"] == "Тест"
    aid = result["items"][0]["id"]
    detail = logged.get(BASE + f"/calls/{aid}").json()
    assert [e["event"] for e in detail["events"]] == ["prepared", "dispatching", "completed"]
    assert logged.get(BASE + "/reports").json()["completion_percent"] == 100
    assert logged.get(BASE + "/calls", params={"status": "failed"}).json()["total"] == 0
    contact(logged, name="=SUM(1,2)")
    csv = logged.get(BASE + "/export/contacts.csv")
    assert "'=SUM(1,2)" in csv.text and "attachment" in csv.headers["content-disposition"]
    assert logged.get(BASE + "/logs/audit").json()["total"] >= 2


def test_user_management_last_admin_and_session_revocation(logged, factory, tenants):
    with factory() as db:
        uid = str(db.scalar(select(User).where(User.tenant_id == tenants[0])).id)
    assert (
        logged.put(
            BASE + f"/users/{uid}", json={"login": "admin", "active": False, "role": "admin"}
        ).status_code
        == 409
    )
    response = logged.post(
        BASE + "/users",
        json={
            "login": "Operator1",
            "password": "OnlyFixturePassword.123",
            "role": "operator",
            "name": "Оператор",
            "extension": "101",
        },
    )
    assert response.status_code == 201, response.text
    users = logged.get(BASE + "/users").json()
    assert all("password_hash" not in u for u in users)
    assert next(u for u in users if u["login"] == "operator1")["extension"] == "101"


@pytest.mark.parametrize(
    "path", ["/users", "/settings", "/logs/audit", "/logs/runtime", "/logs/outbox"]
)
def test_admin_routes_reject_viewer(logged, factory, tenants, path):
    with factory.begin() as db:
        db.scalar(select(User).where(User.tenant_id == tenants[0])).role = "viewer"
    assert logged.get(BASE + path).status_code == 403


def test_workspace_tenant_isolation(logged, prepared, factory, tenants):
    with factory.begin() as db:
        foreign = Contact(tenant_id=tenants[1], name="Private", phone="+79990000202", consent=False)
        db.add(foreign)
        db.flush()
        cid = foreign.id
    assert logged.get(BASE + f"/contacts/{cid}/history").status_code == 404
    assert logged.delete(BASE + f"/contacts/{cid}").status_code == 404
    assert "Private" not in logged.get(BASE + "/contacts").text
    assert logged.get(BASE + "/contacts?offset=-1").status_code == 422


def test_settings_redact_secrets_and_validate(logged, monkeypatch):
    monkeypatch.setenv("ATS_AMI_SECRET", "never-echo-me")
    settings.cache_clear()
    result = logged.get(BASE + "/settings")
    assert result.status_code == 200 and "never-echo-me" not in result.text
    assert result.json()["connections"]["ami_secret_set"] is True
    assert (
        logged.put(BASE + "/settings/providers", json={"ami_secret": "injected"}).status_code == 422
    )
    assert (
        logged.put(
            BASE + "/settings/tenant",
            json={"name": "x", "timezone": "bad-zone", "max_channels": 1, "daily_limit": 5},
        ).status_code
        == 422
    )


def test_provider_secret_is_encrypted_and_round_trips(logged, factory, monkeypatch):
    from cryptography.fernet import Fernet

    from ats.config import settings
    from ats.models import ConnectionProfile

    monkeypatch.setenv("ATS_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    settings.cache_clear()
    response = logged.post(
        "/api/v2/settings/raw",
        json={
            "provider_config": {
                "ami": {
                    "host": "127.0.0.1",
                    "port": 5038,
                    "user": "preprod-ami",
                    "secret": "secret-never-returned",
                }
            }
        },
    )
    assert response.status_code == 200, response.text
    assert "secret-never-returned" not in response.text
    public = logged.get("/api/v2/settings/raw")
    assert public.status_code == 200
    assert public.json()["provider_config"]["ami"]["secret"] == "********"
    with factory() as db:
        row = db.scalar(select(ConnectionProfile))
        assert row.encrypted_secrets and "secret-never-returned" not in row.encrypted_secrets


def test_diagnostic_is_queued_and_does_not_call_network_in_api(logged, factory):
    result = logged.post(BASE + "/diagnostics/ami")
    assert result.status_code == 202
    with factory() as db:
        assert db.get(Outbox, UUID(result.json()["job_id"])).status == "pending"
    Worker(factory).diagnostic_job()
    assert (
        logged.get(BASE + "/logs/runtime").json()["items"][0]["code"]
        == "ami_not_configured_for_tenant"
    )


def test_recording_validation_and_tenant_access(logged, prepared, factory, monkeypatch, tmp_path):
    monkeypatch.setenv("ATS_RECORDINGS_DIR", str(tmp_path))
    settings.cache_clear()
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    aid = logged.get(BASE + "/calls").json()["items"][0]["id"]
    assert (
        logged.post(
            BASE + f"/calls/{aid}/recordings", files={"file": ("x.wav", b"<html>bad</html>")}
        ).status_code
        == 422
    )
    result = logged.post(
        BASE + f"/calls/{aid}/recordings",
        files={"file": ("../../unsafe.wav", b"RIFF\x00\x00\x00\x00WAVEtest")},
    )
    assert result.status_code == 201, result.text
    response = logged.get(BASE + f"/recordings/{result.json()['id']}")
    assert response.content.startswith(b"RIFF") and response.headers["content-type"] == "audio/wav"


def test_expired_crm_creation_is_not_repeated(factory, prepared):
    with factory.begin() as db:
        c = db.scalar(select(Contact))
        c.crm_contact_id = None
        c.crm_sync_state = "creating"
        c.crm_sync_until = now() - timedelta(seconds=1)
        cid, tid = c.id, c.tenant_id
    from ats.adapters import DeliveryUnknown

    with pytest.raises(DeliveryUnknown):
        Worker(factory).resolve_crm_contact(tid, cid, "Name", "+79990000101", None)


def test_complaint_blocks_contact_and_quarantines_caller(logged, prepared, factory):
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    number = logged.post(BASE + "/numbers", json={"phone": "9690229926", "verified": True}).json()
    assert (
        logged.put(
            BASE + "/settings/providers", json={"auto_quarantine_on_complaints": 1}
        ).status_code
        == 200
    )
    with factory.begin() as db:
        db.scalar(select(Attempt)).caller_number = "9690229926"
    response = logged.post(
        BASE + f"/contacts/{prepared[1]}/complaint", json={"outcome": "Попросил больше не звонить"}
    )
    assert response.status_code == 200
    with factory() as db:
        assert db.get(CallerNumber, UUID(number["id"])).quarantined is True
        assert db.scalar(select(Contact)).consent is False
        assert db.scalar(select(BlacklistEntry)) is not None
