import subprocess
from pathlib import Path


def test_updater_recovers_partial_virtualenv_before_checking_application_imports():
    root = Path(__file__).resolve().parents[1]
    updater = root / "deploy/ubuntu/update-beta.sh"
    fixture = root / "tests/fixtures/update-recovery.sh"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-i", "--network", "none",
            "--mount", f"type=bind,src={updater},dst=/input/update-beta.sh,readonly",
            "--entrypoint", "bash", "postgres:17-alpine", "-s",
        ],
        input=fixture.read_text(), text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Update recovery, ownership, symlink preservation and database guards passed" in result.stdout
