#!/usr/bin/python3
"""Install bounded log rotation and conservative retention of compressed old archives."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

MARKER = "# Managed by ATOS Asterisk log retention\n"
AGE_MARKER = ".ats-retained-since"
LOG_DIR = Path("/var/log/asterisk")
INSTALLED = Path("/usr/local/sbin/ats-asterisk-log-retention")


def rotation_policy(log_dir):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(log_dir)):
        raise ValueError("Invalid log directory")
    return MARKER + f"""{log_dir}/messages.log {{
    daily
    maxsize 100M
    rotate 14
    compress
    delaycompress
    dateext
    dateformat -%Y%m%d-%H%M%S
    missingok
    notifempty
    su asterisk asterisk
    create 0640 asterisk asterisk
    postrotate
        /usr/sbin/asterisk -rx 'logger reload' >/dev/null
    endscript
}}
"""


def open_inodes():
    found = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except PermissionError:
            return None
        for descriptor in descriptors:
            try:
                info = descriptor.stat()
                found.add((info.st_dev, info.st_ino))
            except FileNotFoundError:
                continue
            except PermissionError:
                return None
    return found


def clean_archives(log_dir, *, days=30, apply=False, timestamp=None, opened=None):
    timestamp = time.time() if timestamp is None else timestamp
    result = {"enrolled": 0, "removed": 0, "kept": 0, "apply": apply}
    for folder in sorted(log_dir.glob("archive-*")):
        if folder.is_symlink() or not folder.is_dir():
            continue
        marker = folder / AGE_MARKER
        if not marker.exists():
            # gzip preserves old log mtimes. Retain from first enrollment, not
            # the original October timestamps, to avoid immediate deletion.
            if apply:
                descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
                os.utime(marker, (timestamp, timestamp))
            result["enrolled"] += 1
            continue
        if marker.is_symlink() or timestamp - marker.stat().st_mtime < days * 86400:
            result["kept"] += 1
            continue
        files = [file for file in folder.iterdir() if file.name != AGE_MARKER]
        if (opened is None or not files or any(
            file.is_symlink() or not file.is_file()
            or not re.fullmatch(r"messages\.log\.\d+\.gz", file.name)
            or (file.stat().st_dev, file.stat().st_ino) in opened for file in files
        )):
            result["kept"] += 1
            continue
        if apply:
            for file in files:
                file.unlink()
            marker.unlink()
            folder.rmdir()
        result["removed"] += 1
    return result


def write_managed(path, content, mode=0o644):
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    if path.exists():
        if path.read_bytes() == content:
            return
        if path not in {INSTALLED, Path("/etc/logrotate.d/ats-asterisk-messages")} and not path.read_text().startswith(MARKER):
            raise ValueError(f"Existing unmanaged file: {path}")
        backups = Path("/var/backups/ats-log-retention")
        backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(path, backups / (path.name + f".{time.time_ns()}"))
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ats-log-", delete=False) as output:
        temporary = Path(output.name)
        os.fchmod(output.fileno(), mode)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install():
    policy = Path("/etc/logrotate.d/ats-asterisk-messages")
    content = rotation_policy(LOG_DIR)
    # Validate before the file becomes visible to the system's logrotate timer.
    with tempfile.NamedTemporaryFile(mode="w", prefix="ats-logrotate-", delete=True) as candidate:
        candidate.write(content)
        candidate.flush()
        subprocess.run(["/usr/sbin/logrotate", "--debug", candidate.name], check=True)
    write_managed(policy, content.encode())
    write_managed(INSTALLED, Path(__file__).read_bytes(), 0o755)
    units = {
        "ats-asterisk-logrotate.service": MARKER + "[Service]\nType=oneshot\nExecStart=/usr/sbin/logrotate /etc/logrotate.d/ats-asterisk-messages\n",
        "ats-asterisk-logrotate.timer": MARKER + "[Timer]\nOnCalendar=hourly\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
        "ats-asterisk-archive-cleanup.service": MARKER + f"[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {INSTALLED} --cleanup --apply\n",
        "ats-asterisk-archive-cleanup.timer": MARKER + "[Timer]\nOnCalendar=daily\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
    }
    for name, text in units.items():
        write_managed(Path("/etc/systemd/system") / name, text.encode())
    print(json.dumps(clean_archives(LOG_DIR, apply=True, opened=open_inodes())))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "logrotate.timer",
                    "ats-asterisk-logrotate.timer", "ats-asterisk-archive-cleanup.timer"], check=True)
    print("Installed: hourly rotation, 14 rotations with compression; old archive folders retained for 30 days from enrollment.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.apply and os.geteuid() != 0:
        parser.error("Run as root to apply log retention")
    if LOG_DIR.is_symlink() or not LOG_DIR.is_dir():
        parser.error("Expected a regular /var/log/asterisk directory")
    if args.cleanup:
        print(json.dumps(clean_archives(LOG_DIR, apply=args.apply, opened=open_inodes())))
    elif args.apply:
        install()
    else:
        print(rotation_policy(LOG_DIR))
        print("Preview only. Use --apply to install hourly rotation and daily archive cleanup.")


if __name__ == "__main__":
    main()
