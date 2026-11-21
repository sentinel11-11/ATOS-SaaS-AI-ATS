from sqlalchemy import select

from ats.config import settings
from ats.models import Attempt, Campaign, Outbox
from ats.workers import Worker


def test_imported_multicom_number_requires_explicit_verification(logged):
    imported = logged.post(
        "/api/v1/workspace/numbers/import-multicom",
        json={"numbers": ["+79690229926"]},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 1

    numbers = logged.get("/api/v2/numbers").json()["numbers"]
    assert len(numbers) == 1
    number = numbers[0]
    assert number["provider"] == "multicom_ami"
    assert number["active"] is False
    assert number["verified"] is False

    enable = logged.post("/api/v2/numbers/save", json={"id": number["id"], "active": True})
    assert enable.status_code == 409, enable.text
    assert enable.json()["error"] == "caller_number_not_verified"

    verify = logged.post(
        "/api/v2/numbers/save",
        json={"id": number["id"], "verified": True},
    )
    assert verify.status_code == 200, verify.text
    number = logged.get("/api/v2/numbers").json()["numbers"][0]
    assert number["verified"] is True
    assert number["active"] is False

    enable = logged.post("/api/v2/numbers/save", json={"id": number["id"], "active": True})
    assert enable.status_code == 200, enable.text
    number = logged.get("/api/v2/numbers").json()["numbers"][0]
    assert number["active"] is True
    assert number["verified"] is True

    revoke = logged.post("/api/v2/numbers/save", json={"id": number["id"], "verified": False, "active": False})
    assert revoke.status_code == 200, revoke.text
    assert logged.get("/api/v2/numbers").json()["numbers"][0]["verified"] is False

def test_new_multicom_number_starts_disabled_and_unverified(logged):
    response = logged.post("/api/v2/numbers/save", json={"number": "+79690229926"})
    assert response.status_code == 200, response.text
    number = logged.get("/api/v2/numbers").json()["numbers"][0]
    assert number["provider"] == "multicom_ami"
    assert number["active"] is False and number["verified"] is False


def test_importing_fifteen_multicom_numbers_preserves_verification_on_repeat(logged):
    numbers = [f"9990000{i:03d}" for i in range(15)]
    path = "/api/v1/workspace/numbers/import-multicom"
    response = logged.post(path, json={"numbers": numbers})
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 15
    rows = logged.get("/api/v2/numbers").json()["numbers"]
    assert all(not row["active"] and not row["verified"] for row in rows)
    enabled_id = rows[0]["id"]
    response = logged.post(
        "/api/v2/numbers/save", json={"id": enabled_id, "verified": True, "active": True}
    )
    assert response.status_code == 200, response.text
    response = logged.post(path, json={"numbers": numbers[::-1]})
    assert response.status_code == 200 and response.json()["created"] == 0
    rows = logged.get("/api/v2/numbers").json()["numbers"]
    assert len(rows) == 15
    assert all(row["verified"] == row["active"] == (row["id"] == enabled_id) for row in rows)


def test_default_campaign_uses_multicom_with_zero_retries(logged, prepared):
    response = logged.post(
        "/api/v1/campaigns",
        headers={"Idempotency-Key": "multicom-default-route"},
        json={"name": "Multicom default", "contact_ids": [prepared[1]]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["route"] == "multicom_ami"
    assert response.json()["retry_max"] == 0


def test_workspace_number_api_keeps_multicom_verification_required(logged):
    path = "/api/v1/workspace/numbers"
    response = logged.post(path, json={"phone": "9690229926", "enabled": False})
    assert response.status_code == 201, response.text
    number = response.json()
    assert number["provider"] == "multicom_ami" and number["kind"] == "sip"
    enable = logged.put(path + "/" + number["id"], json={"phone": number["phone"]})
    assert enable.status_code == 409
    assert enable.json()["error"] == "caller_number_not_verified"


def test_ui_campaign_default_route_and_zero_retries(logged, factory):
    assert logged.get("/api/v2/settings").json()["settings"]["provider"] == "multicom_ami"
    response = logged.post(
        "/api/v2/campaigns/save",
        json={"name": "Multicom message", "flow": "message", "max_channels": 1, "retry_max": 0},
    )
    assert response.status_code == 200, response.text
    with factory() as db:
        campaign = db.scalar(select(Campaign))
        assert campaign.route == "multicom_ami" and campaign.template_id is None
        assert campaign.max_channels == 1 and campaign.retry_max == 0


def test_missing_multicom_endpoint_blocks_originate(
    prepared, provider_config, factory, monkeypatch
):
    monkeypatch.setenv("ATS_AMI_CLI_ENDPOINT_PREFIX", "mcm-cli-")
    settings.cache_clear()
    provider_config.endpoints.clear()
    worker = Worker(factory)
    worker.schedule()
    assert worker.telephony()
    with factory() as db:
        attempt = db.scalar(select(Attempt))
        assert attempt.status == "failed" and attempt.reason == "ami_endpoint_unavailable"
        assert db.scalar(select(Outbox).where(Outbox.kind == "telephony")).status == "done"
    assert provider_config.endpoint_checks == ["mcm-cli-9990000101"]
    assert provider_config.originates == []


def test_bad_ami_secret_blocks_multicom_originate(
    prepared, provider_config, factory, monkeypatch
):
    monkeypatch.setenv("ATS_AMI_SECRET", "incorrect-fixture-secret")
    settings.cache_clear()
    worker = Worker(factory)
    worker.schedule()
    assert worker.telephony()
    with factory() as db:
        attempt = db.scalar(select(Attempt))
        assert attempt.status == "failed" and attempt.reason == "ami_action_rejected"
    assert provider_config.endpoint_checks == []
    assert provider_config.originates == []
