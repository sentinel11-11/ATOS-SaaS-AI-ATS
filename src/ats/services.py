import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ats.config import settings
from ats.domain import ACTIVE, Problem, next_state
from ats.models import (
    Attempt,
    Audit,
    BlacklistEntry,
    CallerNumber,
    Campaign,
    CampaignItem,
    Contact,
    Inbox,
    Operator,
    Outbox,
    QueueEntry,
    Tenant,
    User,
    now,
)
from ats.repositories import TenantRepository


def create_campaign(db, principal, data, key):
    """Caller owns the transaction. Campaign + items + audit commit together."""
    tenant = db.scalar(select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update())
    repo = TenantRepository(db, principal.tenant_id)
    from ats.workspace_service import validate_template

    validate_template(db, principal.tenant_id, data.template_id)
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
    blocked = set(
        db.scalars(
            select(BlacklistEntry.phone).where(BlacklistEntry.tenant_id == principal.tenant_id)
        )
    )
    if any(
        not c.consent
        or c.blocked
        or c.archived
        or not c.consent_source.strip()
        or c.phone in blocked
        for c in contacts
    ):
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


def validate_route(tenant_id, route, db=None):
    if db is not None:
        from ats.connections import settings_for_tenant

        config = settings_for_tenant(db, tenant_id)
    else:
        config = settings()
    if route == "sim":
        return
    if not config.live_calls_enabled or tenant_id != config.telephony_tenant_id:
        raise Problem(409, "live_calls_disabled_for_tenant")
    if route == "multicom_ami":
        pool = (
            db.scalar(
                select(CallerNumber.id)
                .where(
                    CallerNumber.tenant_id == tenant_id,
                    CallerNumber.enabled,
                    CallerNumber.verified,
                    ~CallerNumber.quarantined,
                )
                .limit(1)
            )
            if db is not None
            else None
        )
        if not config.ami_secret.get_secret_value() or not (config.ami_caller_id or pool):
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
        repo.query(Campaign)
        .where(Campaign.status == "running", ~Campaign.archived)
        .order_by(Campaign.created_at)
    ).all():
        if (
            not campaign.window_start <= hour < campaign.window_end
            or now().astimezone(ZoneInfo(tenant.timezone)).weekday() not in campaign.weekdays
        ):
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
            .where(
                CampaignItem.campaign_id == campaign.id,
                CampaignItem.status == "queued",
                CampaignItem.available_at <= now(),
            )
            .order_by(CampaignItem.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            pending = db.scalar(
                select(func.count())
                .select_from(CampaignItem)
                .where(
                    CampaignItem.tenant_id == tenant_id,
                    CampaignItem.campaign_id == campaign.id,
                    CampaignItem.status == "queued",
                )
            )
            if count == 0 and not pending:
                campaign.status = "completed"
            continue
        contact = repo.get(Contact, item.contact_id, lock=True)
        blacklisted = db.scalar(
            repo.query(BlacklistEntry).where(BlacklistEntry.phone == contact.phone)
        )
        if not contact.consent or contact.blocked or contact.archived or blacklisted:
            item.status = "blocked"
            return True
        validate_route(tenant_id, campaign.route, db)
        if (
            campaign.route == "multicom_ami"
            and (campaign.mode != "message" or campaign.template_id)
            and not settings().media_enabled
        ):
            raise Problem(409, "media_not_configured")
        operator = None
        if campaign.route == "multicom_ami" and (
            campaign.mode == "operator"
            or (campaign.mode == "agent" and campaign.connect_on_qualify)
        ):
            operator = db.scalar(
                repo.query(Operator)
                .join(User, (User.id == Operator.user_id) & (User.tenant_id == Operator.tenant_id))
                .where(
                    Operator.status == "free",
                    Operator.extension != "",
                    User.active,
                    User.role == "operator",
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not operator:
                return False
        if campaign.route != "sim" and contact.phone not in settings().destinations:
            item.status = "blocked"
            repo.add(Audit, actor_id=None, entity_id=item.id, action="call.destination_blocked")
            return True
        from ats.workspace_service import event, select_caller

        try:
            caller_number = select_caller(db, tenant, campaign.route)
        except Problem as exc:
            if exc.code == "caller_pool_exhausted":
                return False
            raise
        ordinal = 1 + (
            db.scalar(
                select(func.max(Attempt.ordinal)).where(
                    Attempt.tenant_id == tenant_id, Attempt.item_id == item.id
                )
            )
            or 0
        )
        attempt = repo.add(
            Attempt,
            id=uuid4(),
            item_id=item.id,
            route=campaign.route,
            ordinal=ordinal,
            caller_number=caller_number,
            phone=contact.phone,
            contact_name=contact.name,
        )
        item.status = "active"
        db.flush()
        event(db, attempt, "prepared")
        if operator:
            operator.status = "busy"
            repo.add(QueueEntry, attempt_id=attempt.id, operator_id=operator.id, status="accepted")
        repo.add(
            Outbox,
            kind="telephony",
            dedupe_key=f"dial:{attempt.id}",
            payload={"attempt_id": str(attempt.id)},
        )
        return True
    return False


def ingest(db, tenant_id, route, event_key, external_id, event, payload=None):
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
            payload=payload or {},
            status="pending",
            received_at=now(),
        )
        .on_conflict_do_nothing()
    )


def finish_attempt(db, attempt, event, reason=""):
    old = attempt.status
    attempt.status = next_state(old, event)
    attempt.updated_at = now()
    if reason:
        attempt.reason = reason
    from ats.workspace_service import event as log_event

    if old != attempt.status:
        log_event(db, attempt, attempt.status)
    if attempt.status == "answered" and attempt.answered_at is None:
        attempt.answered_at = now()
    if attempt.status not in {"completed", "failed"} or old == attempt.status:
        return
    repo = TenantRepository(db, attempt.tenant_id)
    item = repo.get(CampaignItem, attempt.item_id, lock=True)
    item.status = attempt.status
    attempt.ended_at = now()
    campaign = repo.get(Campaign, item.campaign_id)
    if (
        attempt.status == "failed"
        and reason in {"connect_failed", "busy", "no_answer"}
        and attempt.ordinal <= campaign.retry_max
    ):
        from datetime import timedelta

        item.status = "queued"
        item.available_at = now() + timedelta(minutes=campaign.retry_delay_minutes)
    queue = db.scalar(repo.query(QueueEntry).where(QueueEntry.attempt_id == attempt.id))
    if queue and queue.operator_id:
        operator = repo.get(Operator, queue.operator_id, lock=True)
        operator.status = "free"
        queue.status = "completed"
    if (
        queue is None
        and attempt.status == "completed"
        and (campaign.mode == "operator" or attempt.agent_result.get("qualified"))
    ):
        repo.add(QueueEntry, attempt_id=attempt.id)
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
    if "agent_result" in row.payload:
        attempt.agent_result = row.payload["agent_result"]
    finish_attempt(db, attempt, row.event, row.payload.get("reason", ""))
    row.status = "processed"
    return True
