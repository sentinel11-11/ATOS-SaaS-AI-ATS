def test_imported_multicom_number_requires_explicit_verification(logged):
    imported = logged.post(
        "/api/v1/workspace/numbers/import-multicom",
        json={"numbers": ["+79690229926"]},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 1

    numbers = logged.get("/api/v2/numbers").json()["numbers"]
    assert len(numbers) == 1
    number = numbers[0]
    assert number["provider"] == "multicom_ami"
    assert number["active"] is False
    assert number["verified"] is False

    enable = logged.post("/api/v2/numbers/save", json={"id": number["id"], "active": True})
    assert enable.status_code == 409, enable.text
    assert enable.json()["error"] == "caller_number_not_verified"

    verify = logged.post(
        "/api/v2/numbers/save",
        json={"id": number["id"], "verified": True},
    )
    assert verify.status_code == 200, verify.text
    number = logged.get("/api/v2/numbers").json()["numbers"][0]
    assert number["verified"] is True
    assert number["active"] is False

    enable = logged.post("/api/v2/numbers/save", json={"id": number["id"], "active": True})
    assert enable.status_code == 200, enable.text
    number = logged.get("/api/v2/numbers").json()["numbers"][0]
    assert number["active"] is True
    assert number["verified"] is True

    revoke = logged.post("/api/v2/numbers/save", json={"id": number["id"], "verified": False, "active": False})
    assert revoke.status_code == 200, revoke.text
    assert logged.get("/api/v2/numbers").json()["numbers"][0]["verified"] is False
