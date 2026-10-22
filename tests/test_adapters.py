import socketserver
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from ats.adapters import AMI, AmoCRM, DeliveryUnknown, Megafon, Rejected, Retryable


def config(**changes):
    return SimpleNamespace(
        **{
            "megafon_url": "https://test.megafon.ru",
            "megafon_user": "beta-manager",
            "megafon_api_key": SecretStr("fixture-key"),
            "amo_url": "https://beta.amocrm.ru",
            "amo_access_token": SecretStr("fixture-key"),
            **changes,
        }
    )


def test_megafon_contract():
    def handler(request):
        import json

        assert request.url.path == "/crmapi/v1/makecall"
        assert request.headers["x-api-key"] == "fixture-key"
        assert json.loads(request.content) == {"phone": "79990000101", "user": "beta-manager"}
        return httpx.Response(200, json={"callid": "ext-1"})

    driver = Megafon(config(), httpx.Client(transport=httpx.MockTransport(handler)))
    assert driver.originate(uuid4(), "+79990000101") == "ext-1"


@pytest.mark.parametrize(
    "status,exception", [(429, Retryable), (503, DeliveryUnknown), (401, Rejected), (302, Rejected)]
)
def test_remote_write_failure_policy(status, exception):
    driver = Megafon(
        config(), httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    )
    with pytest.raises(exception):
        driver.originate(uuid4(), "+79990000101")


def test_missing_callid_is_unknown():
    driver = Megafon(
        config(),
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))),
    )
    with pytest.raises(DeliveryUnknown):
        driver.originate(uuid4(), "+79990000101")


def test_amo_existing_marker_prevents_second_write():
    aid = uuid4()
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(
            200, json={"_embedded": {"notes": [{"params": {"text": f"ATS#{aid} done"}}]}}
        )

    driver = AmoCRM(config(), httpx.Client(transport=httpx.MockTransport(handler)))
    driver.send(aid, 123, "completed")
    assert requests == ["GET"]


def test_amo_creates_contact_note():
    aid = uuid4()
    requests = []

    def handler(request):
        import json

        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(204)
        assert request.url.path == "/api/v4/contacts/123/notes"
        assert f"ATS#{aid}" in json.loads(request.content)[0]["params"]["text"]
        return httpx.Response(200, json={})

    AmoCRM(config(), httpx.Client(transport=httpx.MockTransport(handler))).send(
        aid, 123, "completed"
    )
    assert requests == ["GET", "POST"]


def test_remote_urls_and_ami_injection_rejected():
    with pytest.raises(Rejected):
        Megafon(config(megafon_url="http://127.0.0.1"))
    with pytest.raises(Rejected):
        AmoCRM(config(amo_url="https://evil.invalid"))
    with pytest.raises(Rejected):
        AMI.encode("Originate", [("Channel", "x\r\nAction: Command")], "id")


def test_ami_real_socket_correlates_response_and_user_event():
    aid = str(uuid4())
    event_received = threading.Event()
    frames = []
    actions = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"Asterisk Call Manager/5.0\r\n\r\n")
            frame = {}
            while line := self.rfile.readline():
                if line == b"\r\n":
                    if not frame:
                        continue
                    actions.append(frame)
                    reply = f"Response: Success\r\nActionID: {frame['ActionID']}\r\n\r\n"
                    if frame["Action"] == "Originate":
                        reply += f"Event: UserEvent\r\nUserEvent: ATSCall\r\nAttemptID: {aid}\r\nState: completed\r\n\r\n"
                        reply += f"Event: OriginateResponse\r\nResponse: Failure\r\nActionID: {aid}\r\n\r\n"
                    self.wfile.write(reply.encode())
                    self.wfile.flush()
                    frame = {}
                elif b":" in line:
                    key, value = line.decode().split(":", 1)
                    frame[key.strip()] = value.strip()

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def callback(frame):
        frames.append(frame)
        if len(frames) == 2:
            event_received.set()

    driver = AMI(
        config(
            ami_host="127.0.0.1",
            ami_port=server.server_address[1],
            ami_tls=False,
            ami_user="fixture",
            ami_caller_id="9990000101",
            ami_secret=SecretStr("fixture-secret"),
        ),
        callback,
    )
    try:
        assert driver.originate(aid, "+79990000101") == aid
        assert event_received.wait(timeout=2)
        assert actions[1]["Channel"] == "Local/+79990000101@ats-beta/n"
        assert frames[0]["AttemptID"] == aid
        assert frames[1]["Event"] == "OriginateResponse"
    finally:
        driver.close()
        server.shutdown()
        server.server_close()


def test_megafon_webhook_auth_durability_and_no_secret_storage(
    client, factory, tenants, monkeypatch
):
    from sqlalchemy import select

    from ats.config import settings
    from ats.models import Inbox

    monkeypatch.setenv("ATS_TELEPHONY_TENANT_ID", str(tenants[0]))
    monkeypatch.setenv("ATS_MEGAFON_CRM_TOKEN", "fixture-webhook-token")
    settings.cache_clear()
    path = f"/webhooks/megafon/{tenants[0]}"
    form = {"cmd": "history", "callid": "call-1", "status": "Success", "crm_token": "wrong"}
    assert client.post(path, data=form).status_code == 403
    form["crm_token"] = "fixture-webhook-token"
    assert client.post(path, data=form).status_code == 200
    assert client.post(path, data=form).status_code == 200
    with factory() as db:
        rows = db.scalars(select(Inbox)).all()
        assert len(rows) == 1 and rows[0].event == "completed"
    form.update(cmd="event", type="ACCEPTED")
    assert client.post(path, data=form).json()["result"] == "accepted_without_state_change"
    settings.cache_clear()
