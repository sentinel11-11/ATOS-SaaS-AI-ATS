import json
import subprocess
import sys

from sqlalchemy import select

from ats.cli import diagnose_multicom
from ats.models import Attempt, ConnectionProfile
from ats.workers import Worker


def test_diagnostic_uses_saved_ami_profile_and_omits_secret_values(factory, prepared, tenants):
    with factory.begin() as db:
        db.add(ConnectionProfile(
            tenant_id=tenants[0], provider="ami", public_values={
                "trunk": "configured-mcm", "cli_endpoint_prefix": "mcm-cli-",
                "context": "ats-custom", "answer_context": "ats-custom-answer",
            },
        ))
    Worker(factory).schedule()
    with factory() as db:
        attempt_id = db.scalar(select(Attempt.id))
    report = diagnose_multicom(factory, attempt_id)
    assert report["ami"]["trunk"] == "configured-mcm"
    assert report["ami"]["selected_endpoint"] == "mcm-cli-9990000101"
    assert report["ami"]["secret_set"] is True
    assert report["asterisk_commands"] == [
        "dialplan show ats-custom", "dialplan show ats-custom-answer",
        "pjsip show endpoint mcm-cli-9990000101",
    ]
    assert "fixture-secret" not in json.dumps(report)
    command = subprocess.run(
        [sys.executable, "-m", "ats.cli", "diagnose-multicom", str(attempt_id)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(command.stdout) == report
    assert "fixture-secret" not in command.stdout
