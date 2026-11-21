"""Loopback-only SIP/RTP test peer. Contains no real provider addresses or credentials."""

import json
import os
import re
import socket
import struct
import threading
import time
from pathlib import Path
from uuid import uuid4


class Peer:
    def __init__(self):
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.1", 5070))
        self.dialogs = {}
        self.mode = os.environ.get("CALLBACK_PEER_MODE", "bridge")
        self.lock = threading.RLock()
        self.state = {"mode": self.mode, "invites": [], "media_received": {"operator": False, "customer": False}}

    def save(self):
        with self.lock:
            Path("/tmp/peer-state.json").write_text(json.dumps(self.state))

    def reply(self, data, address, status, body="", tag="fixture"):
        lines = data.split("\r\n")
        headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        headers = {key.lower(): value.strip() for key, value in headers.items()}
        target = headers["to"]
        if ";tag=" not in target:
            target += ";tag=" + tag
        message = (
            f"SIP/2.0 {status}\r\nVia: {headers['via']}\r\nFrom: {headers['from']}\r\n"
            f"To: {target}\r\nCall-ID: {headers['call-id']}\r\nCSeq: {headers['cseq']}\r\n"
            "Contact: <sip:fixture@127.0.0.1:5070>\r\n"
            + ("Content-Type: application/sdp\r\n" if body else "")
            + f"Content-Length: {len(body)}\r\n\r\n{body}"
        )
        self.sip.sendto(message.encode(), address)
        return headers, target

    def media(self, dialog):
        role = dialog["role"]
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind(("127.0.0.1", dialog["port"]))
        rtp.settimeout(0.001)
        sequence, timestamp = 1, 10000
        payload_byte = 0x2A if role == "operator" else 0xAA
        expected = 0xAA if role == "operator" else 0x2A
        started = time.monotonic()
        dtmf_sent = False
        bye_sent = False

        def send(payload, kind=8, marker=False, stamp=None):
            nonlocal sequence, timestamp
            header = struct.pack("!BBHII", 0x80, kind | (0x80 if marker else 0), sequence,
                                 timestamp if stamp is None else stamp, dialog["port"])
            rtp.sendto(header + payload, dialog["rtp"])
            sequence = (sequence + 1) % 65536
            timestamp += 160

        while time.monotonic() - started < 35 and not dialog.get("ended"):
            send(bytes([payload_byte]) * 160)
            elapsed = time.monotonic() - started
            if role == "operator" and self.mode not in {"no_confirmation", "confirmation_timeout"} and elapsed > 1 and not dtmf_sent:
                event_stamp = timestamp
                for index, duration in enumerate([160, 320, 480, 800, 800, 800]):
                    send(struct.pack("!BBH", 1, 10 | (0x80 if index >= 3 else 0), duration),
                         kind=dialog["dtmf"], marker=index == 0, stamp=event_stamp)
                    time.sleep(0.02)
                dtmf_sent = True
            if ((role == "customer" and elapsed > 2)
                or (role == "operator" and self.mode == "no_confirmation" and elapsed > 3)) and not bye_sent:
                self.bye(dialog)
                bye_sent = True
            try:
                packet = rtp.recv(2048)
                offset = 12 + (packet[0] & 15) * 4
                if len(packet) > offset and (packet[1] & 127) == 8:
                    audio = packet[offset:]
                    if audio and all(value == expected for value in audio):
                        if not self.state["media_received"][role]:
                            self.state["media_received"][role] = True
                            self.save()
            except (socket.timeout, IndexError):
                pass
            time.sleep(0.02)
        rtp.close()

    def bye(self, dialog):
        headers = dialog["headers"]
        target = re.search(r"<([^>]+)>", headers["contact"]).group(1)
        message = (
            f"BYE {target} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:5070;branch=z9hG4bK{uuid4().hex}\r\n"
            f"From: {dialog['to']}\r\nTo: {headers['from']}\r\nCall-ID: {headers['call-id']}\r\n"
            "CSeq: 20 BYE\r\nMax-Forwards: 70\r\nContent-Length: 0\r\n\r\n"
        )
        self.sip.sendto(message.encode(), dialog["address"])

    def run(self):
        self.save()
        Path("/tmp/peer-ready").touch()
        while True:
            raw, address = self.sip.recvfrom(65536)
            data = raw.decode("utf-8", "replace")
            lines = data.split("\r\n")
            headers = {key.lower(): value.strip() for key, value in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            ident = headers.get("call-id", "")
            if data.startswith("INVITE "):
                phone = re.search(r"INVITE sip:([0-9]+)@", data).group(1)
                role = "operator" if phone == "89990001000" else "customer"
                if ident not in self.dialogs:
                    self.state["invites"].append({
                        "phone": phone, "role": role,
                        "from_phone": re.search(r"sip:([0-9]+)@", headers["from"]).group(1),
                    })
                    self.save()
                    if self.mode == role + "_busy":
                        self.reply(data, address, "486 Busy Here")
                        continue
                    port = 59000 if role == "operator" else 59002
                    dtmf = re.search(r"a=rtpmap:([0-9]+) telephone-event", data)
                    self.dialogs[ident] = {
                        "role": role, "port": port, "dtmf": int(dtmf.group(1)) if dtmf else 101,
                        "rtp": (re.search(r"c=IN IP4 ([0-9.]+)", data).group(1),
                                int(re.search(r"m=audio ([0-9]+)", data).group(1))),
                        "address": address,
                    }
                dialog = self.dialogs[ident]
                sdp = (
                    "v=0\r\no=fixture 1 1 IN IP4 127.0.0.1\r\ns=callback-fixture\r\n"
                    f"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {dialog['port']} RTP/AVP 8 {dialog['dtmf']}\r\n"
                    f"a=rtpmap:8 PCMA/8000\r\na=rtpmap:{dialog['dtmf']} telephone-event/8000\r\n"
                    f"a=fmtp:{dialog['dtmf']} 0-16\r\na=sendrecv\r\n"
                )
                saved, target = self.reply(data, address, "200 OK", sdp, tag="fixture-" + role)
                dialog.update(headers=saved, to=target)
            elif data.startswith("ACK ") and ident in self.dialogs:
                dialog = self.dialogs[ident]
                if not dialog.get("started"):
                    dialog["started"] = True
                    threading.Thread(target=self.media, args=(dialog,), daemon=True).start()
            elif data.startswith("BYE "):
                self.reply(data, address, "200 OK")
                if ident in self.dialogs:
                    self.dialogs[ident]["ended"] = True
            elif data.startswith("OPTIONS "):
                self.reply(data, address, "200 OK")


if __name__ == "__main__":
    Peer().run()
