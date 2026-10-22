"""Read-only SQLite snapshot import. Imported campaigns never start themselves."""

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ats.legacy_contacts import migrate_contacts
from ats.models import (
    Audit,
    BlacklistEntry,
    CallerNumber,
    Campaign,
    CampaignItem,
    Contact,
    ContactList,
    HistoricalCall,
    LegacyMap,
    ListMember,
    Operator,
    Template,
    Tenant,
    User,
)
from ats.security import hasher
from ats.workspace_schemas import Scenario


def migrate_workspace(
    factory, path, tenant_id, source_id, apply=False, source_timezone="Europe/Moscow"
):
    zone = ZoneInfo(source_timezone)
    if not source_id or len(source_id) > 64:
        raise ValueError("Use a stable source identifier up to 64 characters")
    report = {
        "contacts": migrate_contacts(factory, path, tenant_id, source_id, apply),
        "applied": apply,
        "created": {},
        "skipped": {},
        "rejected": [],
    }
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        available = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        def rows(table):
            return (
                [dict(r) for r in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]
                if table in available
                else []
            )

        with factory.begin() as db:
            db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
            mappings = {}

            def save(model, entity, legacy_id, values):
                key = (tenant_id, source_id, entity, int(legacy_id))
                existing = db.get(LegacyMap, key)
                ident = (
                    existing.new_id
                    if existing
                    else uuid5(tenant_id, f"{source_id}:{entity}:{legacy_id}")
                )
                mappings[(entity, int(legacy_id))] = ident
                if existing:
                    report["skipped"][entity] = report["skipped"].get(entity, 0) + 1
                else:
                    report["created"][entity] = report["created"].get(entity, 0) + 1
                    if apply:
                        db.add(model(id=ident, tenant_id=tenant_id, **values))
                        db.flush()
                        db.add(
                            LegacyMap(
                                tenant_id=tenant_id,
                                source_id=source_id,
                                entity=entity,
                                legacy_id=int(legacy_id),
                                new_id=ident,
                            )
                        )
                return ident

            for row in rows("users"):
                login = str(row.get("login") or "").strip().lower()[:120]
                if not login:
                    continue
                existing = db.scalar(
                    select(User).where(User.tenant_id == tenant_id, User.login == login)
                )
                if existing:
                    mappings[("user", row["id"])] = existing.id
                    continue
                role = (
                    row.get("role")
                    if row.get("role") in {"admin", "operator", "viewer"}
                    else "viewer"
                )
                save(
                    User,
                    "user",
                    row["id"],
                    {
                        "login": login,
                        "role": role,
                        "active": False,
                        "password_hash": hasher.hash(secrets.token_urlsafe(48)) if apply else "",
                    },
                )
            for row in rows("operators"):
                user_id = mappings.get(("user", row.get("user_id")))
                if not user_id:
                    continue
                if db.scalar(
                    select(Operator).where(
                        Operator.tenant_id == tenant_id, Operator.user_id == user_id
                    )
                ):
                    continue
                extension = str(row.get("ext") or "")[:20]
                save(
                    Operator,
                    "operator",
                    row["id"],
                    {
                        "user_id": user_id,
                        "name": str(row.get("name") or "")[:120],
                        "extension": extension if extension.isdigit() else "",
                        "status": "offline",
                    },
                )
            for row in rows("templates"):
                try:
                    scenario = Scenario.model_validate(
                        json.loads(row.get("scenario") or "{}")
                    ).model_dump()
                except (ValueError, TypeError):
                    report["rejected"].append(
                        {"entity": "template", "id": row["id"], "code": "invalid_scenario"}
                    )
                    continue
                save(
                    Template,
                    "template",
                    row["id"],
                    {
                        "name": str(row.get("name") or "Шаблон")[:120],
                        "text": str(row.get("text") or "")[:10000],
                        "scenario": scenario,
                    },
                )
            for row in rows("databases"):
                name = str(row.get("name") or f"База {row['id']}")[:120]
                existing = db.scalar(
                    select(ContactList).where(
                        ContactList.tenant_id == tenant_id, ContactList.name == name
                    )
                )
                if existing:
                    mappings[("list", row["id"])] = existing.id
                else:
                    save(
                        ContactList,
                        "list",
                        row["id"],
                        {"name": name, "description": str(row.get("description") or "")[:500]},
                    )
            for row in rows("campaigns"):
                try:
                    schedule = json.loads(row.get("schedule") or "{}")
                except (ValueError, TypeError):
                    schedule = {}
                start = schedule.get("start", 9)
                end = schedule.get("end", 18)
                try:
                    start, end = int(str(start).split(":")[0]), int(str(end).split(":")[0])
                    if not 0 <= start < end <= 24:
                        raise ValueError()
                except (ValueError, TypeError):
                    start, end = 9, 18
                save(
                    Campaign,
                    "campaign",
                    row["id"],
                    {
                        "name": str(row.get("name") or "Кампания")[:120],
                        "route": "sim",
                        "status": "paused",
                        "mode": row.get("flow")
                        if row.get("flow") in {"message", "agent", "operator"}
                        else "message",
                        "template_id": mappings.get(("template", row.get("template_id"))),
                        "window_start": start,
                        "window_end": end,
                        "max_channels": max(1, min(100, int(row.get("max_channels") or 1))),
                        "idempotency_key": f"legacy:{source_id}:{row['id']}"[:128],
                        "request_hash": "legacy-import",
                    },
                )
            for row in rows("contacts"):
                mapped = db.get(LegacyMap, (tenant_id, source_id, "contact", row["id"]))
                if not mapped:
                    if row["id"] in report["contacts"]["rejected_ids"]:
                        continue
                    mappings[("contact", row["id"])] = uuid5(
                        tenant_id, f"{source_id}:contact:{row['id']}"
                    )
                    continue
                mappings[("contact", row["id"])] = mapped.new_id
                contact = db.get(Contact, mapped.new_id)
                list_id = mappings.get(("list", row.get("database_id")))
                if (
                    apply
                    and list_id
                    and not db.scalar(
                        select(ListMember).where(
                            ListMember.tenant_id == tenant_id,
                            ListMember.list_id == list_id,
                            ListMember.contact_id == contact.id,
                        )
                    )
                ):
                    db.add(ListMember(tenant_id=tenant_id, list_id=list_id, contact_id=contact.id))
            for row in rows("numbers"):
                digits = "".join(
                    c for c in str(row.get("number") or row.get("phone") or "") if c.isdigit()
                )
                if len(digits) == 11 and digits[0] in "78":
                    digits = digits[1:]
                if len(digits) != 10:
                    report["rejected"].append(
                        {"entity": "number", "id": row["id"], "code": "invalid_number"}
                    )
                    continue
                if db.scalar(
                    select(CallerNumber).where(
                        CallerNumber.tenant_id == tenant_id, CallerNumber.phone == digits
                    )
                ):
                    continue
                save(
                    CallerNumber,
                    "number",
                    row["id"],
                    {
                        "phone": digits,
                        "verified": False,
                        "enabled": bool(row.get("active", True)),
                        "quarantined": bool(row.get("quarantined", False)),
                        "label": "Импортировано из старой ATS",
                    },
                )
            for row in rows("blacklist"):
                from ats.inbound import normalize_phone

                phone = normalize_phone(row.get("phone"))
                if not phone:
                    continue
                if not db.scalar(
                    select(BlacklistEntry).where(
                        BlacklistEntry.tenant_id == tenant_id, BlacklistEntry.phone == phone
                    )
                ):
                    save(
                        BlacklistEntry,
                        "blacklist",
                        row["id"],
                        {
                            "phone": phone,
                            "reason": str(row.get("reason") or "Импорт запрета")[:500],
                        },
                    )
                if apply:
                    for c in db.scalars(
                        select(Contact).where(
                            Contact.tenant_id == tenant_id, Contact.phone == phone
                        )
                    ):
                        c.blocked, c.consent = True, False
            campaign_names = {r["id"]: r for r in rows("campaigns")}
            for row in rows("calls"):
                try:
                    created = datetime.fromisoformat(str(row.get("started_at") or ""))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=zone)
                except ValueError:
                    created = datetime(1970, 1, 1, tzinfo=UTC)
                campaign = campaign_names.get(row.get("campaign_id"), {})
                save(
                    HistoricalCall,
                    "call",
                    row["id"],
                    {
                        "contact_name": str(row.get("contact_name") or "")[:120],
                        "phone": str(row.get("contact_phone") or "")[:32],
                        "campaign_name": str(campaign.get("name") or "")[:120],
                        "route": str(row.get("provider") or "legacy")[:32],
                        "direction": "in" if row.get("direction") == "in" else "out",
                        "status": str(row.get("status") or "unknown")[:32],
                        "result": str(row.get("result") or "")[:500],
                        "duration_seconds": max(0, int(row.get("duration_sec") or 0)),
                        "legacy_id": row["id"],
                        "source_id": source_id,
                        "created_at": created,
                    },
                )
            for row in rows("campaign_items"):
                campaign_id = mappings.get(("campaign", row.get("campaign_id")))
                contact_id = mappings.get(("contact", row.get("contact_id", 0)))
                if not campaign_id or not contact_id:
                    continue
                if db.scalar(
                    select(CampaignItem).where(
                        CampaignItem.tenant_id == tenant_id,
                        CampaignItem.campaign_id == campaign_id,
                        CampaignItem.contact_id == contact_id,
                    )
                ):
                    continue
                # Already processed/active items are not queued for another call during import.
                state = (
                    "queued"
                    if row.get("status") == "queued" and not row.get("attempts")
                    else "blocked"
                )
                save(
                    CampaignItem,
                    "item",
                    row["id"],
                    {"campaign_id": campaign_id, "contact_id": contact_id, "status": state},
                )
            if apply:
                db.add(Audit(tenant_id=tenant_id, action="legacy.workspace_imported"))
    finally:
        connection.close()
    return report
