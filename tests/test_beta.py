from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from ats.adapters import DeliveryUnknown, Rejected, Retryable
from ats.config import settings
from ats.domain import next_state
from ats.models import (
    Attempt,
    Audit,
    Campaign,
    CampaignItem,
    Contact,
    Inbox,
    Outbox,
    Session,
    User,
    now,
)
from ats.schemas import CampaignCreate
from ats.security import Principal
from ats.services import create_campaign, drain_inbox, ingest, schedule_one
from ats.workers import Worker, backoff, claim, complete_job


def test_health_and_auth_required(client):
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/v1/contacts").status_code == 401


def test_http_only_cookie_and_logout(logged):
    cookies = {c.name: c for c in logged.cookies.jar}
    assert cookies["ats_session"]._rest.get("HttpOnly") is None
    assert "HttpOnly" in cookies["ats_session"]._rest
    old = logged.cookies["ats_session"]
    assert logged.post("/api/v1/auth/logout").status_code == 204
    logged.cookies.set("ats_session", old)
    assert logged.get("/api/v1/auth/me").status_code == 401


def test_revocation_is_database_authoritative(logged, factory):
    with factory.begin() as db:
        db.execute(Session.__table__.delete())
    assert logged.get("/api/v1/auth/me").status_code == 401


def test_disabled_user_fails_closed(logged, factory):
    with factory.begin() as db:
        db.scalar(select(User).where(User.login == "admin")).active = False
    assert logged.get("/api/v1/auth/me").status_code == 401


def test_csrf_and_origin(logged):
    logged.headers["X-CSRF-Token"] = "wrong"
    assert logged.post("/api/v1/contacts", json={}).status_code == 403
    logged.headers["Origin"] = "https://evil.invalid"
    assert logged.post("/api/v1/auth/login", json={}).status_code == 403


def test_validation_does_not_echo_password(client):
    response = client.post(
        "/api/v1/auth/login", json={"tenant_id": "invalid", "password": "sensitive-value"}
    )
    assert response.status_code == 422
    assert "sensitive-value" not in response.text


@pytest.mark.parametrize(
    "changes,error_type,location",
    [
        ({"phone": "89990000101"}, "phone_e164", ["body", "phone"]),
        ({"consent_source": "  "}, "consent_source_required", ["body"]),
    ],
)
def test_contact_validation_explains_rejected_field(logged, changes, error_type, location):
    payload = {
        "name": "Private contact name",
        "phone": "+79990000101",
        "consent": True,
        "consent_source": "Test consent",
        "crm_contact_id": None,
    }
    response = logged.post("/api/v1/contacts", json={**payload, **changes})
    assert response.status_code == 422
    assert {"location": location, "type": error_type} in response.json()["fields"]
    assert "Private contact name" not in response.text
    assert "89990000101" not in response.text
    assert logged.get("/api/v1/contacts").json() == []


def test_contact_can_be_created_without_crm_id(logged):
    response = logged.post(
        "/api/v1/contacts",
        json={
            "name": "Тест",
            "phone": "+7 (999) 000-01-01",
            "consent": True,
            "consent_source": "Собственный номер для тестирования",
            "crm_contact_id": None,
        },
    )
    assert response.status_code == 201
    assert response.json()["phone"] == "+79990000101"
    assert response.json()["crm_contact_id"] is None


def test_body_limit(client):
    assert client.post("/api/v1/auth/login", content=b"x" * (256 * 1024 + 1)).status_code == 413


def test_login_rate_limit_shared(client, tenants):
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"tenant_id": str(tenants[0]), "login": "no-user", "password": "bad"},
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"tenant_id": str(tenants[0]), "login": "no-user", "password": "bad"},
        ).status_code
        == 429
    )


def test_idempotent_campaign(logged, prepared):
    campaign_id, _, data = prepared
    response = logged.post(
        "/api/v1/campaigns", json=data, headers={"Idempotency-Key": "fixture-key-01"}
    )
    assert response.status_code == 200 and response.json()["id"] == campaign_id
    response = logged.post(
        "/api/v1/campaigns",
        json={**data, "name": "Other"},
        headers={"Idempotency-Key": "fixture-key-01"},
    )
    assert response.status_code == 409


