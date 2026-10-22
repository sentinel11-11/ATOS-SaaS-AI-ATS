"""Workspace routes: contacts."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select

from ats import workspace_schemas as commands
from ats.db import session
from ats.domain import Problem
from ats.models import (
    Attempt,
    Audit,
    BlacklistEntry,
    CallerNumber,
    CampaignItem,
    Contact,
    ContactList,
    ListMember,
    Tenant,
    WorkspaceSettings,
)
from ats.repositories import TenantRepository
from ats.security import admin, principal
from ats.workspace_queries import call_dict, calls_query, pagination
from ats.workspace_service import (
    audit,
    import_contacts,
    page,
    save_contact,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["contacts"])


@router.get("/contacts")
def contacts(
    q: str = Query("", max_length=120),
    group: str = "",
    list_id: UUID | None = None,
    blocked: bool | None = None,
    pg=Depends(pagination),
    p=Depends(principal),
    db=Depends(session),
):
    query = TenantRepository(db, p.tenant_id).query(Contact).where(~Contact.archived)
    if q:
        query = query.where(
            or_(
                Contact.name.ilike(f"%{q}%"),
                Contact.phone.contains(q),
                Contact.email.ilike(f"%{q}%"),
            )
        )
    if group:
        query = query.where(Contact.group == group)
    if blocked is not None:
        query = query.where(Contact.blocked == blocked)
    if list_id:
        TenantRepository(db, p.tenant_id).get(ContactList, list_id)
        query = query.where(
            Contact.id.in_(
                select(ListMember.contact_id).where(
                    ListMember.tenant_id == p.tenant_id, ListMember.list_id == list_id
                )
            )
        )
    return page(db, query.order_by(Contact.created_at.desc(), Contact.id), *pg)


@router.post("/contacts", status_code=201)
def contact_create(
    data: commands.ContactEdit, list_id: UUID | None = None, p=Depends(admin), db=Depends(session)
):
    row = save_contact(db, p, data)
    if list_id:
        repo = TenantRepository(db, p.tenant_id)
        repo.get(ContactList, list_id)
        repo.add(ListMember, list_id=list_id, contact_id=row.id)
    db.commit()
    return row


@router.post("/contacts/{contact_id}/complaint")
def complaint(contact_id: UUID, data: commands.Outcome, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    row = repo.get(Contact, contact_id, lock=True)
    row.blocked, row.consent = True, False
    blocked = db.scalar(repo.query(BlacklistEntry).where(BlacklistEntry.phone == row.phone))
    if blocked:
        blocked.reason = data.outcome
    else:
        repo.add(BlacklistEntry, phone=row.phone, reason=data.outcome)
    audit(db, p, "contact.complaint", row.id)
    last = db.scalar(
        repo.query(Attempt)
        .join(
            CampaignItem,
            (CampaignItem.id == Attempt.item_id) & (CampaignItem.tenant_id == Attempt.tenant_id),
        )
        .where(CampaignItem.contact_id == contact_id)
        .order_by(Attempt.created_at.desc())
        .limit(1)
    )
    if last and last.caller_number:
        number = db.scalar(
            repo.query(CallerNumber)
            .where(CallerNumber.phone == last.caller_number)
            .with_for_update()
        )
        if number:
            audit(db, p, "number.complaint", number.id)
            options = db.scalar(repo.query(WorkspaceSettings))
            threshold = (options.values if options else {}).get("auto_quarantine_on_complaints", 3)
            count = db.scalar(
                select(func.count())
                .select_from(Audit)
                .where(
                    Audit.tenant_id == p.tenant_id,
                    Audit.action == "number.complaint",
                    Audit.entity_id == number.id,
                )
            )
            if count >= threshold:
                number.quarantined = True
    db.commit()
    return {"blocked": True}


@router.put("/contacts/{contact_id}")
def contact_update(
    contact_id: UUID, data: commands.ContactEdit, p=Depends(admin), db=Depends(session)
):
    row = save_contact(db, p, data, contact_id)
    db.commit()
    return row


@router.delete("/contacts/{contact_id}", status_code=204)
def contact_archive(contact_id: UUID, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Contact, contact_id, lock=True)
    row.archived, row.blocked, row.consent = True, True, False
    audit(db, p, "contact.archived", row.id)
    db.commit()


@router.post("/contacts-import")
def contact_import(data: commands.ImportContacts, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    result = import_contacts(db, p, data)
    db.commit()
    return result


@router.get("/contacts/{contact_id}/history")
def contact_history(contact_id: UUID, p=Depends(principal), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Contact, contact_id)
    return {
        "contact": row,
        "calls": [
            call_dict(*r)
            for r in db.execute(
                calls_query(p)
                .where(Contact.id == row.id)
                .order_by(Attempt.created_at.desc())
                .limit(100)
            )
        ],
    }


@router.post("/contacts-import-file")
async def contacts_import_file(
    file: UploadFile,
    consent: bool = Form(False),
    consent_source: str = Form("", max_length=256),
    list_id: UUID | None = Form(None),
    p=Depends(admin),
    db=Depends(session),
):
    from ats.spreadsheets import spreadsheet_text

    content = await file.read(5 * 1024 * 1024 + 1)
    text = spreadsheet_text(content, file.filename or "contacts.csv")
    try:
        command = commands.ImportContacts(
            text=text, consent=consent, consent_source=consent_source, list_id=list_id
        )
    except ValidationError:
        raise Problem(422, "import_validation_failed") from None
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    report = import_contacts(db, p, command)
    db.commit()
    return report


@router.get("/lists")
def lists(p=Depends(principal), db=Depends(session)):
    return [
        {
            **jsonable_encoder(row),
            "count": db.scalar(
                select(func.count())
                .select_from(ListMember)
                .where(ListMember.tenant_id == p.tenant_id, ListMember.list_id == row.id)
            ),
        }
        for row in db.scalars(
            TenantRepository(db, p.tenant_id).query(ContactList).order_by(ContactList.name)
        )
    ]


@router.post("/lists", status_code=201)
def list_create(data: commands.ListSave, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).add(ContactList, **data.model_dump())
    db.flush()
    audit(db, p, "list.created", row.id)
    db.commit()
    return row


@router.delete("/lists/{list_id}", status_code=204)
def list_delete(list_id: UUID, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(ContactList, list_id)
    db.execute(
        delete(ListMember).where(ListMember.tenant_id == p.tenant_id, ListMember.list_id == row.id)
    )
    db.delete(row)
    audit(db, p, "list.deleted", list_id)
    db.commit()
