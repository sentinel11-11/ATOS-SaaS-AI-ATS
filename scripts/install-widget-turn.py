"""Configure dedicated coturn for the widget, with expiring credentials and one allowed PBX peer."""

import argparse
import grp
import io
import ipaddress
import os
import re
import runpy
import secrets
import shutil
import stat
from pathlib import Path

from dotenv import dotenv_values

MARKER = "# Managed by ATOS install-widget-turn.py. Private TURN shared secret.\n"


def turn_config(address, secret):
    ipaddress.IPv4Address(address)
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", secret):
        raise ValueError("Invalid TURN shared secret")
    return MARKER + f"""listening-port=3478
listening-ip=0.0.0.0
relay-ip={address}
fingerprint
use-auth-secret
static-auth-secret={secret}
realm=atos-widget
no-tls
no-dtls
no-cli
no-multicast-peers
no-tcp-relay
denied-peer-ip=0.0.0.0-255.255.255.255
denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
allowed-peer-ip={address}
min-port=49160
max-port=49200
user-quota=4
total-quota=50
max-bps=64000
bps-capacity=2000000
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-ip", required=True, help="Public IPv4 assigned to this VPS (Asterisk media address)")
    parser.add_argument("--env-file", type=Path, default=Path("/etc/ats-beta/ats.env"))
    parser.add_argument("--turn-file", type=Path, default=Path("/etc/turnserver.conf"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root with /opt/ats-beta/.venv/bin/python")
    try:
        if not ipaddress.IPv4Address(args.public_ip).is_global:
            parser.error("Select the public IPv4 assigned to this VPS")
        if not shutil.which("turnserver"):
            parser.error("Install coturn first: apt-get install coturn")
        if args.env_file.is_symlink() or args.turn_file.is_symlink():
            parser.error("Configuration paths must be regular files")
        meta = args.env_file.stat()
        if stat.S_IMODE(meta.st_mode) & 0o007:
            parser.error("The beta environment file must be private")
        before = args.turn_file.read_text() if args.turn_file.exists() else ""
        configured = [line for line in before.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if configured and not before.startswith(MARKER):
            parser.error("Existing coturn configuration is in use; preserve it and configure a separate TURN service")
        original = args.env_file.read_text()
        values = dotenv_values(stream=io.StringIO(original), interpolate=False)
        secret = values.get("ATS_WEBRTC_TURN_SECRET") or secrets.token_urlsafe(48)
        content = turn_config(args.public_ip, secret)
        keys = ("ATS_WEBRTC_TURN_SECRET", "ATS_WEBRTC_TURN_URLS")
        lines = [line for line in original.splitlines() if not any(
            re.match(rf"^\s*(?:export\s+)?{key}\s*=", line) for key in keys
        )]
        # TCP carries TURN client traffic; the relay towards Asterisk remains UDP/SRTP.
        lines += [f'ATS_WEBRTC_TURN_SECRET="{secret}"',
                  f"ATS_WEBRTC_TURN_URLS='[\"turn:{args.public_ip}:3478?transport=tcp\"]'"]
        write = runpy.run_path(str(Path(__file__).with_name("install-widget-webrtc.py")))["write_private"]
        write(args.turn_file, content, 0, grp.getgrnam("turnserver").gr_gid, 0o640)
        write(args.env_file, "\n".join(lines) + "\n", meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode))
        print("Dedicated widget TURN prepared. Shared secret not printed. No calls or service restarts performed.")
        print("Allow incoming TCP 3478 and UDP 49160:49200; restart coturn and beta API/workers.")
        print("TURN TLS on 5349/443 for networks blocking TCP 3478 requires a separate TLS listener/certificate.")
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"TURN preparation failed ({type(exc).__name__}). Private backups are retained.\n")


if __name__ == "__main__":
    main()
