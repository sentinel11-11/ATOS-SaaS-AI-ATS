"""Workspace routes: resources."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ats import workspace_schemas as commands
from ats.db import session
from ats.models import (
    BlacklistEntry,
    CallerNumber,
    Contact,
    Template,
    Tenant,
)
from ats.repositories import TenantRepository
from ats.security import admin, principal
from ats.workspace_queries import pagination
from ats.workspace_service import (
    audit,
    page,
    run_scenario,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["resources"])


@router.get("/templates")
def templates(p=Depends(principal), db=Depends(session)):
    return db.scalars(
        TenantRepository(db, p.tenant_id)
        .query(Template)
        .where(~Template.archived)
        .order_by(Template.created_at.desc())
    ).all()


@router.post("/templates", status_code=201)
def template_create(data: commands.TemplateSave, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).add(Template, **data.model_dump())
    db.flush()
    audit(db, p, "template.created", row.id)
    db.commit()
    return row


@router.put("/templates/{template_id}")
def template_update(
    template_id: UUID, data: commands.TemplateSave, p=Depends(admin), db=Depends(session)
):
    row = TenantRepository(db, p.tenant_id).get(Template, template_id, lock=True)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    audit(db, p, "template.updated", row.id)
    db.commit()
    return row


@router.delete("/templates/{template_id}", status_code=204)
def template_archive(template_id: UUID, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(Template, template_id, lock=True)
    row.archived = True
    audit(db, p, "template.archived", row.id)
    db.commit()


@router.post("/templates/{template_id}/test")
def template_test(
    template_id: UUID, data: commands.ScenarioTest, p=Depends(principal), db=Depends(session)
):
    row = TenantRepository(db, p.tenant_id).get(Template, template_id)
    return run_scenario(row.scenario, data.answers, data.name)


@router.get("/numbers")
def numbers(p=Depends(principal), db=Depends(session)):
    return db.scalars(
        TenantRepository(db, p.tenant_id).query(CallerNumber).order_by(CallerNumber.phone)
    ).all()


@router.post("/numbers", status_code=201)
def number_create(data: commands.NumberSave, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).add(CallerNumber, **data.model_dump())
    db.flush()
    audit(db, p, "number.created", row.id)
    db.commit()
    return row


@router.put("/numbers/{number_id}")
def number_update(
    number_id: UUID, data: commands.NumberSave, p=Depends(admin), db=Depends(session)
):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    row = TenantRepository(db, p.tenant_id).get(CallerNumber, number_id, lock=True)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    audit(db, p, "number.updated", row.id)
    db.commit()
    return row


@router.delete("/numbers/{number_id}", status_code=204)
def number_delete(number_id: UUID, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(CallerNumber, number_id, lock=True)
    audit(db, p, "number.deleted", row.id)
    db.delete(row)
    db.commit()


@router.get("/blacklist")
def blacklist(q: str = "", pg=Depends(pagination), p=Depends(principal), db=Depends(session)):
    return page(
        db,
        TenantRepository(db, p.tenant_id)
        .query(BlacklistEntry)
        .where(BlacklistEntry.phone.contains(q))
        .order_by(BlacklistEntry.created_at.desc()),
        *pg,
    )


@router.post("/blacklist", status_code=201)
def block(data: commands.BlockSave, p=Depends(admin), db=Depends(session)):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    row = db.scalar(repo.query(BlacklistEntry).where(BlacklistEntry.phone == data.phone))
    if row:
        row.reason = data.reason
    else:
        row = repo.add(BlacklistEntry, **data.model_dump())
    for contact in db.scalars(
        repo.query(Contact).where(Contact.phone == data.phone).with_for_update()
    ):
        contact.blocked, contact.consent = True, False
    db.flush()
    audit(db, p, "blacklist.added", row.id)
    db.commit()
    return row


@router.delete("/blacklist/{entry_id}", status_code=204)
def unblock(entry_id: UUID, p=Depends(admin), db=Depends(session)):
    row = TenantRepository(db, p.tenant_id).get(BlacklistEntry, entry_id, lock=True)
    db.delete(row)
    # Removing a blacklist entry does not recreate withdrawn consent.
    audit(db, p, "blacklist.removed", entry_id)
    db.commit()
