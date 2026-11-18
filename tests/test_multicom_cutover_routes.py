import pytest

from ats.api_amo import _call_route
from ats.domain import Problem


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, "megafon_vats"),
        ({"_legacy_basic": {"provider": "multicom"}}, "multicom_ami"),
        ({"_legacy_basic": {"provider": "ami"}}, "multicom_ami"),
        ({"provider": "multicom_ami"}, "multicom_ami"),
        ({"route": "multicom_ami"}, "multicom_ami"),
        ({"_legacy_basic": {"provider": "megafon_vats"}}, "megafon_vats"),
    ],
)
def test_call_route_follows_workspace_provider(config, expected):
    assert _call_route(config) == expected


def test_stale_widget_route_cannot_override_multicom():
    with pytest.raises(Problem) as exc:
        _call_route({"_legacy_basic": {"provider": "multicom"}}, "megafon_vats")
    assert exc.value.code == "telephony_route_mismatch"


def test_unsupported_provider_fails_closed():
    with pytest.raises(Problem) as exc:
        _call_route({"_legacy_basic": {"provider": "uis"}})
    assert exc.value.code == "telephony_provider_not_supported"