def test_tenant_cannot_access_foreign_contacts(logged, prepared, factory, tenants):
    with factory.begin() as db:
        foreign = Contact(
            id=uuid4(),
            tenant_id=tenants[1],
            name="Other",
            phone="+79990000202",
            consent=True,
            consent_source="test",
        )
        db.add(foreign)
        foreign_id = str(foreign.id)
    response = logged.post(
        "/api/v1/campaigns",
        json={"name": "Attack", "contact_ids": [foreign_id]},
        headers={"Idempotency-Key": "isolation-key"},
    )
    assert response.status_code == 404
    assert foreign_id not in logged.get("/api/v1/contacts").text
    assert (
        logged.patch(
            f"/api/v1/contacts/{foreign_id}",
            json={"consent": False, "consent_source": "test", "blocked": True},
        ).status_code
        == 404
    )


def test_database_rejects_cross_tenant_fk(factory, tenants, prepared):
    from sqlalchemy.exc import IntegrityError

    campaign_id, contact_id, _ = prepared
    with pytest.raises(IntegrityError), factory.begin() as db:
        db.add(
            CampaignItem(
                tenant_id=tenants[1], campaign_id=UUID(campaign_id), contact_id=UUID(contact_id)
            )
        )


def test_viewer_cannot_write(logged, factory, tenants):
    with factory.begin() as db:
        db.scalar(select(User).where(User.tenant_id == tenants[0])).role = "viewer"
    assert (
        logged.post("/api/v1/contacts", json={"name": "test", "phone": "+79990000303"}).status_code
        == 403
    )


def test_transaction_rolls_back_campaign_items_and_audit(factory, tenants):
    with factory.begin() as db:
        contact = Contact(
            tenant_id=tenants[0],
            name="A",
            phone="+79990000303",
            consent=True,
            consent_source="test",
        )
        db.add(contact)
        db.flush()
        cid = contact.id
    with pytest.raises(RuntimeError), factory.begin() as db:
        create_campaign(
            db,
            Principal(tenants[0], uuid4(), "admin", "admin"),
            CampaignCreate(name="Rollback", contact_ids=[cid]),
            "rollback-key",
        )
        raise RuntimeError("Injected failure before commit")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Campaign)) == 0
        assert db.scalar(select(func.count()).select_from(CampaignItem)) == 0
        assert db.scalar(select(func.count()).select_from(Audit)) == 0


def test_live_route_is_explicitly_disabled(logged, prepared, factory):
    campaign_id, _, _ = prepared
    with factory.begin() as db:
        db.get(Campaign, UUID(campaign_id)).route = "multicom_ami"
    assert (
        logged.post(
            f"/api/v1/campaigns/{campaign_id}/state", json={"status": "running"}
        ).status_code
        == 409
    )


def test_withdrawn_consent_blocks_dispatch(factory, tenants, prepared, logged):
    _, cid, _ = prepared
    with factory.begin() as db:
        assert schedule_one(db, tenants[0])
    assert (
        logged.patch(
            f"/api/v1/contacts/{cid}",
            json={"consent": False, "consent_source": "withdrawn", "blocked": True},
        ).status_code
        == 200
    )
    worker = Worker(factory)
    assert worker.telephony()
    with factory() as db:
        row = db.scalar(select(Attempt))
        assert row.status == "failed" and row.reason == "consent_withdrawn"


def test_pause_prevents_dispatch(factory, tenants, prepared, logged):
    campaign_id, _, _ = prepared
    with factory.begin() as db:
        schedule_one(db, tenants[0])
    logged.post(f"/api/v1/campaigns/{campaign_id}/state", json={"status": "paused"})
    Worker(factory).telephony()
    with factory() as db:
        assert db.scalar(select(Attempt)).status == "prepared"
        assert db.scalar(select(Outbox)).status == "pending"


