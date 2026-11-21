import runpy
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/install-multicom-echo-test.py"))


def test_echo_install_preserves_existing_dialplan_and_creates_private_backup(tmp_path):
    target = tmp_path / "extensions-ats-beta.conf"
    original = b"[ats-beta]\r\nexten => _+X.,1,Hangup()\r\n[ats-beta-answer]\r\n"
    target.write_bytes(original)
    target.chmod(0o640)
    SCRIPT["install"](target)
    assert target.read_bytes().startswith(original)
    assert "Set(TIMEOUT(absolute)=30)" in target.read_text()
    assert "Echo()" in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    backup, = tmp_path.glob("*.bak")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    installed = target.read_bytes()
    SCRIPT["install"](target)
    assert target.read_bytes() == installed
    assert len(list(tmp_path.glob("*.bak"))) == 1


@pytest.mark.parametrize("original", [
    "[other]\nexten => s,1,Hangup()\n",
    "[ats-beta]\n[ats-beta-answer]\n[ats-beta-echo]\nexten => s,1,Wait(999)\n",
])
def test_echo_install_refuses_unexpected_or_conflicting_dialplan(tmp_path, original):
    target = tmp_path / "extensions.conf"
    target.write_text(original)
    with pytest.raises(ValueError):
        SCRIPT["install"](target)
    assert target.read_text() == original
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("missing", ["Echo", "TIMEOUT"])
def test_echo_setup_rejects_unavailable_asterisk_app_or_timer_even_on_cli_zero_exit(
    monkeypatch, missing
):
    def command(args, **kwargs):
        name = args[-1].split()[-1]
        output = "Not registered" if name == missing else f"Info about '{name}'\n[Syntax]"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(SCRIPT["subprocess"], "run", command)
    with pytest.raises(ValueError, match=missing):
        SCRIPT["check_asterisk_support"]()
