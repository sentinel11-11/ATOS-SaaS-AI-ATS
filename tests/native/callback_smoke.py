"""Opt-in native test: PYTHONPATH=tests pytest tests/native/callback_smoke.py."""

import json
import shutil
import socket
import subprocess
import time
import wave
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_mobile_callback import start

from ats.config import settings
from ats.models import Attempt, CallLeg, Contact, Operator, Outbox, now

IMAGE = "andrius/asterisk:20@sha256:05391d46660b1dcadd193e265758fb06a785ad484b7add2a0cb0ba5e50a76929"


@pytest.fixture
def native_asterisk(tmp_path, provider_config, monkeypatch, request):
    root = Path(__file__).parents[2]
    folder = tmp_path / "native"
    folder.mkdir()
    mode = getattr(request, "param", "bridge")
    data_dir = "/usr/share/asterisk" if mode == "vps_sound_layout" else "/var/lib/asterisk"
    (folder / "asterisk.conf").write_text(
        "[directories]\nastetcdir=/input\nastmoddir=/usr/lib/asterisk/modules\n"
        f"astvarlibdir=/var/lib/asterisk\nastdatadir={data_dir}\nastdbdir=/tmp\nastlogdir=/tmp\n"
        "astrundir=/tmp\nastspooldir=/var/spool/asterisk\n[options]\nverbose=5\n"
    )
    (folder / "modules.conf").write_text("[modules]\nautoload=yes\n")
    (folder / "logger.conf").write_text("[logfiles]\nconsole=notice,warning,error,verbose(5)\n")
    (folder / "manager.conf").write_text(
        "[general]\nenabled=yes\nbindaddr=0.0.0.0\nport=5038\n"
        "[ats-beta]\nsecret=fixture-secret\nread=call,user\nwrite=originate,call,system\n"
    )
    (folder / "pjsip.conf").write_text(
        "[transport-udp]\ntype=transport\nprotocol=udp\nbind=127.0.0.1:5060\n"
        "[fixture-aor]\ntype=aor\ncontact=sip:127.0.0.1:5070\n"
        "[mcm-cli-9990000101]\ntype=endpoint\ntransport=transport-udp\naors=fixture-aor\n"
        "disallow=all\nallow=alaw,ulaw\nfrom_user=9990000101\nfrom_domain=127.0.0.1\n"
        "direct_media=no\ndtmf_mode=rfc4733\n"
    )
    if mode == "browser":
        from sqlalchemy import select

        from ats.models import Operator
        from ats.webrtc import save_device

        widget = request.getfixturevalue("widget")
        factory = request.getfixturevalue("factory")
        with factory.begin() as db:
            devices = []
            for operator in db.scalars(select(Operator).where(Operator.tenant_id == widget["tenant_id"])):
                for slot in range(2):
                    endpoint = "ats-web-" + (operator.id.hex if slot == 0 else uuid4().hex)
                    save_device(db, operator, endpoint, endpoint, "browser-fixture-private-password-32-chars", slot=slot)
                    devices.append({"endpoint": endpoint, "username": endpoint,
                                    "password": "browser-fixture-private-password-32-chars"})
        import runpy

        render = runpy.run_path(str(root / "scripts/install-widget-webrtc.py"))["render_devices"]
        with (folder / "pjsip.conf").open("a") as output:
            output.write(render(devices, "localhost"))
        (folder / "http.conf").write_text('[general]\nenabled=no\n#include "http-widget.conf"\n')
        (folder / "http-widget.conf").write_text("[general](+)\nenabled=yes\nbindaddr=0.0.0.0\nbindport=8088\n")
    shutil.copy(root / "deploy/asterisk/extensions-ats-beta-callback.conf", folder / "extensions.conf")
    shutil.copy(Path(__file__).with_name("sip_callback_peer.py"), folder / "sip_peer.py")
    sounds = folder / "sounds/ats-beta"
    sounds.mkdir(parents=True)
    with wave.open(str(sounds / "callback-ready.wav"), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 1600)
    name = "atos-native-callback-" + uuid4().hex[:10]
    subprocess.run([
        "docker", "run", "-d", "--name", name,
        "-p", "127.0.0.1::5038", "-p", "127.0.0.1::8088", "--mount", f"type=bind,src={folder},dst=/input,readonly",
        "--entrypoint", "sh", IMAGE, "-c",
        "mkdir -p /usr/share/asterisk; cp -a /var/lib/asterisk/. /usr/share/asterisk/; "
        "mkdir -p /var/spool/asterisk/monitor/ats-beta /var/lib/asterisk/sounds/ats-beta; "
        "cp /input/sounds/ats-beta/* /var/lib/asterisk/sounds/ats-beta/; "
        "exec asterisk -f -n -C /input/asterisk.conf",
    ], check=True, capture_output=True)
    try:
        port = int(subprocess.check_output(["docker", "port", name, "5038/tcp"], text=True).strip().rsplit(":", 1)[1])
        deadline = time.monotonic() + 20
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2) as stream:
                    stream.settimeout(0.2)
                    if b"Asterisk" in stream.recv(100):
                        break
            except (OSError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Native Asterisk AMI did not start")
            running = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Running}}", name], text=True)
            if running.strip() != "true":
                raise RuntimeError("Native Asterisk exited during startup")
            time.sleep(0.1)
        subprocess.run([
            "docker", "exec", "-d", "-e", f"CALLBACK_PEER_MODE={getattr(request, 'param', 'bridge')}",
            name, "python3", "/input/sip_peer.py",
        ], check=True)
        time.sleep(0.3)
        monkeypatch.setenv("ATS_AMI_PORT", str(port))
        settings.cache_clear()
        yield name
    finally:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        suffix = getattr(request, "param", "bridge")
        Path(f".runtime/callback-native-{suffix}.log").write_text(logs.stdout + logs.stderr)
        state = subprocess.run(["docker", "exec", name, "cat", "/tmp/peer-state.json"], capture_output=True, text=True)
        Path(f".runtime/callback-native-{suffix}-peer.json").write_text(state.stdout)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.mark.parametrize("native_asterisk", ["bridge", "vps_sound_layout", "no_dtmf", "missing_prompt"], indirect=True)
