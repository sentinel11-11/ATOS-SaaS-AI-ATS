"""Workspace use cases. Callers own transactions; no network requests under database locks."""

import csv
import io
from datetime import timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select

from ats.domain import ACTIVE, Problem
from ats.models import (
    Attempt,
    Audit,
    BlacklistEntry,
    CallerNumber,
    CallEvent,
    Campaign,
    CampaignItem,
    Contact,
    ListMember,
    Template,
    Tenant,
    now,
)
from ats.repositories import TenantRepository
from ats.schemas import phone_from_import
from ats.workspace_schemas import ContactEdit, Scenario


def audit(db, p, action, entity_id=None):
    db.add(Audit(tenant_id=p.tenant_id, actor_id=p.user_id, action=action, entity_id=entity_id))


def event(db, attempt, code, detail=""):
    db.add(CallEvent(tenant_id=attempt.tenant_id, attempt_id=attempt.id, event=code, detail=detail))


def page(db, query, offset, limit):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {"items": db.scalars(query.offset(offset).limit(limit)).all(), "total": total}


def save_contact(db, p, data, contact_id=None):
    repo = TenantRepository(db, p.tenant_id)
    values = data.model_dump()
    if db.scalar(repo.query(BlacklistEntry).where(BlacklistEntry.phone == data.phone)):
        values["blocked"] = True
    row = repo.get(Contact, contact_id, lock=True) if contact_id else repo.add(Contact, **values)
    if contact_id:
        for key, value in values.items():
            setattr(row, key, value)
    db.flush()
    audit(db, p, "contact.updated" if contact_id else "contact.created", row.id)
    return row


def import_contacts(db, p, command):
    """CSV/text import: explicit consent, preserve existing revocations, never guess consent."""
    repo = TenantRepository(db, p.tenant_id)
    if command.list_id:
        from ats.models import ContactList

        repo.get(ContactList, command.list_id)
    text = command.text.lstrip("\ufeff").strip()
    if not text:
        raise Problem(422, "import_empty")
    lines = text.splitlines()
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = list(csv.reader(lines, dialect))
    header = [v.strip().lower() for v in reader[0]]
    aliases = {"телефон": "phone", "номер": "phone", "имя": "name", "группа": "group"}
    header = [aliases.get(v, v) for v in header]
    has_header = "phone" in header
    if has_header:
        reader = reader[1:]
    if len(reader) > 1000:
        raise Problem(422, "import_limit_1000")
    report = {"created": 0, "existing": 0, "rejected": [], "list_id": command.list_id}
    for line_no, cells in enumerate(reader, 2 if has_header else 1):
        if not cells or not any(c.strip() for c in cells):
            continue
        raw = (
            dict(zip(header, cells, strict=False))
            if has_header
            else {"phone": cells[0], "name": cells[1] if len(cells) > 1 else "Без имени"}
        )
        try:
            data = ContactEdit(
                name=raw.get("name", "").strip() or "Без имени",
                phone=phone_from_import(raw.get("phone", "")),
                group=raw.get("group", ""),
                email=raw.get("email", ""),
                consent=command.consent,
                consent_source=command.consent_source,
            )
        except (ValidationError, ValueError):
            report["rejected"].append({"line": line_no, "code": "invalid_contact"})
            continue
        row = db.scalar(repo.query(Contact).where(Contact.phone == data.phone))
        if row:
            report["existing"] += 1
            # Importing a duplicate must not reactivate an archived/revoked/blocked contact.
        else:
            row = save_contact(db, p, data)
            report["created"] += 1
        if command.list_id and not db.scalar(
            repo.query(ListMember).where(
                ListMember.list_id == command.list_id, ListMember.contact_id == row.id
            )
        ):
            repo.add(ListMember, list_id=command.list_id, contact_id=row.id)
            db.flush()
    audit(db, p, "contacts.imported", command.list_id)
    return report


def lock_campaign(db, p, campaign_id):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    return TenantRepository(db, p.tenant_id).get(Campaign, campaign_id, lock=True)


def require_idle(db, campaign):
    active = db.scalar(
        select(func.count())
        .select_from(Attempt)
        .join(CampaignItem, Attempt.item_id == CampaignItem.id)
        .where(
            Attempt.tenant_id == campaign.tenant_id,
            CampaignItem.campaign_id == campaign.id,
            Attempt.status.in_(ACTIVE),
        )
    )
    if campaign.status == "running" or active:
        raise Problem(409, "pause_and_reconcile_active_calls_first")


