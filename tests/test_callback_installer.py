import importlib.util
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "callback_installer", Path(__file__).parents[1] / "scripts/install-mobile-callback.py",
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_installer_preserves_credentials_and_contexts_and_is_idempotent(tmp_path):
    dialplan, env = tmp_path / "extensions.conf", tmp_path / "ats.env"
    existing = b"[ats-beta]\nexten => s,1,Hangup()\n[ats-beta-answer]\n[ats-beta-echo]\n"
    dialplan.write_bytes(existing)
    secret = 'ATS_AMI_SECRET="fixture$literal#key"\n'
    env.write_text(secret + 'ATS_TEST_DESTINATIONS="+79990000001"\n')
    env.chmod(0o640)
    backups = installer.install(dialplan, env, "+79990000002", "+79990000003")
    assert len(backups) == 2 and all(path.stat().st_mode & 0o777 == 0o600 for path in backups)
    assert dialplan.read_bytes().startswith(existing)
    assert env.read_text().startswith(secret)
    assert 'ATS_TEST_DESTINATIONS="+79990000001,+79990000002,+79990000003"' in env.read_text()
    assert "ATS_CALLBACK_ENABLED" in env.read_text()
    assert installer.install(dialplan, env, "+79990000002", "+79990000003") == []
    assert env.stat().st_mode & 0o777 == 0o640


def test_installer_rejects_context_conflict_without_changing_env(tmp_path):
    dialplan, env = tmp_path / "extensions.conf", tmp_path / "ats.env"
    dialplan.write_text("[ats-beta]\n[ats-beta-answer]\n[ats-beta-callback-operator]\n")
    env.write_text("ATS_CALLBACK_ENABLED=false\n")
    env.chmod(0o600)
    with pytest.raises(ValueError, match="Existing callback"):
        installer.install(dialplan, env, "+79990000002", "+79990000003")
    assert env.read_text() == "ATS_CALLBACK_ENABLED=false\n"


def test_installer_restores_first_file_when_second_write_fails(tmp_path, monkeypatch):
    dialplan, env = tmp_path / "extensions.conf", tmp_path / "ats.env"
    original = b"[ats-beta]\n[ats-beta-answer]\n"
    dialplan.write_bytes(original)
    env.write_text("ATS_CALLBACK_ENABLED=false\n")
    env.chmod(0o600)
    write = installer.atomic_write

    def fail_env(path, *args):
        if path == env:
            raise PermissionError("fixture")
        return write(path, *args)

    monkeypatch.setattr(installer, "atomic_write", fail_env)
    with pytest.raises(PermissionError):
        installer.install(dialplan, env, "+79990000002", "+79990000003")
    assert dialplan.read_bytes() == original
    assert os.stat(env).st_mode & 0o777 == 0o600


def test_installer_upgrades_exact_published_v1_and_keeps_echo_and_private_backup(tmp_path):
    dialplan, env = tmp_path / "extensions.conf", tmp_path / "ats.env"
    prefix = "[ats-beta]\n[ats-beta-answer]\n[ats-beta-echo]\nexten => s,1,Echo()\n\n"
    v1 = (Path(__file__).parent / "fixtures/mobile-callback-v1.conf").read_text()
    original = prefix + v1
    dialplan.write_text(original)
    env.write_text('ATS_CALLBACK_ENABLED="true"\nATS_TEST_DESTINATIONS="+79990000002,+79990000003"\n')
    env.chmod(0o600)
    backups = installer.install(dialplan, env, "+79990000002", "+79990000003")
    assert len(backups) == 1 and backups[0].read_text() == original
    assert dialplan.read_text() == prefix + installer.SOURCE.read_text()
    assert installer.install(dialplan, env, "+79990000002", "+79990000003") == []


def test_installer_refuses_to_overwrite_modified_published_v1(tmp_path):
    dialplan, env = tmp_path / "extensions.conf", tmp_path / "ats.env"
    v1 = (Path(__file__).parent / "fixtures/mobile-callback-v1.conf").read_text()
    original = "[ats-beta]\n[ats-beta-answer]\n" + v1.replace(
        "ats-beta/callback-ready", "custom/manager-confirmation",
    )
    dialplan.write_text(original)
    env.write_text("ATS_CALLBACK_ENABLED=false\n")
    env.chmod(0o600)
    with pytest.raises(ValueError, match="Existing callback"):
        installer.install(dialplan, env, "+79990000002", "+79990000003")
    assert dialplan.read_text() == original and env.read_text() == "ATS_CALLBACK_ENABLED=false\n"
