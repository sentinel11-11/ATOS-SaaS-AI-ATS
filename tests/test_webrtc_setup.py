import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
from test_mobile_callback import callback as callback_fixture
from test_widget_protocol import widget as widget_fixture

callback, widget = callback_fixture, widget_fixture


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).parents[1] / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_webrtc_provision_is_idempotent_and_keeps_credentials_private(factory, widget):
    from sqlalchemy import text

    from ats.models import Operator, OperatorDevice
    from ats.webrtc import SESSION_SLOTS, password_for, save_device

    provision = script("provision-webrtc-devices.py").provision
    endpoint = "ats-web-0123456789abcdef"
    with factory.begin() as db:
        save_device(db, db.get(Operator, widget["operator_id"]), endpoint, endpoint, "p" * 48)
    env = {**os.environ, "ATS_DATABASE_URL": os.environ["ATS_TEST_DATABASE_URL"]}
    # A 5.3 device survives the real schema round trip with its original secret.
    try:
        subprocess.run([".venv/bin/alembic", "downgrade", "0014"], env=env, check=True, capture_output=True)
    finally:
        subprocess.run([".venv/bin/alembic", "upgrade", "head"], env=env, check=True, capture_output=True)
    with factory.begin() as db:
        first = provision(db, widget["tenant_id"])
        assert first[0] == {"endpoint": endpoint, "username": endpoint, "password": "p" * 48}
    with factory.begin() as db:
        second = provision(db, widget["tenant_id"])
        assert first == second and len(first) == SESSION_SLOTS
        assert len({row["endpoint"] for row in first}) == SESSION_SLOTS
        device = db.query(OperatorDevice).filter_by(slot=0).one()
        assert device.encrypted_secret != first[0]["password"]
        assert password_for(device) == first[0]["password"]
    rejected = subprocess.run([".venv/bin/alembic", "downgrade", "0014"], env=env, capture_output=True, text=True)
    assert rejected.returncode != 0 and "Parallel browser devices exist" in rejected.stderr
    with factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0015"
        assert db.query(OperatorDevice).count() == SESSION_SLOTS


def test_webrtc_configuration_cannot_dial_pstn_and_preserves_env():
    installer = script("install-widget-webrtc.py")
    endpoint = "ats-web-0123456789abcdef"
    rendered = installer.render_devices([{"endpoint": endpoint, "username": endpoint, "password": "x" * 48}], "ats.example.invalid")
    assert "context=ats-beta-webrtc-deny" in rendered
    assert "direct_media=no" in rendered and "rtp_timeout=15" in rendered
    assert "from_domain=ats.example.invalid" in rendered
    original = 'ATS_AMI_SECRET="literal$test#secret"\nATS_WEBRTC_ENABLED="false"\n'
    result = installer.environment(original, "ats.example.invalid")
    assert result.startswith('ATS_AMI_SECRET="literal$test#secret"\n')
    assert result.count("ATS_WEBRTC_ENABLED=") == 1
    include = installer.with_include("[existing]\n", "private.conf")
    assert installer.with_include(include, "private.conf") == include
    with pytest.raises(ValueError):
        installer.render_devices([{"endpoint": endpoint, "username": endpoint, "password": "bad\ninjection"}], "localhost")
