"""Loopback-only AMI protocol fixture for the disposable HTTP smoke test."""

import socketserver
import threading


class AMIFixture:
    def __init__(self, secret, states=("answered", "completed", "completed", "answered")):
        self.secret = secret
        self.originates = []
        self.endpoint_checks = []
        self.setvars = []
        self.endpoints = {"mcm-cli-9990000101"}
        self.channels = {}
        self.hangups = []
        self.reject_hangup = False
        self.browser_registered = True
        self.registration_checks = []
        self.list_items_override = None
        fixture = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"Asterisk Call Manager/5.0\r\n\r\n")
                authenticated = False
                frame = {}
                while line := self.rfile.readline(8193):
                    if line != b"\r\n":
                        key, value = line.decode().split(":", 1)
                        frame.setdefault(key.strip(), []).append(value.strip())
                        continue
                    action = frame.get("Action", [""])[0]
                    action_id = frame.get("ActionID", [""])[0]
                    if action == "Login":
                        authenticated = frame.get("Secret") == [fixture.secret]
                    success = authenticated and action in {"Login", "Ping", "Originate", "Setvar", "CoreShowChannels"}
                    if success and action == "Setvar":
                        fixture.setvars.append(frame)
                    if authenticated and action == "PJSIPShowEndpoint":
                        endpoint = frame.get("Endpoint", [""])[0]
                        fixture.endpoint_checks.append(endpoint)
                        success = endpoint in fixture.endpoints
                    if authenticated and action in {"Getvar", "Hangup"}:
                        channel = frame.get("Channel", [""])[0]
                        success = channel in fixture.channels
                        if action == "Hangup":
                            fixture.hangups.append(channel)
                            success = success and not fixture.reject_hangup
                            if success:
                                fixture.channels.pop(channel)
                        variable = frame.get("Variable", [""])[0]
                        if action == "Getvar" and variable.startswith("PJSIP_DIAL_CONTACTS(") and variable.endswith(")"):
                            success = True
                            fixture.registration_checks.append(variable[len("PJSIP_DIAL_CONTACTS("):-1])
                    response = "Success" if success else "Error"
                    reply = f"Response: {response}\r\nActionID: {action_id}\r\n\r\n"
                    if success and action == "Getvar":
                        variable = frame.get("Variable", [""])[0]
                        if variable.startswith("PJSIP_DIAL_CONTACTS("):
                            target = variable[len("PJSIP_DIAL_CONTACTS("):-1]
                            value = "PJSIP/fixture/sip:registered@127.0.0.1" if fixture.browser_registered and target in fixture.endpoints else ""
                        else:
                            value = fixture.channels[channel].get(variable, "")
                        reply = f"Response: Success\r\nActionID: {action_id}\r\nValue: {value}\r\n\r\n"
                    if success and action == "CoreShowChannels":
                        channels = list(fixture.channels.items())
                        for name, values in channels:
                            reply += (f"Event: CoreShowChannel\r\nActionID: {action_id}\r\nChannel: {name}\r\n"
                                      f"Uniqueid: {values['UNIQUEID']}\r\nLinkedid: {values.get('LINKEDID', values['UNIQUEID'])}\r\n\r\n")
                        count = fixture.list_items_override if fixture.list_items_override is not None else len(channels)
                        reply += f"Event: CoreShowChannelsComplete\r\nActionID: {action_id}\r\nListItems: {count}\r\n\r\n"
                    if success and action == "Originate":
                        fixture.originates.append(frame)
                        # Duplicated terminal events and a late answer exercise
                        # inbox idempotency and monotonic terminal state.
                        for state in states:
                            reply += (
                                "Event: UserEvent\r\nUserEvent: ATSCall\r\n"
                                f"AttemptID: {action_id}\r\nState: {state}\r\n\r\n"
                            )
                    self.wfile.write(reply.encode())
                    self.wfile.flush()
                    frame = {}

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