def add_members(db, p, campaign, contact_ids):
    repo = TenantRepository(db, p.tenant_id)
    contacts = db.scalars(repo.query(Contact).where(Contact.id.in_(set(contact_ids)))).all()
    if len(contacts) != len(set(contact_ids)):
        raise Problem(404, "contacts_not_found")
    blocked = set(
        db.scalars(select(BlacklistEntry.phone).where(BlacklistEntry.tenant_id == p.tenant_id))
    )
    if any(
        c.archived
        or c.blocked
        or not c.consent
        or not c.consent_source.strip()
        or c.phone in blocked
        for c in contacts
    ):
        raise Problem(422, "contacts_not_callable")
    existing = set(
        db.scalars(
            select(CampaignItem.contact_id).where(
                CampaignItem.tenant_id == p.tenant_id, CampaignItem.campaign_id == campaign.id
            )
        )
    )
    for contact in contacts:
        if contact.id not in existing:
            repo.add(CampaignItem, campaign_id=campaign.id, contact_id=contact.id)
    if campaign.status == "completed":
        campaign.status = "paused"
    audit(db, p, "campaign.members_added", campaign.id)
    return len(set(contact_ids) - existing)


def retry_failed(db, p, campaign):
    require_idle(db, campaign)
    repo = TenantRepository(db, p.tenant_id)
    items = db.scalars(
        repo.query(CampaignItem)
        .where(CampaignItem.campaign_id == campaign.id, CampaignItem.status == "failed")
        .with_for_update()
    ).all()
    for item in items:
        item.status = "queued"
    campaign.status = "paused"
    audit(db, p, "campaign.failed_requeued", campaign.id)
    return len(items)


def select_caller(db, tenant, route):
    """Caller selection shares the tenant allocation lock; counts persisted attempts, no reset races."""
    if route != "multicom_ami":
        return ""
    numbers = db.scalars(
        select(CallerNumber)
        .where(CallerNumber.tenant_id == tenant.id)
        .order_by(CallerNumber.last_used_at.asc().nullsfirst(), CallerNumber.id)
        .with_for_update()
    ).all()
    if not numbers:
        from ats.config import settings

        return settings().ami_caller_id
    day_start = (
        now()
        .astimezone(ZoneInfo(tenant.timezone))
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    for number in numbers:
        if not number.enabled or not number.verified or number.quarantined:
            continue
        if (
            number.last_used_at
            and number.last_used_at + timedelta(seconds=number.cooldown_seconds) > now()
        ):
            continue
        count = db.scalar(
            select(func.count())
            .select_from(Attempt)
            .where(
                Attempt.tenant_id == tenant.id,
                Attempt.caller_number == number.phone,
                Attempt.created_at >= day_start,
            )
        )
        if count < number.daily_limit:
            number.last_used_at = now()
            return number.phone
    raise Problem(409, "caller_pool_exhausted")


def run_scenario(raw, answers, name="Тест"):
    scenario = Scenario.model_validate(raw).model_dump()
    transcript = [{"speaker": "agent", "text": scenario["greeting"].replace("{name}", name)}]
    if scenario["intro"]:
        transcript.append({"speaker": "agent", "text": scenario["intro"].replace("{name}", name)})
    questions = scenario["questions"]
    indices = {q["id"]: i for i, q in enumerate(questions)}
    qualified, position, steps = scenario["default_qualified"], 0, 0
    while position < len(questions):
        if steps >= 60:
            raise Problem(422, "scenario_cycle_limit")
        steps += 1
        q = questions[position]
        transcript.append({"speaker": "agent", "text": q["text"].replace("{name}", name)})
        answer = answers.get(q["id"], "")[:2000]
        transcript.append({"speaker": "contact", "text": answer})
        normalized = answer.strip().lower()
        normalized = (
            "1"
            if normalized in {"да", "yes", "1"}
            else "2"
            if normalized in {"нет", "no", "2"}
            else normalized
        )
        choice = q["choices"].get(normalized)
        if choice is None:
            # Unrecognized input never silently qualifies a contact.
            qualified = False
            break
        if choice["qualified"] is not None:
            qualified = choice["qualified"]
        if choice["next"] == "end":
            break
        position = indices[choice["next"]] if choice["next"] != "continue" else position + 1
    transcript.append(
        {
            "speaker": "agent",
            "text": scenario["qualified_text"] if qualified else scenario["not_qualified_text"],
        }
    )
    if scenario["goodbye"]:
        transcript.append({"speaker": "agent", "text": scenario["goodbye"]})
    return {"qualified": qualified, "transcript": transcript}


def validate_template(db, tenant_id, template_id):
    if template_id:
        template = TenantRepository(db, tenant_id).get(Template, template_id)
        if template.archived:
            raise Problem(422, "template_archived")


def csv_document(headers, rows):
    stream = io.StringIO(newline="")
    stream.write("\ufeff")
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(headers)
    for row in rows:
        # Spreadsheet formula injection protection also applies to imported values.
        writer.writerow(
            [
                "'" + str(v) if str(v).lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v
                for v in row
            ]
        )
    return stream.getvalue()
