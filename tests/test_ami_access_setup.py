import runpy
import stat
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/enable-ami-endpoint-check.py"))


def test_ami_permission_setup_preserves_credentials_and_other_accounts(tmp_path, capsys):
    main = tmp_path / "manager.conf"
    included = tmp_path / "manager-ats-beta.conf"
    main.write_text('[general]\nenabled=yes\n#include "manager-ats-beta.conf"\n')
    original = (
        "[ats-beta]\nsecret = fixture-private-value\ndeny = 0.0.0.0/0\n"
        "permit = 127.0.0.1/32\nwrite = call,originate ; keep comment\n"
        "[other]\nwrite=call\nsecret=other-private-value\n"
    )
    included.write_text(original)
    included.chmod(0o640)
    SCRIPT["enable"](main)
    assert included.read_text() == original.replace(
        "write = call,originate ;", "write = call,originate,system ;"
    )
    assert stat.S_IMODE(included.stat().st_mode) == 0o640
    backup, = tmp_path.glob("*.bak")
    assert backup.read_text() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    SCRIPT["enable"](main)
    assert len(list(tmp_path.glob("*.bak"))) == 1
    output = capsys.readouterr().out
    assert "private-value" not in output


def test_ami_permission_setup_refuses_ambiguous_included_accounts(tmp_path):
    main = tmp_path / "manager.conf"
    main.write_text('#include "accounts-*.conf"\n')
    original = "[ats-beta]\nwrite=call,originate\n"
    for suffix in ["one", "two"]:
        (tmp_path / f"accounts-{suffix}.conf").write_text(original)
    with pytest.raises(ValueError, match="Expected one ats-beta"):
        SCRIPT["enable"](main)
    assert not list(tmp_path.glob("*.bak"))
    assert all(path.read_text() == original for path in tmp_path.glob("accounts-*.conf"))


def test_ami_permission_setup_resolves_nested_includes_from_asterisk_config_dir(tmp_path):
    main = tmp_path / "manager.conf"
    directory = tmp_path / "accounts"
    directory.mkdir()
    main.write_text('#include "accounts/includes.conf"\n')
    (directory / "includes.conf").write_text('#include "accounts/ats.conf"\n')
    account = directory / "ats.conf"
    account.write_bytes(b"[ats-beta]\r\nwrite=call,originate\r\n")
    SCRIPT["enable"](main)
    assert account.read_bytes() == b"[ats-beta]\r\nwrite=call,originate,system\r\n"


@pytest.mark.parametrize("section", [
    "[ats-beta](template)\nwrite=call,originate\n",
    "[ats-beta]\nwrite=call,originate\nwrite=system\n",
    "[ats-beta]\nwrite=call\n",
])
def test_ami_permission_setup_refuses_unexpected_account_layout(section):
    with pytest.raises(ValueError):
        SCRIPT["patch_account"](section)
