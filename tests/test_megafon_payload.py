import asyncio

import pytest
from starlette.requests import Request

from ats.api import _megafon_request_payload
from ats.domain import Problem


def parse_body(content_type, body):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/megafon/events",
            "headers": [(b"content-type", content_type.encode("ascii"))],
        },
        receive=receive,
    )
    return asyncio.run(_megafon_request_payload(request))


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", b'{"cmd":"history","status":"Success","callid":"abc"}'),
        ("application/json; charset=utf-8", b'{"cmd":"history","status":"Success","callid":"abc"}'),
        ("application/x-www-form-urlencoded", b"cmd=history&status=Success&callid=abc"),
        ("application/x-www-form-urlencoded; charset=UTF-8", b"cmd=history&status=Success&callid=abc"),
        (
            "multipart/form-data; boundary=test",
            b"--test\r\nContent-Disposition: form-data; name=\"cmd\"\r\n\r\nhistory\r\n"
            b"--test\r\nContent-Disposition: form-data; name=\"status\"\r\n\r\nSuccess\r\n"
            b"--test\r\nContent-Disposition: form-data; name=\"callid\"\r\n\r\nabc\r\n--test--\r\n",
        ),
    ],
)
def test_megafon_payload_formats(content_type, body):
    assert parse_body(content_type, body) == {
        "cmd": "history",
        "status": "Success",
        "callid": "abc",
    }


@pytest.mark.parametrize(
    ("content_type", "body", "status", "code"),
    [
        ("application/json", b"{", 400, "invalid_json"),
        ("application/json", b"[]", 400, "invalid_payload"),
        ("text/plain", b"cmd=history", 415, "unsupported_content_type"),
    ],
)
def test_megafon_payload_rejections(content_type, body, status, code):
    with pytest.raises(Problem) as exc:
        parse_body(content_type, body)
    assert exc.value.status == status
    assert exc.value.code == code
