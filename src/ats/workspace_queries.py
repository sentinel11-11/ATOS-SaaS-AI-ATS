"""Tenant-scoped read projections shared by workspace routes."""

from fastapi import Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_, select

from ats.models import (
    Attempt,
    Campaign,
    CampaignItem,
    Contact,
)


def pagination(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    return offset, limit


def dated(query, column, since, until):
    if since:
        query = query.where(column >= since)
    if until:
        query = query.where(column < until)
    return query


def calls_query(p, q="", status="", campaign_id=None, since=None, until=None):
    query = (
        select(Attempt, Contact, Campaign)
        .join(
            CampaignItem,
            (CampaignItem.id == Attempt.item_id) & (CampaignItem.tenant_id == Attempt.tenant_id),
        )
        .join(
            Contact,
            (Contact.id == CampaignItem.contact_id) & (Contact.tenant_id == Attempt.tenant_id),
        )
        .join(
            Campaign,
            (Campaign.id == CampaignItem.campaign_id) & (Campaign.tenant_id == Attempt.tenant_id),
        )
        .where(Attempt.tenant_id == p.tenant_id)
    )
    if q:
        query = query.where(
            or_(
                Attempt.contact_name.ilike(f"%{q}%"),
                Attempt.phone.contains(q),
                Contact.name.ilike(f"%{q}%"),
                Contact.phone.contains(q),
            )
        )
    if status:
        query = query.where(Attempt.status == status)
    if campaign_id:
        query = query.where(Campaign.id == campaign_id)
    return dated(query, Attempt.created_at, since, until)


def call_dict(attempt, contact, campaign):
    return {
        **jsonable_encoder(attempt),
        "contact_name": attempt.contact_name or contact.name,
        "phone": attempt.phone or contact.phone,
        "contact_id": contact.id,
        "campaign_name": campaign.name,
        "campaign_id": campaign.id,
        "duration_seconds": max(0, int((attempt.ended_at - attempt.answered_at).total_seconds()))
        if attempt.ended_at and attempt.answered_at
        else 0,
    }
