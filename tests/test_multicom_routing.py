from uuid import uuid4

from ats.config import settings
from ats.connections import settings_for_tenant
from ats.models import ConnectionProfile
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