def test_real_asterisk_callback_without_keypress_and_bidirectional_rtp(factory, callback, native_asterisk):
    initial = json.loads(subprocess.check_output(["docker", "exec", native_asterisk, "cat", "/tmp/peer-state.json"], text=True))
    if initial["mode"] == "missing_prompt":
        subprocess.run(["docker", "exec", native_asterisk, "rm", "/var/lib/asterisk/sounds/ats-beta/callback-ready.wav"], check=True)
    worker, attempt_id = start(factory, callback)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        worker.call_control()
        worker.inbox()
        with factory() as db:
            call = db.get(Attempt, UUID(attempt_id))
            if call.status in {"completed", "failed"}:
                break
        time.sleep(0.05)
    with factory() as db:
        call = db.get(Attempt, UUID(attempt_id))
        assert call.status == "completed", (call.status, call.reason, call.pending_terminal)
        assert call.manager_confirmed_at and call.bridged_at and call.bridge_ended_at
        legs = db.scalars(select(CallLeg).where(CallLeg.attempt_id == call.id)).all()
        assert all(leg.ended_at and leg.entered_bridge_at for leg in legs)
    state = json.loads(subprocess.check_output([
        "docker", "exec", native_asterisk, "cat", "/tmp/peer-state.json",
    ], text=True))
    assert [row["role"] for row in state["invites"]] == ["operator", "customer"]
    assert all(row["from_phone"] == "9990000101" for row in state["invites"])
    assert state["media_received"] == {"operator": True, "customer": True}


@pytest.mark.parametrize("native_asterisk,reason,roles", [
    ("operator_busy", "operator_busy", ["operator"]),
    ("customer_busy", "customer_busy", ["operator", "customer"]),
    ("gate_denied", "callback_gate_denied", ["operator"]),
], indirect=["native_asterisk"])
def test_real_asterisk_failures_do_not_become_success(factory, callback, native_asterisk, reason, roles):
    worker, attempt_id = start(factory, callback)
    if reason == "callback_gate_denied":
        with factory.begin() as db:
            db.get(Contact, callback["contact_id"]).consent = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        worker.call_control()
        worker.inbox()
        with factory() as db:
            call = db.get(Attempt, UUID(attempt_id))
            if call.status in {"completed", "failed"}:
                break
        time.sleep(0.05)
    with factory() as db:
        call = db.get(Attempt, UUID(attempt_id))
        assert (call.status, call.reason) == ("failed", reason)
        assert not call.bridged_at and not call.answered_at
        assert all(leg.ended_at for leg in db.scalars(select(CallLeg)))
    state = json.loads(subprocess.check_output([
        "docker", "exec", native_asterisk, "cat", "/tmp/peer-state.json",
    ], text=True))
    assert [row["role"] for row in state["invites"]] == roles


@pytest.mark.parametrize("native_asterisk", ["bridge"], indirect=True)
def test_targeted_hangup_recovers_after_real_ami_terminal_events_are_lost(factory, callback, native_asterisk):
    from datetime import timedelta

    from ats.callbacks import queue_hangup

    worker, ident = start(factory, callback)
    attempt_id = UUID(ident)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        worker.call_control()
        with factory() as db:
            attempt = db.get(Attempt, attempt_id)
            if attempt.bridged_at:
                break
        time.sleep(0.02)
    assert attempt.bridged_at
    driver = worker.driver("multicom_ami", callback["tenant_id"])
    driver.on_event = lambda _frame: None
    # Lose the observer after a real SIP/RTP bridge. Resource release must now
    # require a complete CoreShowChannels/Getvar read, not an AMI event or clock.
    with factory.begin() as db:
        attempt = db.get(Attempt, attempt_id)
        attempt.status, attempt.reason = "unknown", "callback_evidence_missing"
        db.scalar(select(Outbox).where(Outbox.kind == "telephony")).available_at = now() - timedelta(minutes=2)
        queue_hangup(db, attempt)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        worker.call_control()
        with factory() as db:
            attempt = db.get(Attempt, attempt_id)
            if attempt.status == "failed":
                assert db.get(Operator, callback["operator_id"]).status == "free"
                assert attempt.reason == "cancelled_by_user"
                assert driver.channels() == []
                return
        time.sleep(0.1)
    raise AssertionError("Real AMI channel evidence did not release the cancelled call")
