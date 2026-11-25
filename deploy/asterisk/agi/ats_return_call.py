#!/usr/bin/python3
"""Asterisk AGI client for the local return-call socket."""

import http.client
import json
import re
import socket
import sys
from uuid import UUID

SOCKET = "/run/ats-beta-return-call.sock"
DIAL = re.compile(r"PJSIP/(?:[0-9]{1,20}|ats-web-[a-f0-9]{12,32}|8[0-9]{10}@mcm-cli-[0-9]{10})")


class LocalConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def request(path, payload=None):
    connection = LocalConnection("localhost", timeout=1.2)
    try:
        body = json.dumps(payload).encode() if payload is not None else b""
        connection.request("POST", path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        return json.loads(response.read(2048)) if response.status == 200 else {}
    finally:
        connection.close()


def read_environment():
    values = {}
    for _ in range(100):
        line = sys.stdin.readline(8193).strip()
        if not line:
            return values
        key, _, value = line.partition(":")
        values[key] = value.strip()
    raise ValueError("AGI environment limit")


def set_variable(name, value):
    # Values are checked before issuing AGI commands. No input enters these commands directly.
    print(f'SET VARIABLE {name} "{value}"', flush=True)
    if not sys.stdin.readline(8193).startswith("200 result=1"):
        raise ValueError("AGI command rejected")


def resolve(environment, destination):
    result = request("/resolve", {
        "external_id": environment.get("agi_uniqueid", ""),
        "phone": environment.get("agi_callerid", ""),
        "destination": destination,
    })
    dial = result.get("dial", "")
    if not DIAL.fullmatch(dial):
        return
    ident = str(UUID(result["id"]))
    token, caller_number = result["token"], result["caller_number"]
    ring, talk = int(result["ring_seconds"]), int(result["talk_seconds"])
    if (not re.fullmatch(r"(?:[a-f0-9]{48})?", token)
        or not re.fullmatch(r"(?:[0-9]{10})?", caller_number)
        or not 1 <= ring <= 30 or not 60 <= talk <= 7200):
        return
    set_variable("ATS_RETURN_ID", ident)
    set_variable("ATS_RETURN_TOKEN", token)
    set_variable("ATS_RETURN_CALLER", caller_number)
    set_variable("ATS_RETURN_RING", str(ring))
    set_variable("ATS_RETURN_TALK", str(talk))
    set_variable("ATS_RETURN_DIAL", dial)


def main():
    try:
        environment = read_environment()
        command, argument = sys.argv[1:3]
        if command == "resolve":
            resolve(environment, argument)
        elif command == "answer":
            if request(f"/answer/{UUID(argument)}").get("answered") is True:
                set_variable("ATS_RETURN_ACCEPTED", "1")
        elif command in {"release", "hangup"}:
            request(f"/release/{UUID(argument)}?hangup={str(command == 'hangup').lower()}")
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        # A missing route or local lookup failure leaves the existing inbound path in charge.
        return


if __name__ == "__main__":
    main()
