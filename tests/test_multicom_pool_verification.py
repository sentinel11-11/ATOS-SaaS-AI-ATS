import pytest
from sqlalchemy import select

from ats.config import settings
from ats.models import Attempt, CallerNumber, Campaign, Outbox
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
    prepared, provider_config, factory, monkeypatch, logged
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
        attempt_id = str(attempt.id)
    timeline = logged.get(f"/api/v2/calls/{attempt_id}/timeline").json()
    assert timeline["call"]["detail"] == "ami_endpoint_unavailable"
    failure = next(event for event in timeline["events"] if event["event_type"] == "failed")
    assert failure["detail"] == "ami_endpoint_unavailable"
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


def test_ami_connection_succeeds_but_unverified_active_number_blocks_campaign(
    logged, prepared, provider_config, factory, tenants, monkeypatch
):
    monkeypatch.setenv("ATS_AMI_CALLER_ID", "")
    monkeypatch.setenv("ATS_AMI_CLI_ENDPOINT_PREFIX", "mcm-cli-")
    settings.cache_clear()
    assert logged.post(f"/api/v2/campaigns/{prepared[0]}/pause").status_code == 200
    with factory.begin() as db:
        db.add(CallerNumber(
            tenant_id=tenants[0], phone="9990000101", provider="multicom_ami",
            enabled=True, verified=False,
        ))
    result = logged.post("/api/v2/multicom/check")
    assert result.status_code == 202
    Worker(factory).diagnostic_job()
    diagnostic = logged.get("/api/v2/jobs/" + result.json()["job_id"]).json()
    assert diagnostic["connected"] is True
    response = logged.post(f"/api/v2/campaigns/{prepared[0]}/start")
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "caller_number_not_verified"
    assert "Подтвердите" in response.json()["detail"]
    assert diagnostic["report"]["route_ready"] is False
    assert diagnostic["report"]["blocked_reason"] == "caller_number_not_verified"
    assert provider_config.originates == []
    with factory() as db:
        number_id = str(db.scalar(select(CallerNumber.id)))
        assert db.scalar(select(Campaign)).status == "paused"
    confirm = logged.post("/api/v2/numbers/save", json={"id": number_id, "verified": True})
    assert confirm.status_code == 200, confirm.text
    response = logged.post(f"/api/v2/campaigns/{prepared[0]}/start")
    assert response.status_code == 200, response.text
    worker = Worker(factory)
    worker.schedule()
    assert worker.telephony()
    assert len(provider_config.originates) == 1
    assert provider_config.endpoint_checks == ["mcm-cli-9990000101"]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_operator_campaign_reports_media_requirement_before_running(
    logged, prepared, factory, version
):
    with factory.begin() as db:
        campaign = db.scalar(select(Campaign))
        campaign.mode, campaign.status = "operator", "paused"
    if version == "v1":
        response = logged.post(
            f"/api/v1/campaigns/{prepared[0]}/state", json={"status": "running"}
        )
    else:
        response = logged.post(f"/api/v2/campaigns/{prepared[0]}/start")
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "media_not_configured"
    assert "Информатор" in response.json()["detail"]
    with factory() as db:
        assert db.scalar(select(Campaign)).status == "paused"
        assert db.scalar(select(Attempt)) is None


@pytest.mark.parametrize(
    "setting,expected",
    [("ATS_AMI_SECRET", "ami_secret_required"), ("ATS_AMI_CALLER_ID", "caller_pool_empty")],
)
def test_campaign_reports_missing_ami_secret_and_caller_pool_separately(
    logged, prepared, provider_config, monkeypatch, setting, expected
):
    monkeypatch.setenv(setting, "")
    settings.cache_clear()
    response = logged.post(f"/api/v2/campaigns/{prepared[0]}/start")
    assert response.status_code == 409, response.text
    assert response.json()["error"] == expected
    assert response.json()["detail"]
    assert "fixture-secret" not in response.text
    assert provider_config.originates == []


def test_ami_hangup_cause_is_preserved_in_timeline_without_raw_payload(
    prepared, factory, logged
):
    worker = Worker(factory)
    worker.schedule()
    with factory.begin() as db:
        attempt = db.scalar(select(Attempt))
        attempt.status = "accepted"
        attempt.external_id = str(attempt.id)
        attempt.channel = "Local/fixture-channel;1"
        attempt_id = str(attempt.id)
    worker.ami_event({
        "Event": "Hangup", "Channel": "Local/fixture-channel;1",
        "Uniqueid": "fixture-unique-id", "Cause": "21", "PrivateToken": "do-not-echo",
    })
    worker.ami_event({
        "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id,
        "State": "failed", "DialStatus": "CHANUNAVAIL", "PrivateToken": "do-not-echo",
    })
    worker.inbox()
    response = logged.get(f"/api/v2/calls/{attempt_id}/timeline")
    assert response.status_code == 200, response.text
    timeline = response.json()
    assert timeline["call"]["status"] == "failed"
    assert timeline["call"]["detail"] != "ami_hangup_fallback"
    hangup = next(event for event in timeline["events"] if event.get("hangup_cause") == 21)
    assert hangup["payload"] == {}
    assert any(event.get("dial_status") == "CHANUNAVAIL" for event in timeline["events"])
    assert "do-not-echo" not in response.text
