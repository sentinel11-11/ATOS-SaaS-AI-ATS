"""Contact-only snapshot import retained for incremental migration."""

import sqlite3
from pathlib import Path
from uuid import uuid5

from pydantic import ValidationError
from sqlalchemy import select

from ats.models import Contact, LegacyMap, Tenant
from ats.schemas import ContactCreate


def migrate_contacts(factory, path, tenant_id, source_id, apply=False):
    """Import a sqlite3.backup snapshot. No source writes, credential copying or consent guessing."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    report = {
        "valid": 0,
        "rejected_ids": [],
        "already_imported": 0,
        "consent_reset": 0,
        "applied": apply,
    }
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        rows = connection.execute("SELECT * FROM contacts ORDER BY id").fetchall()
        with factory.begin() as db:
            if not db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update()):
                raise ValueError("Create the destination tenant first")
            for row in rows:
                raw = dict(row)
                legacy_id = int(raw["id"])
                key = (tenant_id, source_id, "contact", legacy_id)
                if db.get(LegacyMap, key):
                    report["already_imported"] += 1
                    continue
                digits = "".join(c for c in str(raw.get("phone", "")) if c.isdigit())
                if len(digits) == 11 and digits.startswith("8"):
                    digits = "7" + digits[1:]
                source = str(raw.get("consent_source") or "").strip()
                consent = raw.get("consent") == 1 and bool(source)
                if raw.get("consent") == 1 and not source:
                    report["consent_reset"] += 1
                try:
                    data = ContactCreate(
                        name=raw.get("name") or "Без имени",
                        phone="+" + digits,
                        consent=consent,
                        consent_source=source,
                    )
                except ValidationError:
                    report["rejected_ids"].append(legacy_id)
                    continue
                report["valid"] += 1
                if apply:
                    contact = db.scalar(
                        select(Contact).where(
                            Contact.tenant_id == tenant_id, Contact.phone == data.phone
                        )
                    )
                    if contact:
                        # Merge conservatively: one blocked/revoked entry vetoes calling.
                        contact.consent = contact.consent and consent
                        contact.blocked = contact.blocked or bool(raw.get("blacklisted"))
                    else:
                        contact = Contact(
                            id=uuid5(tenant_id, f"{source_id}:contact:{legacy_id}"),
                            tenant_id=tenant_id,
                            blocked=bool(raw.get("blacklisted")),
                            group=str(raw.get("grp") or "")[:120],
                            notes=str(raw.get("note") or "")[:10000],
                            **data.model_dump(),
                        )
                        db.add(contact)
                        db.flush()
                    db.add(
                        LegacyMap(
                            tenant_id=tenant_id,
                            source_id=source_id,
                            entity="contact",
                            legacy_id=legacy_id,
                            new_id=contact.id,
                        )
                    )
    finally:
        connection.close()
    return report
