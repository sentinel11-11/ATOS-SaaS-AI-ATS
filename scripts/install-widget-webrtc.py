"""Prepare private Asterisk browser devices. Does not restart Asterisk or place calls."""

import argparse
import grp
import io
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

MARKER = "; Managed by ATOS install-widget-webrtc.py. Private SIP credentials.\n"


class PreparationError(ValueError):
    """A fixed diagnostic that never includes a configuration value."""


def render_devices(devices, domain):
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,200}", domain):
        raise PreparationError("Invalid browser SIP domain")
    blocks = [MARKER, "[ats-beta-ws]\ntype=transport\nprotocol=ws\nbind=0.0.0.0\n"]
    for device in devices:
        endpoint, username, password = (device[key] for key in ("endpoint", "username", "password"))
        if not re.fullmatch(r"ats-web-[a-f0-9]{12,32}", endpoint) or username != endpoint:
            raise PreparationError("Invalid browser device identity")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
            raise PreparationError("Invalid browser device secret")
        blocks.append(f"""[{endpoint}]
type=endpoint
transport=ats-beta-ws
context=ats-beta-webrtc-deny
disallow=all
allow=alaw,ulaw
aors={endpoint}
auth={endpoint}
from_domain={domain}
webrtc=yes
dtls_auto_generate_cert=yes
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
rtp_timeout=15
rtp_timeout_hold=60
[{endpoint}]
type=auth
auth_type=userpass
username={username}
password={password}
[{endpoint}]
type=aor
max_contacts=1
remove_existing=yes
minimum_expiration=60
maximum_expiration=3600
""")
    return "\n".join(blocks)


def with_include(original, filename):
    directive = f'#include "{filename}"'
    if re.search(rf'^\s*#include\s+["<]?{re.escape(filename)}[">]?\s*$', original, re.MULTILINE):
        return original
    return original.rstrip() + "\n\n" + directive + "\n"


def write_private(path, contents, uid, gid, mode):
    if path.is_symlink():
        raise PreparationError("Configuration paths must not be symbolic links")
    if path.exists() and path.read_text() == contents:
        return
    if path.exists():
        backup = path.with_name(path.name + datetime.now(UTC).strftime(".before-widget-%Y%m%dT%H%M%S%fZ.bak"))
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(path.read_bytes())
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        os.fchown(output.fileno(), uid, gid)
        os.fchmod(output.fileno(), mode)
        output.write(contents.encode())
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def environment(original, domain):
    # Keep the original quoting of all other values, especially SIP/AMI credentials.
    updates = {"ATS_WEBRTC_ENABLED": "true", "ATS_WEBRTC_SIP_DOMAIN": domain,
               "ATS_WEBRTC_WSS_URL": f"wss://{domain}/asterisk/ws"}
    lines = [line for line in original.splitlines() if not any(
        re.match(rf"^\s*(?:export\s+)?{key}\s*=", line) for key in updates
    )]
    return "\n".join(lines + [f'{key}="{value}"' for key, value in updates.items()]) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/etc/ats-beta/ats.env"))
    parser.add_argument("--asterisk-dir", type=Path, default=Path("/etc/asterisk"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root with /opt/ats-beta/.venv/bin/python")
    try:
        info = args.env_file.stat()
        if args.env_file.is_symlink() or stat.S_IMODE(info.st_mode) & 0o007:
            raise PreparationError("The beta environment file must be private and regular")
        original = args.env_file.read_text()
        values = dotenv_values(stream=io.StringIO(original), interpolate=False)
        origin = urlsplit(values.get("ATS_PUBLIC_ORIGIN", ""))
        if origin.scheme != "https" or not origin.hostname or origin.port or origin.path not in ("", "/"):
            raise PreparationError("ATS_PUBLIC_ORIGIN must be the public HTTPS origin on port 443")
        domain = origin.hostname
        if not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
            raise PreparationError("Invalid public hostname")
        pjsip, http = (args.asterisk_dir / name for name in ("pjsip.conf", "http.conf"))
        if any(path.is_symlink() or not path.is_file() for path in (pjsip, http)):
            raise PreparationError("Expected regular pjsip.conf and http.conf files")
        pjsip_before, http_before = pjsip.read_text(), http.read_text()
        # An already used HTTP service needs its existing route preserved.
        status = subprocess.run(["asterisk", "-rx", "http show status"], text=True, capture_output=True, check=True, timeout=10).stdout
        bindings = re.findall(r"(?:HTTP|HTTPS) Server Enabled and Bound to ([^\s]+)", status)
        if any(binding != "127.0.0.1:8088" for binding in bindings):
            raise PreparationError("Asterisk HTTP already uses another binding; preserve it and configure the WSS proxy separately")
        for module in ("res_http_websocket.so", "res_pjsip_transport_websocket.so", "res_srtp.so"):
            loaded = subprocess.run(["asterisk", "-rx", f"module show like {module}"], capture_output=True, text=True, timeout=10, check=True)
            if not re.search(r"\b[1-9][0-9]* modules loaded", loaded.stdout):
                raise PreparationError(f"Load the required Asterisk module first: {module}")
        group, account = grp.getgrnam("asterisk"), pwd.getpwnam("ats-beta")
        target = args.asterisk_dir / "pjsip-ats-beta-webrtc.conf"
        if target.exists() and not target.read_text().startswith(MARKER):
            raise PreparationError("Existing browser configuration is not managed by ATOS")
        addon = args.asterisk_dir / "http-ats-beta-webrtc.conf"
        if addon.exists() and not addon.read_text().startswith(MARKER):
            raise PreparationError("Existing HTTP addition is not managed by ATOS")
        helper = Path(__file__).with_name("provision-webrtc-devices.py").resolve()
        with tempfile.TemporaryDirectory(prefix="ats-webrtc-", dir="/tmp") as directory:
            os.chown(directory, account.pw_uid, account.pw_gid)
            private = Path(directory) / "devices.json"
            subprocess.run(["runuser", "-u", "ats-beta", "--", str(Path(__file__).parents[1] / ".venv/bin/python"),
                            str(helper), "--env-file", str(args.env_file), "--private-output", str(private)], check=True)
            content = render_devices(json.loads(private.read_text()), domain)
            private.unlink()
        general = "[general](+)" if re.search(r"^\s*\[general\]", http_before, re.MULTILINE) else "[general]"
        write_private(target, content, 0, group.gr_gid, 0o640)
        write_private(addon, MARKER + general + "\nenabled=yes\nbindaddr=127.0.0.1\nbindport=8088\n", 0, group.gr_gid, 0o640)
        for path, contents in ((pjsip, with_include(pjsip_before, target.name)), (http, with_include(http_before, addon.name))):
            meta = path.stat()
            write_private(path, contents, meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode))
        write_private(args.env_file, environment(original, domain), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        print("WebRTC files prepared. Credentials were not printed. No calls or restarts were performed.")
        print("Next: configure deploy/ubuntu/nginx-asterisk-ws.conf in the HTTPS server block; nginx -t.")
        print("With no active calls: asterisk -rx 'core reload'; check http show status and pjsip show transports.")
        print("Restart beta API and workers to load the new environment. See docs/AMO_WIDGET_SETUP_RU.md.")
    except PreparationError as exc:
        parser.exit(1, f"WebRTC preparation failed: {exc}\n")
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        # The exception might contain a private SQL/config value. Only expose its class.
        parser.exit(1, f"WebRTC preparation failed ({type(exc).__name__}). Review configuration; private backups are retained.\n")


if __name__ == "__main__":
    main()
