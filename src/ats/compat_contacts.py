"""Legacy contact contracts backed by the tenant scoped PostgreSQL workspace.

The v2 UI remains a client of application services; it never opens the old SQLite DB.
"""

import base64
import binascii
import csv
import io
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select

from ats.api_contacts import complaint as register_complaint
from ats.db import session
from ats.domain import Problem
from ats.models import (
    Attempt,
    Audit,
    Campaign,
    CampaignItem,
    Contact,
    ContactList,
    ListMember,
    Tenant,
)
from ats.repositories import TenantRepository
from ats.schemas import phone_from_import
from ats.security import admin, principal
from ats.spreadsheets import spreadsheet_text
from ats.workspace_queries import call_dict, calls_query
from ats.workspace_schemas import ContactEdit, ImportContacts, ListSave, Outcome
from ats.workspace_service import audit, import_contacts, save_contact

router = APIRouter(prefix="/api/v2", tags=["legacy contacts"])


class ContactSave(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID | None = None
    name: str = Field(default="", max_length=120)
    phone: str = Field(max_length=64)
    consent: bool = False
    consent_source: str | None = Field(default=None, max_length=256)
    group_name: str | None = Field(default=None, max_length=120)
    group: str | None = Field(default=None, max_length=120)
    grp: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=10000)
    email: str | None = Field(default=None, max_length=254)
    database_id: UUID | None = None
    blacklisted: bool | None = None
    crm_contact_id: int | None = Field(default=None, gt=0)


class Ids(BaseModel):
    ids: list[UUID] = Field(default_factory=list, max_length=5000)
    id: UUID | None = None


class ImportFile(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    content_b64: str = Field(min_length=1, max_length=7 * 1024 * 1024)
    database_id: UUID | None = None
    database_name: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=500)
    consent_default: bool = False


class ImportRows(BaseModel):
    rows: list[ContactSave] = Field(max_length=1000)
    database_id: UUID | None = None


class DatabaseSave(BaseModel):
    id: UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=500)
    filename: str = Field(default="", max_length=300)


class DatabaseDelete(Ids):
    delete_contacts: bool = False


class Complaint(BaseModel):
    phone: str = Field(max_length=64)
    reason: str = Field(default="Жалоба / Отказ клиента", min_length=1, max_length=500)


def contact_dict(row, database=None, complaints=0):
    return {
        "id": str(row.id),
        "name": row.name,
        "phone": row.phone,
        "consent": int(row.consent),
        "consent_source": row.consent_source,
        "blacklisted": int(row.blocked),
        "group_name": row.group,
        "group": row.group,
        "grp": row.group,
        "note": row.notes,
        "email": row.email,
        "tags": getattr(row, "tags", ""),
        "database_id": str(database.id) if database else None,
        "db_name": database.name if database else "",
        "complaints": complaints,
        "crm_contact_id": row.crm_contact_id,
        "created": row.created_at.isoformat(),
    }


def legacy_call(attempt, contact, campaign):
    """Small call projection shared by cards; detail transport lives in compat_calls."""
    data = call_dict(attempt, contact, campaign)
    return {
        **data,
        "id": str(attempt.id),
        "campaign_id": str(campaign.id),
        "contact_id": str(contact.id),
        "camp_name": campaign.name,
        "contact_phone": data["phone"],
        "caller_id": attempt.caller_number,
        "provider": attempt.route,
        "started_at": attempt.created_at.isoformat(),
        "ended_at": attempt.ended_at.isoformat() if attempt.ended_at else "",
        "answered_at": attempt.answered_at.isoformat() if attempt.answered_at else "",
        "duration_sec": data["duration_seconds"],
        "detail": attempt.reason,
    }


@router.get("/contacts")
def contacts(
    q: str = Query("", max_length=120),
    offset: int = Query(0, ge=0),
    limit: int = Query(5000, ge=1, le=5000),
    p=Depends(principal),
    db=Depends(session),
):
    repo = TenantRepository(db, p.tenant_id)
    query = repo.query(Contact).where(~Contact.archived)
    if q:
        query = query.where(or_(Contact.name.ilike(f"%{q}%"), Contact.phone.contains(q)))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Contact.created_at.desc(), Contact.id).offset(offset).limit(limit)).all()
    # Fetch memberships/counts in bulk: no per-row SQL for a 5,000-contact legacy page.
    ids = [r.id for r in rows]
    memberships = db.execute(
        select(ListMember.contact_id, ContactList)
        .join(ContactList, (ListMember.list_id == ContactList.id) & (ListMember.tenant_id == ContactList.tenant_id))
        .where(ListMember.tenant_id == p.tenant_id, ListMember.contact_id.in_(ids))
        .order_by(ContactList.created_at, ContactList.id)
    ).all() if ids else []
    databases = {}
    for cid, database in memberships:
        databases.setdefault(cid, database)
    complaints = dict(db.execute(
        select(Audit.entity_id, func.count()).where(
            Audit.tenant_id == p.tenant_id, Audit.action == "contact.complaint", Audit.entity_id.in_(ids)
        ).group_by(Audit.entity_id)
    ).all()) if ids else {}
    return {"ok": True, "contacts": [contact_dict(c, databases.get(c.id), complaints.get(c.id, 0)) for c in rows], "total": total, "offset": offset, "limit": limit}