def test_dispatch_rechecks_calling_window(factory, tenants, prepared):
    from zoneinfo import ZoneInfo

    from ats.models import Tenant

    worker = Worker(factory)
    worker.schedule()
    with factory.begin() as db:
        tenant = db.get(Tenant, tenants[0])
        hour = now().astimezone(ZoneInfo(tenant.timezone)).hour
        campaign = db.scalar(select(Campaign))
        campaign.window_start = (hour + 1) % 24
        campaign.window_end = campaign.window_start + 1
    worker.telephony()
    with factory() as db:
        assert db.scalar(select(Attempt)).status == "prepared"
        assert db.scalar(select(Outbox)).status == "pending"


def test_concurrent_schedulers_respect_tenant_channels(factory, tenants, prepared):
    campaign_id, _, _ = prepared
    with factory.begin() as db:
        db.get(Campaign, UUID(campaign_id)).max_channels = 2
        for i in range(10):
            c = Contact(
                id=uuid4(),
                tenant_id=tenants[0],
                name="A",
                phone=f"+79990001{i:03d}",
                consent=True,
                consent_source="test",
            )
            db.add(c)
            db.flush()
            db.add(
                CampaignItem(tenant_id=tenants[0], campaign_id=UUID(campaign_id), contact_id=c.id)
            )

    def allocate(_):
        with factory.begin() as db:
            return schedule_one(db, tenants[0])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(allocate, range(24)))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) <= 2
    # SKIP LOCKED may skip every contending allocator; subsequent scheduler ticks fill capacity.
    allocate(None)
    allocate(None)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 2
        assert db.scalar(select(func.count()).select_from(Outbox)) == 2


def test_simulated_workflow_and_crm(factory, tenants, prepared):
    class CRM:
        calls = []

        def send(self, *args):
            self.calls.append(args)

    crm = CRM()
    worker = Worker(factory, crm=crm)
    worker.schedule()
    assert worker.telephony()
    assert worker.crm_job()
    assert len(crm.calls) == 1 and crm.calls[0][1:] == (123, "completed")
    worker.schedule()
    with factory() as db:
        assert db.scalar(select(Campaign)).status == "completed"
        assert all(r.status == "done" for r in db.scalars(select(Outbox)))


def test_inbox_duplicate_and_out_of_order(factory, tenants, prepared):
    Worker(factory).schedule()
    with factory.begin() as db:
        a = db.scalar(select(Attempt))
        a.external_id = "external-test"
        a.status = "accepted"
        for event in ("completed", "completed", "answered"):
            ingest(db, tenants[0], "sim", f"ext:{event}", "external-test", event)
    Worker(factory).inbox()
    with factory() as db:
        assert db.scalar(select(Attempt)).status == "completed"
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm")) == 1
        assert db.scalar(select(func.count()).select_from(Inbox)) == 2


def test_orphan_is_bounded(factory, tenants):
    with factory.begin() as db:
        ingest(db, tenants[0], "sim", "orphan", "missing", "completed")
    with factory.begin() as db:
        assert not drain_inbox(db)
        row = db.scalar(select(Inbox))
        assert row.status == "orphan"
        row.received_at = now() - timedelta(minutes=6)
        row.available_at = now() - timedelta(seconds=1)
    with factory.begin() as db:
        drain_inbox(db)
        assert db.scalar(select(Inbox)).status == "unmatched"


def test_crash_after_dispatch_does_not_redial(factory, prepared):
    worker = Worker(factory)
    worker.schedule()
    with factory.begin() as db:
        job = claim(db, "telephony")
        a = db.scalar(select(Attempt))
        a.status = "dispatching"
        db.get(Outbox, job[0]).lease_until = now() - timedelta(seconds=1)
    assert worker.telephony()
    with factory() as db:
        assert db.scalar(select(Attempt)).status == "unknown"


