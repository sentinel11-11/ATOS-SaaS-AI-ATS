import importlib.util
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
    from ats.models import OperatorDevice
    from ats.webrtc import password_for

    provision = script("provision-webrtc-devices.py").provision
    with factory.begin() as db:
        first = provision(db, widget["tenant_id"])
    with factory.begin() as db:
        second = provision(db, widget["tenant_id"])
        assert first == second and len(first) == 1
        device = db.query(OperatorDevice).one()
        assert device.encrypted_secret != first[0]["password"]
        assert password_for(device) == first[0]["password"]


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
