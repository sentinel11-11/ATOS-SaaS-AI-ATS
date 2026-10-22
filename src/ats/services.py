import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ats.config import settings
from ats.domain import ACTIVE, Problem, next_state
from ats.models import Attempt, Audit, Campaign, CampaignItem, Contact, Inbox, Outbox, Tenant, now
from ats.repositories import TenantRepository


def create_campaign(db, principal, data, key):
    """Caller owns the transaction. Campaign + items + audit commit together."""
    tenant = db.scalar(select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update())
    repo = TenantRepository(db, principal.tenant_id)
    fingerprint = hashlib.sha256(
        json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    existing = db.scalar(repo.query(Campaign).where(Campaign.idempotency_key == key))
    if existing:
        if existing.request_hash != fingerprint:
            raise Problem(409, "idempotency_key_reused")
        return existing, False
    if data.max_channels > tenant.max_channels:
        raise Problem(422, "tenant_channel_limit")
    contacts = db.scalars(repo.query(Contact).where(Contact.id.in_(data.contact_ids))).all()
    if len(contacts) != len(data.contact_ids):
        raise Problem(404, "contacts_not_found")
    if any(not c.consent or c.blocked or not c.consent_source for c in contacts):
        raise Problem(422, "contacts_not_callable")
    campaign = repo.add(
        Campaign,
        id=uuid4(),
        idempotency_key=key,
        request_hash=fingerprint,
        **data.model_dump(exclude={"contact_ids"}),
    )
    db.flush()
    for contact in contacts:
        repo.add(CampaignItem, campaign_id=campaign.id, contact_id=contact.id)
    repo.add(Audit, actor_id=principal.user_id, action="campaign.created", entity_id=campaign.id)
    db.flush()
    return campaign, True


def validate_route(tenant_id, route):
    config = settings()
    if route == "sim":
        return
    if not config.live_calls_enabled or tenant_id != config.telephony_tenant_id:
        raise Problem(409, "live_calls_disabled_for_tenant")
    if route == "multicom_ami" and not (
        config.ami_secret.get_secret_value() and config.ami_caller_id
    ):
        raise Problem(409, "ami_not_configured")
    if route == "megafon_vats" and not all(
        (
            config.megafon_url,
            config.megafon_user,
            config.megafon_api_key.get_secret_value(),
            config.megafon_crm_token.get_secret_value(),
        )
    ):
        raise Problem(409, "megafon_not_configured")


def schedule_one(db, tenant_id):
    """Serialize allocations for a tenant, not the entire database. One attempt/item in beta."""
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update(skip_locked=True)
    )
    if not tenant:
        return False
    repo = TenantRepository(db, tenant_id)
    hour = now().astimezone(ZoneInfo(tenant.timezone)).hour
    active = db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.tenant_id == tenant_id, Attempt.status.in_(ACTIVE))
    )
    if active >= tenant.max_channels:
        return False
    start_day = (
        now()
        .astimezone(ZoneInfo(tenant.timezone))
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    today = db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.tenant_id == tenant_id, Attempt.created_at >= start_day)
    )
    if today >= tenant.daily_limit:
        return False
    for campaign in db.scalars(
        repo.query(Campaign).where(Campaign.status == "running").order_by(Campaign.created_at)
    ).all():
        if not campaign.window_start <= hour < campaign.window_end:
            continue
        count = db.scalar(
            select(func.count())
            .select_from(Attempt)
            .join(CampaignItem, CampaignItem.id == Attempt.item_id)
            .where(
                Attempt.tenant_id == tenant_id,
                CampaignItem.campaign_id == campaign.id,
                Attempt.status.in_(ACTIVE),
            )
        )
        if count >= campaign.max_channels:
            continue
        item = db.scalar(
            repo.query(CampaignItem)
            .where(CampaignItem.campaign_id == campaign.id, CampaignItem.status == "queued")
            .order_by(CampaignItem.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            if count == 0:
                campaign.status = "completed"
            continue
        contact = repo.get(Contact, item.contact_id, lock=True)
        if not contact.consent or contact.blocked:
            item.status = "blocked"
            return True
        validate_route(tenant_id, campaign.route)
        if campaign.route != "sim" and contact.phone not in settings().destinations:
            item.status = "blocked"
            repo.add(Audit, actor_id=None, entity_id=item.id, action="call.destination_blocked")
            return True
        attempt = repo.add(Attempt, id=uuid4(), item_id=item.id, route=campaign.route)
        item.status = "active"
        db.flush()
        repo.add(
            Outbox,
            kind="telephony",
            dedupe_key=f"dial:{attempt.id}",
            payload={"attempt_id": str(attempt.id)},
        )
        return True
    return False


def ingest(db, tenant_id, route, event_key, external_id, event):
    from sqlalchemy.dialects.postgresql import insert

    db.execute(
        insert(Inbox)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            route=route,
            event_key=event_key,
            external_id=external_id,
            event=event,
            status="pending",
            received_at=now(),
        )
        .on_conflict_do_nothing()
    )


def finish_attempt(db, attempt, event):
    old = attempt.status
    attempt.status = next_state(old, event)
    attempt.updated_at = now()
    if attempt.status not in {"completed", "failed"} or old == attempt.status:
        return
    repo = TenantRepository(db, attempt.tenant_id)
    item = repo.get(CampaignItem, attempt.item_id, lock=True)
    item.status = attempt.status
    repo.add(
        Outbox, kind="crm", dedupe_key=f"crm:{attempt.id}", payload={"attempt_id": str(attempt.id)}
    )
    repo.add(Audit, actor_id=None, action=f"call.{attempt.status}", entity_id=attempt.id)


def drain_inbox(db):
    row = db.scalar(
        select(Inbox)
        .where(Inbox.status.in_(["pending", "orphan"]), Inbox.available_at <= now())
        .order_by(Inbox.received_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not row:
        return False
    repo = TenantRepository(db, row.tenant_id)
    attempt = db.scalar(
        repo.query(Attempt)
        .where(Attempt.route == row.route, Attempt.external_id == row.external_id)
        .with_for_update()
    )
    if not attempt:
        # Do not starve newer events; bounded retry window handled by received_at ordering.
        from datetime import timedelta

        row.status = "unmatched" if now() - row.received_at > timedelta(minutes=5) else "orphan"
        row.available_at = now() + timedelta(seconds=5)
        return False
    finish_attempt(db, attempt, row.event)
    row.status = "processed"
    return True
