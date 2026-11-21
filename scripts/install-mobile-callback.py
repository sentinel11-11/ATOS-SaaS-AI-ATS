"""Install the mobile callback dialplan and enable it for two explicit test destinations."""

import argparse
import grp
import hashlib
import io
import os
import pwd
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

SOURCE = Path(__file__).parents[1] / "deploy/asterisk/extensions-ats-beta-callback.conf"
V1_SHA256 = "9185c0a06a6d639e0c8ba91e27025236067f61396e716e77d40f8179fb03e11a"
V2_SHA256 = "0ee1d597fa3734943521fb7d60f6f5daef3a12df4594d5ca18e48ddc3ec8e9a1"
BLOCK_START = "; Mobile operator-first callback. The manager must press 1 before customer authorization."


class InstallationError(ValueError):
    """Messages contain only fixed diagnostics, never configuration values."""


def check_asterisk_support():
    required = [("application", name) for name in (
        "Dial", "Gosub", "Return", "UserEvent", "Wait", "MixMonitor", "StopMixMonitor",
    )] + [("function", name) for name in ("REGEX", "TIMEOUT")]
    for kind, name in required:
        result = subprocess.run(
            ["asterisk", "-n", "-rx", f"core show {kind} {name}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode or f"'{name}'" not in result.stdout or "[Syntax]" not in result.stdout:
            raise InstallationError(f"Asterisk {kind} {name} must be available")


def dialplan_bytes(original, block):
    contents = original.decode()
    headers = re.findall(r"^\s*\[([^]]+)\]", contents, re.MULTILINE)
    required = set(re.findall(r"^\[([^]]+)\]", block, re.MULTILINE))
    if not {"ats-beta", "ats-beta-answer"}.issubset(headers):
        raise InstallationError("Expected the existing ats-beta and ats-beta-answer contexts")
    if required.intersection(headers):
        if all(headers.count(name) == 1 for name in required) and block in contents:
            return original
        old_contexts = {"ats-beta-callback-operator", "ats-beta-callback-created",
                        "ats-beta-callback-answered", "ats-beta-callback-customer"}
        start = contents.find(BLOCK_START)
        # Only exact shipped versions may be upgraded; preserve custom prefixes/contexts.
        if (start >= 0 and set(headers).intersection(required) == old_contexts
            and all(headers.count(name) == 1 for name in old_contexts)
            and hashlib.sha256(contents[start:].rstrip().encode()).hexdigest() in {V1_SHA256, V2_SHA256}):
            return contents[:start].encode() + block.encode() + b"\n"
        raise InstallationError("Existing callback contexts differ; review them before replacement")
    return original.rstrip() + b"\n\n" + block.encode() + b"\n"


def environment_bytes(original, manager, customer):
    for phone in (manager, customer):
        if not re.fullmatch(r"\+7[0-9]{10}", phone):
            raise InstallationError("Both destinations must use +7 and ten digits")
    if manager == customer:
        raise InstallationError("Manager and customer must have different phone numbers")
    contents = original.decode()
    values = dotenv_values(stream=io.StringIO(contents), interpolate=False)
    allowed = {p.strip() for p in (values.get("ATS_TEST_DESTINATIONS") or "").split(",") if p.strip()}
    allowed.update((manager, customer))
    updates = {"ATS_CALLBACK_ENABLED": "true", "ATS_TEST_DESTINATIONS": ",".join(sorted(allowed))}
    # Only replace the two selected keys. Preserve all credentials and other settings verbatim.
    lines = [line for line in contents.splitlines() if not any(
        re.match(rf"^\s*(?:export\s+)?{key}\s*=", line) for key in updates
    )]
    lines.extend(f'{key}="{value}"' for key, value in updates.items())
    return ("\n".join(lines) + "\n").encode()


def atomic_write(path, contents, uid, gid, mode):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchown(output.fileno(), uid, gid)
            os.fchmod(output.fileno(), mode)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def install(dialplan, env_file, manager, customer, source=SOURCE):
    paths = [dialplan, env_file]
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise InstallationError("Select existing regular beta configuration files")
    originals = [path.read_bytes() for path in paths]
    metadata = [path.stat() for path in paths]
    if stat.S_IMODE(metadata[1].st_mode) & 0o007:
        raise InstallationError("The beta environment file must not be accessible to other users")
    updated = [
        dialplan_bytes(originals[0], source.read_text().rstrip()),
        environment_bytes(originals[1], manager, customer),
    ]
    stamp = datetime.now(UTC).strftime(".before-callback-%Y%m%dT%H%M%S%fZ.bak")
    backups, written = [], []
    try:
        for path, before, after, info in zip(paths, originals, updated, metadata, strict=True):
            if before == after:
                continue
            backup = path.with_name(path.name + stamp)
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(before)
                output.flush()
                os.fsync(output.fileno())
            backups.append(backup)
            atomic_write(path, after, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
            written.append((path, before, info))
    except OSError:
        for path, before, info in reversed(written):
            atomic_write(path, before, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        raise
    return backups


def install_audio():
    account = pwd.getpwnam("asterisk")
    group = grp.getgrnam("asterisk")
    pwd.getpwnam("ats-beta")
    recording_dir = Path("/var/spool/asterisk/monitor/ats-beta")
    for folder in (recording_dir,):
        if folder.is_symlink():
            raise InstallationError("Asterisk callback directories must not be symbolic links")
        folder.mkdir(parents=True, exist_ok=True)
        os.chown(folder, account.pw_uid, group.gr_gid)
        os.chmod(folder, 0o750)
    subprocess.run(["usermod", "-a", "-G", "asterisk", "ats-beta"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-phone", required=True)
    parser.add_argument("--customer-phone", required=True)
    parser.add_argument("--dialplan-file", type=Path, default=Path("/etc/asterisk/extensions-ats-beta.conf"))
    parser.add_argument("--env-file", type=Path, default=Path("/etc/ats-beta/ats.env"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root with /opt/ats-beta/.venv/bin/python")
    try:
        # Validate all configuration before preparing audio or modifying protected files.
        environment_bytes(args.env_file.read_bytes(), args.manager_phone, args.customer_phone)
        dialplan_bytes(args.dialplan_file.read_bytes(), SOURCE.read_text().rstrip())
        check_asterisk_support()
        install_audio()
        backups = install(args.dialplan_file, args.env_file, args.manager_phone, args.customer_phone)
        print("Callback installed; both test destinations are allowed. No calls were placed.")
        for backup in backups:
            print(f"Private backup: {backup}")
        print("Next: asterisk -rx 'dialplan reload', then bash deploy/ubuntu/update-beta.sh")
    except InstallationError as error:
        parser.exit(1, f"Callback installation failed: {error}\n")
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        # Never echo environment file contents or credentials in diagnostics.
        parser.exit(1, f"Callback installation failed ({type(error).__name__}). Configuration backups are retained.\n")


if __name__ == "__main__":
    main()
