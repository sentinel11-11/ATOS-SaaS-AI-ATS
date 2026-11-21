"""Add PJSIPShowEndpoint access to the existing ats-beta AMI account."""

import argparse
import glob
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SECTION = re.compile(r"^\s*\[([^]]+)\](.*)$")
INCLUDE = re.compile(r'^\s*#(?:try)?include\s+(?:"([^"]+)"|<([^>]+)>|(\S+))\s*(?:;.*)?$')
WRITE = re.compile(r"^(\s*write\s*(?:=>|=)\s*)([^;\r\n]*)(.*)$", re.IGNORECASE)


def config_files(root, seen=None, base=None):
    seen = set() if seen is None else seen
    base = root.parent.resolve() if base is None else base
    path = root.resolve(strict=True)
    if path in seen:
        return
    seen.add(path)
    if len(seen) > 128:
        raise ValueError("Too many included configuration files")
    contents = path.read_bytes().decode()
    yield path, contents
    for line in contents.splitlines():
        if re.match(r"^\s*#exec\b", line):
            raise ValueError("Dynamic #exec configuration requires manual editing")
        if match := INCLUDE.fullmatch(line):
            pattern = next(group for group in match.groups() if group is not None)
            include_path = Path(pattern)
            if not include_path.is_absolute():
                include_path = base / include_path
            for included in sorted(glob.glob(str(include_path))):
                yield from config_files(Path(included), seen, base)


def patch_account(contents):
    lines = contents.splitlines(keepends=True)
    sections = []
    for index, line in enumerate(lines):
        if match := SECTION.match(line):
            sections.append((index, match.group(1), match.group(2).strip()))
    matches = [entry for entry in sections if entry[1] == "ats-beta"]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Multiple ats-beta sections require manual editing")
    start, _, suffix = matches[0]
    if suffix and not suffix.startswith(";"):
        raise ValueError("Inherited ats-beta configuration requires manual editing")
    end = next((index for index, _, _ in sections if index > start), len(lines))
    writes = [(index, WRITE.fullmatch(lines[index].rstrip("\r\n")))
              for index in range(start + 1, end)]
    writes = [(index, match) for index, match in writes if match]
    if len(writes) != 1:
        raise ValueError("Expected exactly one write setting in the ats-beta section")
    index, match = writes[0]
    rights = [value.strip() for value in match.group(2).split(",") if value.strip()]
    if {value.lower() for value in rights} & {"system", "all"}:
        return contents
    if not {"call", "originate"}.issubset(value.lower() for value in rights):
        raise ValueError("Existing ats-beta account must have call and originate access")
    newline = lines[index][len(lines[index].rstrip("\r\n")):]
    comment = match.group(3)
    lines[index] = match.group(1) + ",".join([*rights, "system"])
    lines[index] += (" " + comment if comment else "") + newline
    return "".join(lines)


def enable(root):
    candidates = []
    for path, contents in config_files(root):
        updated = patch_account(contents)
        if updated is not None:
            candidates.append((path, contents, updated))
    if len(candidates) != 1:
        raise ValueError("Expected one ats-beta account in the included manager configuration")
    path, contents, updated = candidates[0]
    if contents == updated:
        print("ats-beta already has the required AMI write access")
        return
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("AMI configuration must be a regular file")
    backup = path.with_name(path.name + datetime.now(UTC).strftime(".ats-beta-%Y%m%dT%H%M%S%fZ.bak"))
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(path.read_bytes())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    print(f"Added system to ats-beta AMI write access in {path}")
    print(f"Backup {backup}")
    print("Apply with asterisk -rx 'manager reload'")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-conf", type=Path, default=Path("/etc/asterisk/manager.conf"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root")
    try:
        enable(args.manager_conf)
    except ValueError as error:
        parser.exit(1, f"AMI access update failed: {error}\n")
    except OSError as error:
        parser.exit(1, f"AMI access update failed ({type(error).__name__})\n")


if __name__ == "__main__":
    main()
