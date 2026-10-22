import io
import zipfile

from ats.spreadsheets import spreadsheet_text


def workbook(formula=False):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>phone</t></is></c><c r="B1" t="inlineStr"><is><t>name</t></is></c></row><row r="2"><c r="A2"><v>89990000333</v>'
            + ("<f>1+1</f>" if formula else "")
            + '</c><c r="B2" t="inlineStr"><is><t>Тест Excel</t></is></c></row></sheetData></worksheet>',
        )
    return result.getvalue()


def test_xlsx_import_requires_explicit_consent(logged):
    response = logged.post(
        "/api/v1/workspace/contacts-import-file", files={"file": ("contacts.xlsx", workbook())}
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    row = logged.get("/api/v1/workspace/contacts").json()["items"][0]
    assert row["phone"] == "+79990000333" and row["consent"] is False
    assert row["name"] == "Тест Excel"


def test_xlsx_formulas_are_not_executed_or_silently_dialed(logged):
    response = logged.post(
        "/api/v1/workspace/contacts-import-file", files={"file": ("contacts.xlsx", workbook(True))}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "spreadsheet_formulas_rejected"
    assert logged.get("/api/v1/workspace/contacts").json()["total"] == 0


def test_csv_windows_encoding():
    assert "Марина" in spreadsheet_text(
        "phone;name\n89990000333;Марина".encode("cp1251"), "contacts.csv"
    )


def test_text_import_does_not_salvage_phone_from_formula(logged):
    response = logged.post(
        "/api/v1/workspace/contacts-import",
        json={"text": "phone;name\n=+79990000333;Formula\ncall79990000444;Text"},
    )
    assert response.status_code == 200
    assert len(response.json()["rejected"]) == 2
    assert response.json()["created"] == 0
