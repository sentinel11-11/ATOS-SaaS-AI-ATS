from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ats.config import settings
from ats.models import AmoSyncItem, AmoSyncJob, Attempt, Audit, Contact, Outbox
from ats.workers import Worker

BASE = "/api/v1/workspace"


@pytest.mark.parametrize("consent", [True, False])
def test_readding_hidden_contact_restores_visibility_and_preserves_restrictions(
    logged, prepared, factory, consent
):
    Worker(factory).schedule()
    contact_id = UUID(prepared[1])
    with factory.begin() as db:
        row = db.get(Contact, contact_id)
        row.archived, row.blocked, row.consent = True, True, consent
        source = row.consent_source
        attempt_id = db.scalar(select(Attempt.id))
    response = logged.post("/api/v2/contacts/save", json={
        "phone": "8 (999) 000-01-01", "name": "Updated", "consent": True,
        "blacklisted": False, "consent_source": "New form default",
    })
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(contact_id)
    assert response.json()["result"] == "restored"
    assert response.json()["updated_existing"] == str(contact_id)
    assert logged.get("/api/v2/contacts", params={"q": "89990000101"}).json()["total"] == 1
    history = logged.get(BASE + f"/contacts/{contact_id}/history").json()
    assert history["calls"][0]["id"] == str(attempt_id)
    with factory() as db:
        row = db.get(Contact, contact_id)
        assert not row.archived and row.blocked
        assert row.consent == consent and row.consent_source == source
        assert db.scalar(select(func.count()).select_from(Contact)) == 1
        assert db.scalar(select(Attempt.phone)) == "+79990000101"
        assert db.scalar(select(Audit).where(Audit.action == "contact.restored")).entity_id == contact_id


@pytest.mark.parametrize("query", [
    "79855772554", "+79855772554", "+7 (985) 577-25-54", "8 985 577 25 54", "9855772554",
])
def test_contact_search_normalizes_phone_and_keeps_contacts_without_groups(logged, query):
    saved = logged.post("/api/v2/contacts/save", json={
        "name": "Phone fixture", "phone": "+79855772554", "consent": True,
    }).json()
    for path, key in [("/api/v2/contacts", "contacts"), (BASE + "/contacts", "items")]:
        result = logged.get(path, params={"q": query}).json()
        assert result["total"] == 1
        assert result[key][0]["id"] == saved["id"]
        assert result[key][0]["group"] == ""