def _save(db, p, data):
    repo = TenantRepository(db, p.tenant_id)
    phone = phone_from_import(data.phone)
    row = repo.get(Contact, data.id, lock=True) if data.id else db.scalar(repo.query(Contact).where(Contact.phone == phone).with_for_update())
    existing = row is not None
    # The legacy checkbox records an explicit manual action. Preserve the source on edits.
    source = data.consent_source if data.consent_source is not None else (row.consent_source if row and row.consent_source else f"manual:{p.login}")
    command = ContactEdit(
        name=data.name.strip() or (row.name if row else "Без имени"), phone=phone,
        consent=data.consent, consent_source=source,
        group=next((v for v in (data.group_name, data.group, data.grp) if v is not None), row.group if row else ""),
        notes=data.note if data.note is not None else row.notes if row else "",
        email=data.email if data.email is not None else row.email if row else "",
        blocked=data.blacklisted if data.blacklisted is not None else row.blocked if row else False,
        crm_contact_id=data.crm_contact_id if data.crm_contact_id is not None else row.crm_contact_id if row else None,
    )
    row = save_contact(db, p, command, row.id if row else None)
    if data.database_id:
        repo.get(ContactList, data.database_id)
        member = db.scalar(repo.query(ListMember).where(ListMember.list_id == data.database_id, ListMember.contact_id == row.id))
        if not member:
            repo.add(ListMember, list_id=data.database_id, contact_id=row.id)
    return row, existing


@router.post("/contacts")
@router.post("/contacts/save")
def contact_save(data: ContactSave, p=Depends(admin), db=Depends(session)):
    # Lock once for race-free phone upsert and consent changes, matching scheduler allocation.
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    try:
        row, existing = _save(db, p, data)
    except ValueError:
        raise Problem(422, "bad_phone") from None
    db.commit()
    return {"ok": True, "id": str(row.id), "updated_existing": str(row.id) if existing and not data.id else False}


