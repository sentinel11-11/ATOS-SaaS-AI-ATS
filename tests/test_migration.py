import sqlite3

from sqlalchemy import select

from ats.cli import migrate_contacts
from ats.models import Contact, LegacyMap


def test_readonly_import_dry_run_repeatability_and_consent(factory, tenants, tmp_path):
    path = tmp_path / "snapshot.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE contacts(id INTEGER, name TEXT, phone TEXT, consent INTEGER, consent_source TEXT, blacklisted INTEGER)"
        )
        db.executemany(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            [
                (1, "A", "89990000101", 1, "Written", 0),
                (2, "B", "+79990000202", 1, "", 0),
                (3, "C", "bad", 1, "Written", 0),
                (4, "D", "89990000303", 1, "Written", 1),
            ],
        )
    original = path.read_bytes()
    report = migrate_contacts(factory, path, tenants[0], "source-1")
    assert report["valid"] == 3 and report["rejected_ids"] == [3] and report["consent_reset"] == 1
    with factory() as db:
        assert db.scalars(select(Contact)).all() == []
    migrate_contacts(factory, path, tenants[0], "source-1", apply=True)
    report = migrate_contacts(factory, path, tenants[0], "source-1", apply=True)
    assert report["already_imported"] == 3
    with factory() as db:
        contacts = db.scalars(select(Contact).order_by(Contact.phone)).all()
        assert contacts[0].phone == "+79990000101" and contacts[0].consent
        assert not contacts[1].consent and contacts[2].blocked
        assert len(db.scalars(select(LegacyMap)).all()) == 3
    assert original == path.read_bytes()
