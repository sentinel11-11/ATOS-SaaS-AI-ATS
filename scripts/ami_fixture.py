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
                    success = authenticated and action in {"Login", "Ping", "Originate", "Setvar"}
                    if success and action == "Setvar":
                        fixture.setvars.append(frame)
                    if authenticated and action == "PJSIPShowEndpoint":
                        endpoint = frame.get("Endpoint", [""])[0]
                        fixture.endpoint_checks.append(endpoint)
                        success = endpoint in fixture.endpoints
                    response = "Success" if success else "Error"
                    reply = f"Response: {response}\r\nActionID: {action_id}\r\n\r\n"
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
