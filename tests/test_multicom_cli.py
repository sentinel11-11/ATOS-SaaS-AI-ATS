from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ats.adapters import AMI, Rejected
from ats.connections import _validate


MODULE = run_path(str(Path(__file__).parents[1] / "deploy" / "asterisk" / "build_mcm_cli_endpoints.py"))


def test_generates_per_number_endpoint_aliases():
    normalize = MODULE["normalize_number"]
    render = MODULE["render"]
    numbers = [normalize("+79690229926"), normalize("89991234567")]
    result = render(numbers)
    assert "from_user=9690229926" in result
    assert "from_user=9991234567" in result
    assert "[mcm-cli-9690229926]" in result
    assert "outbound_auth=mcm-auth" in result
    assert "aors=mcm-aor" in result
    assert "password" not in result.lower()
    assert "\\n" not in result


def test_rejects_duplicate_and_malformed_dids():
    normalize = MODULE["normalize_number"]
    render = MODULE["render"]
    with pytest.raises(ValueError):
        normalize("9;999")
    with pytest.raises(ValueError):
        render(["9690229926", "9690229926"])


def _config(prefix):
    return SimpleNamespace(
        ami_context="ats-beta",
        ami_answer_context="ats-beta-answer",
        ami_trunk="mcm",
        ami_tech="PJSIP",
        ami_cli_endpoint_prefix=prefix,
        ami_caller_id="",
    )


def test_ami_uses_selected_did_endpoint_when_enabled():
    ami = AMI(_config("mcm-cli-"), lambda _: None)
    commands = []
    ami.connect = lambda: None
    ami.action = lambda name, params, action_id=None: commands.append((name, params))
    attempt_id = uuid4()
    assert ami.originate(attempt_id, "+79255497254", caller_id="9690229926") == str(attempt_id)
    assert len(commands) == 1
    assert ("Variable", "__ATS_TRUNK=mcm-cli-9690229926") in commands[0][1]
    assert ("CallerID", "9690229926") in commands[0][1]


def test_ami_rejects_missing_caller_identity_in_rotation_mode():
    ami = AMI(_config("mcm-cli-"), lambda _: None)
    with pytest.raises(Rejected, match=""):
        ami.originate(uuid4(), "+79255497254", caller_id="")
    with pytest.raises(Rejected):
        ami.originate(uuid4(), "+79255497254", caller_id="00083819")


def test_operator_allowlist_validation():
    assert _validate("ami", "operator_number_map", {"101": ["9690229926"]}) == {
        "101": ["9690229926"]
    }
    from ats.domain import Problem

    for invalid in ({"101": ["9690229926", "9690229926"]}, {"bad": ["9690229926"]}, {"101": ["+79690229926"]}):
        with pytest.raises(Problem):
            _validate("ami", "operator_number_map", invalid)