def test_concurrent_manual_adds_keep_one_phone_record(logged, factory):
    def add_contact(_):
        return logged.post("/api/v2/contacts/save", json={
            "name": "Concurrent fixture", "phone": "79855772554", "consent": True,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(add_contact, range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert {r.json()["result"] for r in responses} == {"created", "updated"}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Contact)) == 1


@pytest.fixture
def sync_crm(factory, tenants, monkeypatch):
    monkeypatch.setenv("ATS_CRM_ENABLED", "true")
    monkeypatch.setenv("ATS_AMO_TENANT_ID", str(tenants[0]))
    settings.cache_clear()

    class CRM:
        rows = []

        def contacts(self, **kwargs):
            return self.rows

        def lead_contacts(self, **kwargs):
            return self.rows

        def pipelines(self):
            return []

        def users(self):
            return []

    crm = CRM()
    monkeypatch.setattr("ats.api_amo._crm", lambda db, tenant_id: crm)
    worker = Worker(factory)
    worker.crm_drivers[str(tenants[0])] = crm
    return crm, worker


@pytest.mark.parametrize("deleted_response", [True, False])
def test_crm_deletions_block_calls_and_keep_local_contacts_visible(
    logged, factory, tenants, sync_crm, deleted_response
):
    crm, worker = sync_crm
    saved = logged.post("/api/v2/contacts/save", json={
        "name": "Retained fixture", "phone": "+79855772554", "consent": True,
        "crm_contact_id": 123,
    }).json()
    if deleted_response:
        crm.rows = [{"crm_contact_id": 123, "archived": True}]
    for _ in range(2):
        response = logged.post(BASE + "/amo/sync/run")
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        with factory.begin() as db:
            job = db.scalar(select(Outbox).where(Outbox.dedupe_key == f"amo-sync:{job_id}"))
            job.payload = {**job.payload, "payload": {"contacts": {"delete": [{"id": 123}]}}}
        assert worker.crm_sync_job()
        result = logged.get(BASE + f"/amo/sync/{job_id}").json()
        assert result["status"] == "completed" and result["counts"]["archived"] == 0
        assert result["counts"]["blocked"] == 1
    with factory() as db:
        row = db.get(Contact, UUID(saved["id"]))
        assert not row.archived and row.blocked and not row.consent
        assert db.scalar(select(func.count()).select_from(Audit).where(
            Audit.action == "contact.crm_deleted", Audit.entity_id == row.id,
        )) == 1
    assert logged.get("/api/v2/contacts").json()["total"] == 1


def test_old_crm_archive_preview_cannot_hide_a_contact(logged, factory, tenants):
    saved = logged.post("/api/v2/contacts/save", json={
        "name": "Retained fixture", "phone": "+79855772554", "consent": True,
        "crm_contact_id": 123,
    }).json()
    with factory.begin() as db:
        job = AmoSyncJob(tenant_id=tenants[0], status="preview", mode="manual")
        db.add(job)
        db.flush()
        item = AmoSyncItem(tenant_id=tenants[0], job_id=job.id, action="archive",
                           crm_contact_id=123, contact_id=UUID(saved["id"]))
        db.add(item)
        db.flush()
        job_id, item_id = job.id, item.id
    result = logged.post(BASE + f"/amo/sync/{job_id}/confirm", json={
        "item_ids": [str(item_id)], "consent_source": "Manual import",
    })
    assert result.status_code == 200 and result.json()["applied"] == 1
    assert logged.get("/api/v2/contacts").json()["total"] == 1
    with factory() as db:
        row = db.get(Contact, UUID(saved["id"]))
        assert row.blocked and not row.consent and not row.archived


def test_crm_preview_marks_deleted_contacts_unselected(logged, sync_crm):
    crm, _ = sync_crm
    crm.rows = [{"crm_contact_id": 123, "archived": True}]
    result = logged.post(BASE + "/amo/sync/preview", json={"contact_ids": [123]})
    assert result.status_code == 200, result.text
    item = result.json()["items"][0]
    assert item["action"] == "unchanged" and not item["selected"]
    assert item["error"] == "crm_contact_deleted"


def test_confirming_crm_deletion_blocks_all_prior_phone_mappings(logged, factory, sync_crm):
    crm, _ = sync_crm
    for phone in ["+79855772554", "+79165833868"]:
        logged.post("/api/v2/contacts/save", json={
            "name": "Historical fixture", "phone": phone, "crm_contact_id": 123, "consent": True,
        })
    crm.rows = [{"crm_contact_id": 123, "archived": True}]
    preview = logged.post(BASE + "/amo/sync/preview", json={"contact_ids": [123]}).json()
    item = preview["items"][0]
    assert item["action"] == "block" and item["selected"]
    result = logged.post(BASE + f"/amo/sync/{preview['job_id']}/confirm", json={
        "item_ids": [item["id"]], "consent_source": "Reviewed CRM deletion",
    })
    assert result.status_code == 200 and result.json()["applied"] == 1
    with factory() as db:
        assert all(row.blocked and not row.consent and not row.archived
                   for row in db.scalars(select(Contact)))


@pytest.mark.parametrize("body_kind", ["form", "json"])
def test_authenticated_amo_webhook_excludes_deleted_contact_without_browser_session(
    logged, factory, tenants, sync_crm, monkeypatch, body_kind
):
    _, worker = sync_crm
    saved = logged.post("/api/v2/contacts/save", json={
        "name": "Webhook fixture", "phone": "+79855772554", "crm_contact_id": 123,
        "consent": True,
    }).json()
    monkeypatch.setenv("ATS_AMO_WEBHOOK_TOKEN", "synthetic-webhook-token")
    settings.cache_clear()
    logged.cookies.clear()
    logged.headers.pop("Origin", None)
    logged.headers.pop("X-CSRF-Token", None)
    url = BASE + f"/amo/webhook/{tenants[0]}"
    body = {"data": {"contacts[delete][0][id]": "123"}} if body_kind == "form" else {
        "json": {"contacts": {"delete": [{"id": 123}]}},
    }
    denied = logged.post(url, **body)
    assert denied.status_code == 401
    response = logged.post(url, **body, headers={"X-Amo-Webhook-Token": "synthetic-webhook-token"})
    assert response.status_code == 200 and response.json()["accepted"]
    duplicate = logged.post(url, **body, headers={"X-Amo-Webhook-Token": "synthetic-webhook-token"})
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    assert worker.crm_sync_job()
    with factory() as db:
        row = db.get(Contact, UUID(saved["id"]))
        assert row.blocked and not row.consent and not row.archived


def test_concurrent_amo_webhooks_enqueue_one_sync_job(logged, factory, tenants, sync_crm, monkeypatch):
    monkeypatch.setenv("ATS_AMO_WEBHOOK_TOKEN", "synthetic-webhook-token")
    settings.cache_clear()

    def send(_):
        return logged.post(BASE + f"/amo/webhook/{tenants[0]}",
                           data={"contacts[delete][0][id]": "123"},
                           headers={"X-Amo-Webhook-Token": "synthetic-webhook-token"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert {response.json()["duplicate"] for response in responses} == {True, False}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AmoSyncJob)) == 1
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "crm_sync")) == 1