@router.post("/contacts/delete")
def contact_delete(data: Ids, p=Depends(admin), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    ids = set(data.ids or ([data.id] if data.id else []))
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    rows = db.scalars(repo.query(Contact).where(Contact.id.in_(ids)).with_for_update()).all()
    if len(rows) != len(ids):
        raise Problem(404, "not_found")
    for row in rows:
        row.archived, row.consent, row.blocked = True, False, True
        audit(db, p, "contact.archived", row.id)
    db.commit()
    return {"ok": True, "deleted": len(rows)}


@router.get("/contacts/history")
def contact_history(id: UUID, p=Depends(principal), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    contact = repo.get(Contact, id)
    calls = [legacy_call(*row) for row in db.execute(calls_query(p).where(Contact.id == id).order_by(Attempt.created_at.desc()).limit(200))]
    items = db.execute(
        select(CampaignItem, Campaign).join(Campaign, (Campaign.id == CampaignItem.campaign_id) & (Campaign.tenant_id == CampaignItem.tenant_id))
        .where(CampaignItem.tenant_id == p.tenant_id, CampaignItem.contact_id == id)
        .order_by(Campaign.created_at.desc()).limit(200)
    ).all()
    done = [c for c in calls if c["ended_at"]]
    last = done[0] if done else calls[0] if calls else {}
    database = db.scalar(repo.query(ContactList).join(ListMember, (ListMember.list_id == ContactList.id) & (ListMember.tenant_id == ContactList.tenant_id)).where(ListMember.contact_id == id).limit(1))
    return {"ok": True, "contact": contact_dict(contact, database), "calls": calls,
            "items": [{"id": str(i.id), "campaign_id": str(c.id), "camp_name": c.name, "status": i.status} for i, c in items],
            "stats": {"calls_total": len(calls), "calls_done": len(done), "last_result": last.get("result", ""), "last_at": last.get("ended_at") or last.get("started_at", "")}}


def _report(report):
    return {
        "ok": True,
        "added": report["created"],
        "updated": report["existing"],
        "skipped": len(report["rejected"]),
        "errors": [{"row": r["line"], "reason": r["code"]} for r in report["rejected"][:50]],
        "errors_total": len(report["rejected"]),
    }


@router.post("/contacts/import-file")
def contacts_import_file(data: ImportFile, p=Depends(admin), db=Depends(session)):
    try:
        raw = base64.b64decode(data.content_b64, validate=True)
    except (ValueError, binascii.Error):
        raise Problem(422, "bad_base64") from None
    text = spreadsheet_text(raw, data.filename)
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    if data.database_id:
        database = repo.get(ContactList, data.database_id)
    else:
        name = data.database_name or data.filename.replace("\\", "/").rsplit("/", 1)[-1][:120]
        database = db.scalar(repo.query(ContactList).where(ContactList.name == name))
        if not database:
            database = repo.add(ContactList, name=name, description=data.notes)
            db.flush()
    report = import_contacts(db, p, ImportContacts(text=text, list_id=database.id, consent=data.consent_default, consent_source=f"file:{data.filename}"[:256]))
    db.commit()
    return {**_report(report), "database_id": str(database.id), "database": {"id": str(database.id), "name": database.name}}


@router.post("/contacts/import")
def contacts_import(data: ImportRows, p=Depends(admin), db=Depends(session)):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["phone", "name", "group"])
    # Consent is evaluated per row, not silently inferred from a file column.
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    if data.database_id:
        repo.get(ContactList, data.database_id)
    report = {"ok": True, "added": 0, "updated": 0, "skipped": 0, "errors": []}
    for index, value in enumerate(data.rows, 1):
        try:
            with db.begin_nested():
                row, existing = _save(db, p, value.model_copy(update={"database_id": data.database_id or value.database_id}))
                report["updated" if existing else "added"] += 1
        except ValueError:
            report["skipped"] += 1
            report["errors"].append({"row": index, "reason": "invalid_contact"})
    report["errors_total"] = len(report["errors"])
    report["errors"] = report["errors"][:50]
    db.commit()
    return report


@router.get("/databases")
def databases(p=Depends(principal), db=Depends(session)):
    counts = select(ListMember.list_id, func.count().label("count")).where(ListMember.tenant_id == p.tenant_id).group_by(ListMember.list_id).subquery()
    rows = db.execute(select(ContactList, counts.c.count).outerjoin(counts, counts.c.list_id == ContactList.id).where(ContactList.tenant_id == p.tenant_id).order_by(ContactList.created_at.desc())).all()
    return {"ok": True, "databases": [{"id": str(d.id), "name": d.name, "notes": d.description, "created": d.created_at.isoformat(), "contacts_count": count or 0, "count": count or 0} for d, count in rows]}


@router.post("/databases/save")
def database_save(data: DatabaseSave, p=Depends(admin), db=Depends(session)):
    values = ListSave(name=data.name, description=data.notes)
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(ContactList, data.id, lock=True) if data.id else repo.add(ContactList, **values.model_dump())
    row.name, row.description = values.name, values.description
    db.flush()
    audit(db, p, "list.saved", row.id)
    db.commit()
    return {"ok": True, "id": str(row.id)}


@router.post("/databases/delete")
def database_delete(data: DatabaseDelete, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    for identity in data.ids or ([data.id] if data.id else []):
        row = repo.get(ContactList, identity, lock=True)
        if data.delete_contacts:
            members = select(ListMember.contact_id).where(ListMember.tenant_id == p.tenant_id, ListMember.list_id == row.id)
            for contact in db.scalars(repo.query(Contact).where(Contact.id.in_(members)).with_for_update()):
                contact.archived, contact.consent, contact.blocked = True, False, True
                audit(db, p, "contact.archived", contact.id)
        db.execute(delete(ListMember).where(ListMember.tenant_id == p.tenant_id, ListMember.list_id == row.id))
        db.delete(row)
        audit(db, p, "list.deleted", row.id)
    db.commit()
    return {"ok": True}


@router.post("/complaint")
def complaint(data: Complaint, p=Depends(admin), db=Depends(session)):
    try:
        phone = phone_from_import(data.phone)
    except ValueError:
        raise Problem(422, "bad_phone") from None
    row = db.scalar(TenantRepository(db, p.tenant_id).query(Contact).where(Contact.phone == phone))
    if not row:
        raise Problem(404, "not_found")
    register_complaint(contact_id=row.id, data=Outcome(outcome=data.reason), p=p, db=db)
    return {"ok": True}


@router.get("/export/contacts.csv")
def contacts_csv(p=Depends(principal), db=Depends(session)):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Имя", "Телефон", "Группа", "Согласие", "Примечание"])
    # Constant-memory DB iteration; formula-like text cannot become a spreadsheet command.
    def safe(value):
        value = str(value)
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for row in db.scalars(TenantRepository(db, p.tenant_id).query(Contact).where(~Contact.archived).order_by(Contact.id).execution_options(yield_per=500)):
        writer.writerow([safe(row.name), safe(row.phone), safe(row.group), int(row.consent), safe(row.notes)])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="contacts.csv"'})
