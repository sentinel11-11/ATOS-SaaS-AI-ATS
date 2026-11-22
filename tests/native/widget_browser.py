"""Opt-in real Chromium -> WSS -> Asterisk -> loopback SIP/RTP bridge.

PYTHONPATH=tests:tests/native pytest tests/native/widget_browser.py
Requires Docker, Chromium and Node Playwright. Only the amoCRM SDK/data are fixtures;
the shipped widget, API, JWT authentication, SQL, workers, SIP and media are real.
"""

import json
import runpy
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
import uvicorn
from callback_smoke import native_asterisk as asterisk_fixture
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from test_mobile_callback import callback as callback_fixture
from test_widget_protocol import SECRET
from test_widget_protocol import widget as widget_fixture

from ats.config import settings
from ats.connections import save_provider_config
from ats.models import Attempt, Operator, Outbox, User, WidgetCall
from ats.workers import Worker

native_asterisk, callback, widget_base = asterisk_fixture, callback_fixture, widget_fixture
TURN_IMAGE = "coturn/coturn@sha256:71c3c990283385567f11794ee692e3a47b66fd9b0bb39e42afbe776e331dd888"


@pytest.fixture
def widget(widget_base, factory, request):
    widget = widget_base
    widget["cross"] = request.param
    widget["recipient_id"] = widget["operator_id"]
    widget["recipient_amo_id"] = 7654321
    if request.param:
        with factory.begin() as db:
            user = User(tenant_id=widget["tenant_id"], login="native-recipient", role="operator", password_hash="fixture")
            db.add(user)
            db.flush()
            operator = Operator(tenant_id=widget["tenant_id"], user_id=user.id, name="Native responsible",
                                extension="705", status="free")
            db.add(operator)
            db.flush()
            widget["recipient_id"], widget["recipient_amo_id"] = operator.id, 7654322
            save_provider_config(db, widget["tenant_id"], {"amocrm": {
                "operator_user_map": {"704": 7654321, "705": 7654322},
            }})
        widget["raw_card"]["responsible_user_id"] = 7654322
    return widget