def test_crm_import_finds_hidden_phone_and_preserves_its_restrictions(
    logged, factory, sync_crm
):
    crm, worker = sync_crm
    saved = logged.post("/api/v2/contacts/save", json={
        "name": "Hidden fixture", "phone": "+79855772554", "consent": False,
    }).json()
    with factory.begin() as db:
        row = db.get(Contact, UUID(saved["id"]))
        row.archived, row.blocked = True, True
    crm.rows = [{"crm_contact_id": 123, "phone": "+79855772554", "name": "CRM fixture",
                 "email": "", "group": ""}]
    preview = logged.post(BASE + "/amo/sync/preview", json={"contact_ids": [123]})
    assert preview.status_code == 200, preview.text
    item = preview.json()["items"][0]
    assert item["action"] == "update"
    response = logged.post(BASE + f"/amo/sync/{preview.json()['job_id']}/confirm", json={
        "item_ids": [item["id"]], "consent_source": "Import checkbox",
    })
    assert response.status_code == 200 and response.json()["applied"] == 1
    logged.post(BASE + "/amo/sync/run")
    assert worker.crm_sync_job()
    with factory() as db:
        row = db.get(Contact, UUID(saved["id"]))
        assert row.archived and row.blocked and not row.consent
        assert row.crm_contact_id == 123
        assert db.scalar(select(func.count()).select_from(Contact)) == 1


@pytest.mark.parametrize("phone,expected_action", [
    ("+79855772554", "update"), ("+79165833868", "duplicate_crm_id"),
])
def test_crm_sync_uses_the_current_phone_when_old_crm_mappings_remain(
    logged, factory, sync_crm, phone, expected_action
):
    crm, worker = sync_crm
    for stored_phone in ["+79855772554", "+79915218899"]:
        response = logged.post("/api/v2/contacts/save", json={
            "name": "Original fixture", "phone": stored_phone, "consent": True,
            "crm_contact_id": 123,
        })
        assert response.status_code == 200
    crm.rows = [{"crm_contact_id": 123, "phone": phone, "name": "Current CRM fixture",
                 "email": "", "group": ""}]
    preview = logged.post(BASE + "/amo/sync/preview", json={"contact_ids": [123]})
    assert preview.status_code == 200, preview.text
    assert preview.json()["items"][0]["action"] == expected_action
    result = logged.post(BASE + "/amo/sync/run")
    assert worker.crm_sync_job()
    status = logged.get(BASE + f"/amo/sync/{result.json()['job_id']}").json()
    assert status["status"] == "completed"
    with factory() as db:
        original = db.scalar(select(Contact).where(Contact.phone == "+79915218899"))
        assert original.name == "Original fixture"
        assert db.scalar(select(func.count()).select_from(Contact)) == 2
        target = db.scalar(select(Contact).where(Contact.phone == "+79855772554"))
        assert target.name == ("Current CRM fixture" if expected_action == "update" else "Original fixture")
