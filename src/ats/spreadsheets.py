"""Read CSV/XLSX without executing formulas, extracting paths, or loading unbounded ZIP data."""

import csv
import io
import zipfile
from xml.etree import ElementTree

from ats.domain import Problem

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def xml_part(archive, name):
    raw = archive.read(name)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise Problem(422, "spreadsheet_entities_rejected")
    return ElementTree.fromstring(raw)


def spreadsheet_text(content, filename):
    if len(content) > 5 * 1024 * 1024:
        raise Problem(413, "import_file_too_large")
    if not filename.lower().endswith(".xlsx"):
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                return content.decode("cp1251")
            except UnicodeDecodeError:
                raise Problem(422, "unsupported_text_encoding") from None
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 100 or sum(item.file_size for item in entries) > 10 * 1024 * 1024:
                raise Problem(413, "spreadsheet_unpacked_limit")
            names = archive.namelist()
            strings = []
            if "xl/sharedStrings.xml" in names:
                strings = [
                    "".join(node.itertext())
                    for node in xml_part(archive, "xl/sharedStrings.xml").findall("s:si", NS)
                ]
            sheets = sorted(
                name
                for name in names
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )
            if not sheets:
                raise Problem(422, "spreadsheet_sheet_missing")
            output = io.StringIO(newline="")
            writer = csv.writer(output, delimiter=";")
            rows = xml_part(archive, sheets[0]).findall("s:sheetData/s:row", NS)
            if len(rows) > 1001:
                raise Problem(422, "import_limit_1000")
            for row in rows:
                values = {}
                for cell in row.findall("s:c", NS):
                    if cell.find("s:f", NS) is not None:
                        raise Problem(422, "spreadsheet_formulas_rejected")
                    ref = cell.get("r", "A1")
                    letters = "".join(c for c in ref if c.isalpha()).upper()
                    column = 0
                    for c in letters:
                        column = column * 26 + ord(c) - ord("A") + 1
                    if not 1 <= column <= 50:
                        raise Problem(422, "spreadsheet_column_limit")
                    kind = cell.get("t")
                    raw = cell.findtext("s:v", default="", namespaces=NS)
                    value = (
                        strings[int(raw)]
                        if kind == "s"
                        else "".join(cell.find("s:is", NS).itertext())
                        if kind == "inlineStr" and cell.find("s:is", NS) is not None
                        else raw
                    )
                    values[column - 1] = value
                writer.writerow([values.get(i, "") for i in range(max(values, default=-1) + 1)])
            return output.getvalue()
    except (
        zipfile.BadZipFile,
        KeyError,
        IndexError,
        ValueError,
        ElementTree.ParseError,
        RuntimeError,
    ) as exc:
        raise Problem(422, "invalid_spreadsheet") from exc
