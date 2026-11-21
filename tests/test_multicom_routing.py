from uuid import uuid4

import pytest
from sqlalchemy import select

from ats.config import settings
from ats.connections import settings_for_tenant
from ats.models import Attempt, ConnectionProfile
from ats.workers import Worker


def test_saved_multicom_prefix_updates_cached_driver_without_disconnect(
    factory, prepared, tenants, provider_config
):
    worker = Worker(factory)
    driver = worker.driver("multicom_ami", tenants[0], settings())
    driver.originate(uuid4(), "+79990000101", caller_id="9990000101")
    connection = driver.sock
    assert "__ATS_TRUNK=mcm" in provider_config.originates[-1]["Variable"]

    with factory.begin() as db:
        db.add(ConnectionProfile(
            tenant_id=tenants[0], provider="ami",
            public_values={"cli_endpoint_prefix": "mcm-cli-"},
        ))
    with factory() as db:
        config = settings_for_tenant(db, tenants[0])
    refreshed = worker.driver("multicom_ami", tenants[0], config)
    refreshed.originate(uuid4(), "+79990000101", caller_id="9990000101")

    assert refreshed is driver and refreshed.sock is connection
    assert "__ATS_TRUNK=mcm-cli-9990000101" in provider_config.originates[-1]["Variable"]
    assert provider_config.endpoint_checks == ["mcm-cli-9990000101"]


def test_ami_connection_is_reopened_when_connection_settings_change(
    factory, prepared, tenants
):
    worker = Worker(factory)
    config = settings()
    driver = worker.driver("multicom_ami", tenants[0], config)
    driver.connect()
    connection = driver.sock
    changed = config.model_copy(update={"ami_user": "updated-fixture-user"})
    refreshed = worker.driver("multicom_ami", tenants[0], changed)
    assert refreshed.config.ami_user == "updated-fixture-user"
    assert refreshed.sock is None
    refreshed.reader.join(timeout=2)
    assert not refreshed.reader.is_alive()
    assert connection.fileno() == -1
    refreshed.connect()
    assert refreshed.sock is not connection
    assert refreshed.sock.fileno() >= 0


def test_saved_echo_context_reaches_real_ami_originate(
    logged, prepared, factory, tenants, provider_config
):
    response = logged.post("/api/v2/settings/raw", json={
        "provider_config": {"ami": {"answer_context": "ats-beta-echo"}},
    })
    assert response.status_code == 200, response.text
    with factory() as db:
        config = settings_for_tenant(db, tenants[0])
    assert config.ami_answer_context == "ats-beta-echo"
    worker = Worker(factory)
    worker.schedule()
    assert worker.telephony()
    assert provider_config.originates[-1]["Context"] == ["ats-beta-echo"]
    with factory() as db:
        assert db.scalar(select(Attempt)).status == "accepted"
    driver = worker.driver("multicom_ami", tenants[0], config)
    assert driver.config.ami_answer_context == "ats-beta-echo"


@pytest.mark.parametrize("flow,template", [("operator", False), ("message", True)])
def test_echo_context_does_not_replace_normal_operator_or_template_scenario(
    logged, prepared, factory, tenants, flow, template
):
    from ats.models import Campaign, Template

    with factory.begin() as db:
        db.add(ConnectionProfile(
            tenant_id=tenants[0], provider="ami", public_values={"answer_context": "ats-beta-echo"},
        ))
        campaign = db.get(Campaign, prepared[0])
        campaign.status = "paused"
        campaign.mode = flow
        if template:
            row = Template(tenant_id=tenants[0], name="Message", text="Test")
            db.add(row)
            db.flush()
            campaign.template_id = row.id
    response = logged.post(f"/api/v2/campaigns/{prepared[0]}/start")
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "echo_test_requires_plain_message"
