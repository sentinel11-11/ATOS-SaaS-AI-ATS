"""Append the bounded Echo diagnostic context to the existing beta dialplan."""

import argparse
import os
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "deploy/asterisk/extensions-ats-beta-echo.conf"


def check_asterisk_support():
    for kind, name in [("application", "Echo"), ("function", "TIMEOUT")]:
        result = subprocess.run(
            ["asterisk", "-n", "-rx", f"core show {kind} {name}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode or f"'{name}'" not in result.stdout or "[Syntax]" not in result.stdout:
            raise ValueError(f"Asterisk {kind} {name} must be available before installing Echo")


def install(target, source=SOURCE):
    if target.is_symlink():
        raise ValueError("Select the actual beta dialplan file instead of a symbolic link")
    original = target.read_bytes()
    contents = original.decode()
    block = source.read_text().rstrip()
    headers = re.findall(r"^\s*\[([^]]+)\]", contents, re.MULTILINE)
    if not {"ats-beta", "ats-beta-answer"}.issubset(headers):
        raise ValueError("Expected the existing ats-beta and ats-beta-answer contexts")
    if "ats-beta-echo" in headers:
        if headers.count("ats-beta-echo") == 1 and block in contents:
            print("The bounded Echo context is already installed")
            return
        raise ValueError("An existing ats-beta-echo context requires manual review")
    metadata = target.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("The beta dialplan must be a regular file")
    backup = target.with_name(
        target.name + datetime.now(UTC).strftime(".before-echo-%Y%m%dT%H%M%S%fZ.bak")
    )
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(original)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            separator = b"\n" if original.endswith(b"\n") else b"\n\n"
            output.write(original + separator + block.encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    print(f"Appended ats-beta-echo to {target}")
    print(f"Backup {backup}")
    print("Apply with asterisk -rx 'dialplan reload'")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dialplan-file", type=Path, default=Path("/etc/asterisk/extensions-ats-beta.conf")
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root")
    try:
        check_asterisk_support()
        install(args.dialplan_file)
    except ValueError as error:
        parser.exit(1, f"Echo context installation failed: {error}\n")
    except OSError as error:
        parser.exit(1, f"Echo context installation failed ({type(error).__name__})\n")
    except subprocess.SubprocessError as error:
        parser.exit(1, f"Asterisk support check failed ({type(error).__name__})\n")


if __name__ == "__main__":
    main()
