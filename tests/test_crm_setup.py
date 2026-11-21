import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from ats.config import settings
from ats.models import ConnectionProfile
from ats.workers import Worker


def test_settings_screen_can_save_complete_default_provider_configuration(logged):
    providers = logged.get("/api/v2/settings/raw").json()["provider_config"]
    assert providers["multicom"]["account_id"] == ""
    response = logged.post("/api/v2/settings/raw", json={"provider_config": providers})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("value", ["", "   ", None])
def test_unconfigured_amo_account_does_not_block_saving_or_diagnostics(logged, value):
    response = logged.post("/api/v2/settings/raw", json={"provider_config": {"amocrm": {"account_id": value}}})
    assert response.status_code == 200, response.text
    assert logged.post("/api/v2/amocrm/check").status_code == 202


def test_multicom_account_identifier_keeps_text_and_leading_zeroes(logged):
    response = logged.post("/api/v2/settings/raw", json={"provider_config": {"multicom": {"account_id": "account-0000123"}}})
    assert response.status_code == 200, response.text
    assert logged.get("/api/v2/settings/raw").json()["provider_config"]["multicom"]["account_id"] == "account-0000123"


@pytest.mark.parametrize("value", ["invalid", "1.5", -1, 0, True, 1.5])
def test_nonempty_amo_account_id_must_be_a_positive_integer(logged, value):
    response = logged.post("/api/v2/settings/raw", json={"provider_config": {"amocrm": {"account_id": value}}})
    assert response.status_code == 422


@pytest.fixture
def configured_crm(logged, tenants, monkeypatch, factory):
    for name, value in {"ATS_AMO_TENANT_ID": str(tenants[0]), "ATS_CONNECTION_ENCRYPTION_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    response = logged.post("/api/v2/settings/raw", json={"provider_config": {"amocrm": {
        "subdomain": "omkr", "access_token": "synthetic-access-token-for-crm-setup", "account_id": "",
    }}})
    assert response.status_code == 200
    return tenants[0]


def mock_account(monkeypatch, payload, before=None):
    import httpx

    real_client = httpx.Client

    def respond(request):
        assert str(request.url) == "https://omkr.amocrm.ru/api/v4/account"
        assert request.headers["authorization"] == "Bearer synthetic-access-token-for-crm-setup"
        if before:
            before()
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))


def run_check(logged, factory):
    from uuid import UUID

    response = logged.post("/api/v2/amocrm/check")
    assert response.status_code == 202
    job_id = UUID(response.json()["job_id"])
    assert Worker(factory).diagnostic_job()
    return logged.get(f"/api/v2/jobs/{job_id}").json()


def test_check_connection_discovers_and_persists_amo_account(logged, factory, configured_crm, monkeypatch):
    with factory() as db:
        secret = db.scalar(select(ConnectionProfile)).encrypted_secrets
    mock_account(monkeypatch, {"id": 12345678, "subdomain": "omkr"})
    result = run_check(logged, factory)
    assert result["connected"] is True
    assert result["report"]["account_id"] == 12345678
    public = logged.get("/api/v2/settings/raw").json()["provider_config"]
    assert public["amocrm"]["account_id"] == 12345678
    assert public["amocrm"]["access_token"] == "********"
    with factory() as db:
        assert db.scalar(select(ConnectionProfile)).encrypted_secrets == secret


def test_check_does_not_replace_explicit_different_account(logged, factory, configured_crm, monkeypatch):
    logged.post("/api/v2/settings/raw", json={"provider_config": {"amocrm": {"account_id": 98765432}}})
    mock_account(monkeypatch, {"id": 12345678, "subdomain": "omkr"})
    result = run_check(logged, factory)
    assert result["error"] == "crm_account_id_mismatch" and not result["connected"]
    assert logged.get("/api/v2/settings/raw").json()["provider_config"]["amocrm"]["account_id"] == 98765432


def test_check_does_not_overwrite_configuration_changed_during_network_request(logged, factory, configured_crm, monkeypatch):
    def change_profile():
        with factory.begin() as db:
            row = db.scalar(select(ConnectionProfile))
            row.public_values = {**row.public_values, "subdomain": "other"}
            row.revision += 1

    mock_account(monkeypatch, {"id": 12345678, "subdomain": "omkr"}, before=change_profile)
    result = run_check(logged, factory)
    assert result["error"] == "crm_settings_changed" and not result["connected"]
    profile = logged.get("/api/v2/settings/raw").json()["provider_config"]["amocrm"]
    assert profile["subdomain"] == "other" and not profile["account_id"]


@pytest.mark.parametrize("payload,code", [
    ({"id": True}, "crm_account_response_invalid"), ({"id": 0}, "crm_account_response_invalid"),
    ({"id": "12345678"}, "crm_account_response_invalid"),
    ({"id": 12345678, "subdomain": "other"}, "crm_account_domain_mismatch"),
])
def test_check_rejects_invalid_or_wrong_account_response(logged, factory, configured_crm, monkeypatch, payload, code):
    mock_account(monkeypatch, payload)
    result = run_check(logged, factory)
    assert result["error"] == code and not result["connected"]


def test_server_bootstrap_can_discover_id_before_amo_tenant_binding(factory, configured_crm, monkeypatch, tmp_path, capsys):
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    from ats.connections import save_provider_config
    from ats.models import Operator, User

    monkeypatch.setenv("ATS_AMO_TENANT_ID", "")
    settings.cache_clear()
    with factory.begin() as db:
        user = User(tenant_id=configured_crm, login="setup-manager", role="operator", password_hash="fixture-only")
        db.add(user)
        db.flush()
        db.add(Operator(tenant_id=configured_crm, user_id=user.id, extension="704"))
        save_provider_config(db, configured_crm, {"amocrm": {
            "client_id": "76b9a5b3-5c3f-4e15-8b6a-bc96d93fc316",
            "client_secret": "synthetic-client-secret-for-crm-setup", "operator_user_map": {"704": 1234567},
        }})
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    monkeypatch.setattr("ats.db.session_factory", lambda: factory)
    mock_account(monkeypatch, {"id": 12345678, "subdomain": "omkr"})
    check = runpy.run_path(str(Path(__file__).parents[1] / "scripts/configure-amo-widget.py"))["check_profile"]
    check(SimpleNamespace(env_file=env_file, check_profile=str(configured_crm), subdomain="omkr"))
    with factory() as db:
        assert db.scalar(select(ConnectionProfile)).public_values["account_id"] == 12345678
    output = capsys.readouterr().out
    assert "12345678" in output
    assert "synthetic-access-token" not in output and "synthetic-client-secret" not in output
