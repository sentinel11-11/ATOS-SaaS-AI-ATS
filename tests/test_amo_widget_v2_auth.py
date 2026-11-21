"""Signature verification for zero-click amoCRM widget identity."""

import base64
import hashlib
import hmac
import json

import pytest

from ats.amo_widget_v2_auth import verify_amo_widget_token
from ats.domain import Problem

KEY = "a-test-signing-secret-only-not-used-in-production"
PARAMS = {
    "secret": KEY,
    "account_id": 1234,
    "client_uuid": "abcd-efgh",
    "audience": "https://axiomats.ru",
    "issuer": "https://example.amocrm.ru",
    "origin": "https://example.amocrm.ru",
    "clock": 100000,
}


def _segment(value):
    data = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign(claims, secret=KEY):
    unsigned = _segment({"alg": "HS256", "typ": "JWT"}) + "." + _segment(claims)
    signature = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()
    return unsigned + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def claims():
    return {
        "iat": 99990,
        "nbf": 99990,
        "exp": 100200,
        "jti": "unique-token-1",
        "iss": PARAMS["issuer"],
        "aud": PARAMS["audience"],
        "client_uuid": PARAMS["client_uuid"],
        "account_id": PARAMS["account_id"],
        "user_id": 1001,
    }


def test_accepts_valid_amo_sdk_token():
    assert verify_amo_widget_token(sign(claims()), **PARAMS)["user_id"] == 1001


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", 0),
        ("account_id", 9999),
        ("aud", "https://elsewhere.invalid"),
        ("iss", "https://other.amocrm.ru"),
        ("client_uuid", "different"),
        ("exp", 99999),
        ("nbf", 100500),
        ("iat", 100500),
    ],
)
def test_rejects_invalid_identity_or_expired_token(field, value):
    payload = claims()
    payload[field] = value
    with pytest.raises(Problem):
        verify_amo_widget_token(sign(payload), **PARAMS)


def test_rejects_wrong_signature_or_origin():
    with pytest.raises(Problem):
        verify_amo_widget_token(sign(claims(), secret="wrong-secret"), **PARAMS)
    with pytest.raises(Problem):
        verify_amo_widget_token(
            sign(claims()), **{**PARAMS, "origin": "https://other.amocrm.ru"}
        )


def test_rejects_unconfigured_server_credentials():
    with pytest.raises(Problem) as exc:
        verify_amo_widget_token(sign(claims()), **{**PARAMS, "secret": ""})
    assert exc.value.code == "amo_widget_auth_not_configured"
