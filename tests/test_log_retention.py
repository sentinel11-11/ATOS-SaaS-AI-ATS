import importlib.util
import os
from pathlib import Path


def module():
    path = Path(__file__).parents[1] / "scripts/install-asterisk-log-retention.py"
    spec = importlib.util.spec_from_file_location("log_retention", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_old_compressed_timestamps_do_not_trigger_immediate_deletion(tmp_path):
    retention = module()
    archive = tmp_path / "archive-old"
    archive.mkdir()
    compressed = archive / "messages.log.0.gz"
    compressed.write_bytes(b"synthetic")
    os.utime(compressed, (1, 1))
    result = retention.clean_archives(tmp_path, apply=True, timestamp=10_000_000, opened=set())
    assert result["enrolled"] == 1 and compressed.exists()
    result = retention.clean_archives(tmp_path, apply=True, timestamp=10_000_000 + 31 * 86400, opened=set())
    assert result["removed"] == 1 and not archive.exists()


def test_cleanup_keeps_incomplete_open_and_unverifiable_archives(tmp_path):
    retention = module()
    for kind in ["incomplete", "opened", "unverifiable", "symlink"]:
        archive = tmp_path / ("archive-" + kind)
        archive.mkdir()
        marker = archive / retention.AGE_MARKER
        marker.touch()
        os.utime(marker, (1, 1))
        compressed = archive / "messages.log.0.gz"
        compressed.write_bytes(b"synthetic")
        if kind == "incomplete":
            (archive / "messages.log.0").write_bytes(b"gzip still running")
        if kind == "symlink":
            compressed.unlink()
            compressed.symlink_to(marker)
    opened_file = tmp_path / "archive-opened/messages.log.0.gz"
    opened = {(opened_file.stat().st_dev, opened_file.stat().st_ino)}
    retention.clean_archives(tmp_path, apply=True, timestamp=10_000_000, opened=None)
    assert len(list(tmp_path.glob("archive-*"))) == 4
    retention.clean_archives(tmp_path, apply=True, timestamp=10_000_000, opened=opened)
    assert (tmp_path / "archive-incomplete/messages.log.0").exists()
    assert opened_file.exists()
    assert (tmp_path / "archive-symlink/messages.log.0.gz").is_symlink()


def test_policy_targets_actual_filename_and_reload_only(tmp_path):
    policy = module().rotation_policy(tmp_path)
    assert str(tmp_path / "messages.log") in policy
    assert "rotate 14" in policy and "compress" in policy and "maxsize 100M" in policy
    assert "logger reload" in policy and "restart" not in policy
