"""Workspace routes: campaigns."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from ats import workspace_schemas as commands
from ats.db import session
from ats.domain import Problem
from ats.models import (
    Campaign,
    CampaignItem,
    Contact,
    Tenant,
)
from ats.repositories import TenantRepository
from ats.security import admin, principal
from ats.workspace_queries import pagination
from ats.workspace_service import (
    add_members,
    audit,
    lock_campaign,
    page,
    require_idle,
    retry_failed,
    validate_template,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["campaigns"])


@router.get("/campaigns")
def campaigns(
    q: str = Query("", max_length=120),
    pg=Depends(pagination),
    p=Depends(principal),
    db=Depends(session),
):
    return page(
        db,
        TenantRepository(db, p.tenant_id)
        .query(Campaign)
        .where(~Campaign.archived, Campaign.name.ilike(f"%{q}%"))
        .order_by(Campaign.created_at.desc(), Campaign.id),
        *pg,
    )


@router.get("/campaigns/{campaign_id}")
def campaign_details(campaign_id: UUID, p=Depends(principal), db=Depends(session)):
    campaign = TenantRepository(db, p.tenant_id).get(Campaign, campaign_id)
    rows = db.execute(
        select(CampaignItem, Contact)
        .join(
            Contact,
            (Contact.id == CampaignItem.contact_id) & (Contact.tenant_id == CampaignItem.tenant_id),
        )
        .where(CampaignItem.tenant_id == p.tenant_id, CampaignItem.campaign_id == campaign.id)
        .order_by(CampaignItem.id)
        .limit(1000)
    )
    return {
        "campaign": campaign,
        "items": [{**jsonable_encoder(item), "name": c.name, "phone": c.phone} for item, c in rows],
        "counts": dict(
            db.execute(
                select(CampaignItem.status, func.count())
                .where(
                    CampaignItem.tenant_id == p.tenant_id, CampaignItem.campaign_id == campaign.id
                )
                .group_by(CampaignItem.status)
            ).all()
        ),
    }


@router.put("/campaigns/{campaign_id}")
def campaign_update(
    campaign_id: UUID, data: commands.CampaignEdit, p=Depends(admin), db=Depends(session)
):
    row = lock_campaign(db, p, campaign_id)
    require_idle(db, row)
    validate_template(db, p.tenant_id, data.template_id)
    if data.max_channels > db.get(Tenant, p.tenant_id).max_channels:
        raise Problem(422, "tenant_channel_limit")
    for key, value in data.model_dump(exclude={"contact_ids"}).items():
        setattr(row, key, value)
    audit(db, p, "campaign.updated", row.id)
    db.commit()
    return row


@router.post("/campaigns/{campaign_id}/members")
def campaign_members(
    campaign_id: UUID, data: commands.Members, p=Depends(admin), db=Depends(session)
):
    row = lock_campaign(db, p, campaign_id)
    added = add_members(db, p, row, data.contact_ids)
    db.commit()
    return {"added": added}


@router.post("/campaigns/{campaign_id}/retry-failed")
def campaign_retry(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    count = retry_failed(db, p, row)
    db.commit()
    return {"queued": count}


@router.post("/campaigns/{campaign_id}/clear-queue")
def campaign_clear(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    require_idle(db, row)
    items = db.scalars(
        TenantRepository(db, p.tenant_id)
        .query(CampaignItem)
        .where(CampaignItem.campaign_id == row.id, CampaignItem.status == "queued")
        .with_for_update()
    ).all()
    for item in items:
        item.status = "cancelled"
    audit(db, p, "campaign.queue_cleared", row.id)
    db.commit()
    return {"cancelled": len(items)}


@router.delete("/campaigns/{campaign_id}", status_code=204)
def campaign_archive(campaign_id: UUID, p=Depends(admin), db=Depends(session)):
    row = lock_campaign(db, p, campaign_id)
    require_idle(db, row)
    row.archived = True
    audit(db, p, "campaign.archived", row.id)
    db.commit()