def test_stale_worker_cannot_ack_new_lease(factory, tenants):
    with factory.begin() as db:
        row = Outbox(
            id=uuid4(), tenant_id=tenants[0], kind="telephony", dedupe_key="fence", payload={}
        )
        db.add(row)
        db.flush()
        rid = row.id
    with factory.begin() as db:
        old = claim(db, "telephony")
        db.get(Outbox, rid).lease_until = now() - timedelta(seconds=1)
    with factory.begin() as db:
        new = claim(db, "telephony")
        assert new[1] != old[1]
    with factory.begin() as db:
        complete_job(db, rid, old[1])
        assert db.get(Outbox, rid).status == "leased"


@pytest.mark.parametrize(
    "failure,expected",
    [
        (Retryable("test", 30), "pending"),
        (Rejected("http_401"), "dead"),
        (DeliveryUnknown(), "unknown"),
    ],
)
def test_crm_failure_policy(factory, prepared, failure, expected):
    class CRM:
        def send(self, *_):
            raise failure

    worker = Worker(factory, crm=CRM())
    worker.schedule()
    worker.telephony()
    worker.crm_job()
    with factory() as db:
        row = db.scalar(select(Outbox).where(Outbox.kind == "crm"))
        assert row.status == expected
        if expected == "pending":
            assert row.available_at > now() + timedelta(seconds=29)


def test_crm_lost_lease_is_not_blindly_retried(factory, prepared):
    worker = Worker(factory)
    worker.schedule()
    worker.telephony()
    with factory.begin() as db:
        job = claim(db, "crm")
        db.get(Outbox, job[0]).lease_until = now() - timedelta(seconds=1)
    with factory.begin() as db:
        assert claim(db, "crm") is None
        assert db.get(Outbox, job[0]).status == "unknown"


def test_terminal_state_is_monotonic():
    assert next_state("completed", "answered") == "completed"
    assert next_state("answered", "accepted") == "answered"
    assert next_state("unknown", "completed") == "completed"


def test_backoff_bounds_and_retry_after():
    assert 8 <= backoff(3) < 9
    assert backoff(3, 90) >= 90


def test_live_config_requires_binding():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings().__class__(
            database_url="unused",
            live_calls_enabled=True,
            telephony_tenant_id=None,
            test_destinations="",
        )


def test_terminal_event_before_timeout_stays_terminal(factory, tenants, prepared, monkeypatch):
    from ats.services import finish_attempt

    monkeypatch.setenv("ATS_LIVE_CALLS_ENABLED", "true")
    monkeypatch.setenv("ATS_TELEPHONY_TENANT_ID", str(tenants[0]))
    monkeypatch.setenv("ATS_TEST_DESTINATIONS", "+79990000101")
    monkeypatch.setenv("ATS_AMI_SECRET", "fixture")
    monkeypatch.setenv("ATS_AMI_CALLER_ID", "9990000101")
    settings.cache_clear()
    with factory.begin() as db:
        db.scalar(select(Campaign)).route = "multicom_ami"

    class Driver:
        def originate(self, attempt_id, phone, caller_id=None, media=False, operator_ext=""):
            with factory.begin() as db:
                finish_attempt(db, db.get(Attempt, attempt_id), "completed")
            raise DeliveryUnknown()

    worker = Worker(factory, drivers={"multicom_ami": Driver()})
    worker.schedule()
    worker.telephony()
    with factory() as db:
        assert db.scalar(select(Attempt)).status == "completed"


def test_storage_failure_denies_session_with_503(client):
    from sqlalchemy.exc import OperationalError

    from ats.api import app
    from ats.db import session

    def broken():
        raise OperationalError("hidden SQL", {"secret": "do-not-echo"}, Exception("down"))
        yield

    app.dependency_overrides[session] = broken
    client.cookies.set("ats_session", "unknown")
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 503
    assert "hidden SQL" not in response.text and "do-not-echo" not in response.text


def test_existing_amocrm_binding_is_supported(monkeypatch):
    monkeypatch.setenv("ATS_AMOCRM_TOKEN", "fixture-existing-binding")
    settings.cache_clear()
    assert settings().amo_access_token.get_secret_value() == "fixture-existing-binding"