@pytest.fixture
def turn_server(native_asterisk, tmp_path):
    # Also proves media through restrictive corporate networks: only client TCP is used.
    name = "atos-native-turn-" + uuid4().hex[:10]
    address = subprocess.check_output(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", native_asterisk], text=True).strip()
    render = runpy.run_path(str(Path(__file__).parents[2] / "scripts/install-widget-turn.py"))["turn_config"]
    # In production both services share a VPS; the test uses separate isolated containers.
    config = render(address, "fixture-turn-secret-for-native-only").replace(f"relay-ip={address}\n", "")
    private = tmp_path / "turnserver.conf"
    private.write_text(config)
    private.chmod(0o644)  # Synthetic fixture secret, readable by the image's nobody user.
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::3478/tcp",
                    "--mount", f"type=bind,src={private},dst=/input/turnserver.conf,readonly", TURN_IMAGE,
                    "-c", "/input/turnserver.conf", "--log-file=stdout", "-v"], check=True, capture_output=True)
    try:
        port = int(subprocess.check_output(["docker", "port", name, "3478/tcp"], text=True).strip().rsplit(":", 1)[1])
        yield port
    finally:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        Path(".runtime/widget-turn.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def certificate(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    (folder / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


@pytest.mark.parametrize("native_asterisk", ["browser"], indirect=True)
@pytest.mark.parametrize("widget", [False, True], indirect=True, ids=["self", "other-responsible"])
def test_shipped_widget_real_webrtc_bridge_and_hangup(factory, widget, native_asterisk, turn_server, tmp_path, monkeypatch):
    from ats.api import app

    root = Path(__file__).parents[2]
    subprocess.run(["docker", "exec", native_asterisk, "asterisk", "-C", "/input/asterisk.conf", "-rx", "rtp set debug on"], check=True, capture_output=True)
    certificate(tmp_path)
    # Uvicorn binds an ephemeral local port; no external CRM or PSTN is contacted.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    api_port = server.servers[0].sockets[0].getsockname()[1]
    ws_port = int(subprocess.check_output(["docker", "port", native_asterisk, "8088/tcp"], text=True).strip().rsplit(":", 1)[1])
    config_path = tmp_path / "fixture.json"
    config_path.write_text(json.dumps({"root": str(root), "apiPort": api_port, "wsPort": ws_port,
                                       "folder": str(tmp_path), "cross": widget["cross"]}))
    # The Node process opens a TLS proxy and reports its port before launching Chromium.
    output = (tmp_path / "browser.log").open("w")
    process = subprocess.Popen(["node", str(Path(__file__).with_name("widget_browser.cjs")), str(config_path)], stdout=output, stderr=subprocess.STDOUT)
    worker = Worker(factory)
    try:
        ready = tmp_path / "gateway.json"
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), (tmp_path / "browser.log").read_text()
        port = json.loads(ready.read_text())["port"]
        origin = f"https://localhost:{port}"
        for key, value in {"ATS_PUBLIC_ORIGIN": origin, "ATS_WEBRTC_ENABLED": "true",
                           "ATS_WEBRTC_WSS_URL": f"wss://localhost:{port}/asterisk/ws", "ATS_WEBRTC_SIP_DOMAIN": "localhost",
                           "ATS_WEBRTC_TURN_URLS": json.dumps([f"turn:127.0.0.1:{turn_server}?transport=tcp"]),
                           "ATS_WEBRTC_TURN_SECRET": "fixture-turn-secret-for-native-only"}.items():
            monkeypatch.setenv(key, value)
        settings.cache_clear()
        (tmp_path / "token.txt").write_text(jwt.encode({**widget["claims"], "aud": origin}, SECRET, algorithm="HS256"))
        (tmp_path / "recipient-token.txt").write_text(jwt.encode({**widget["claims"], "aud": origin,
            "user_id": widget["recipient_amo_id"]}, SECRET, algorithm="HS256"))
        deadline = time.monotonic() + 60
        while process.poll() is None and time.monotonic() < deadline:
            worker.schedule()
            worker.telephony()
            worker.call_control()
            worker.inbox()
            time.sleep(0.05)
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        logs = (tmp_path / "browser.log").read_text()
        Path(".runtime/widget-browser.log").write_text(logs)
        assert process.returncode == 0, logs
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            worker.call_control()
            worker.inbox()
            with factory() as db:
                attempt = db.scalar(select(Attempt))
                if attempt and attempt.status in {"completed", "failed"}:
                    break
            time.sleep(0.05)
        with factory() as db:
            attempt = db.scalar(select(Attempt))
            assert attempt.status == "completed", (attempt.status, attempt.reason)
            assert attempt.call_flow == "browser_callback" and attempt.channel_units == 1
            assert attempt.bridged_at and attempt.bridge_ended_at
            assert db.get(Operator, widget["operator_id"]).status == "free"
            assert db.get(Operator, widget["recipient_id"]).status == "free"
            assert attempt.route_snapshot["operator_id"] == str(widget["recipient_id"])
            call = db.scalar(select(WidgetCall))
            assert call.media_ready_at and call.cancel_requested
            assert call.operator_id == widget["recipient_id"]
            assert call.initiator_user_id == db.get(Operator, widget["operator_id"]).user_id
            crm = db.scalar(select(Outbox).where(Outbox.kind == "crm"))
            assert crm.payload["amo_responsible_user_id"] == widget["recipient_amo_id"]
        peer = json.loads(subprocess.check_output(["docker", "exec", native_asterisk, "cat", "/tmp/peer-state.json"], text=True))
        assert len(peer["invites"]) == 1 and peer["invites"][0]["role"] == "customer"
        assert peer["rtp_packets"] > 5
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        server.should_exit = True
        thread.join(timeout=5)
        output.close()
