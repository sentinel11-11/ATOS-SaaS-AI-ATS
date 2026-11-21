import hashlib
import json
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ats.config import settings
from ats.domain import (
    ACTIVE,
    EVIDENCE_REQUIRED,
    MISSING_AMI_TERMINAL,
    TERMINAL,
    Problem,
    next_state,
)
from ats.models import (
    Attempt,
    Audit,
    BlacklistEntry,
    CallerNumber,
    CallLeg,
    Campaign,
    CampaignItem,
    Contact,
    HistoricalCall,
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


def validate_route(
    tenant_id, route, db=None, *, require_megafon_user=True, mode=None, template_id=None,
    operator_id=None,
):
    if route not in {"multicom_ami", "megafon_vats"}:
        raise Problem(422, "unsupported_route")
    if db is not None:
        from ats.connections import settings_for_tenant

        config = settings_for_tenant(db, tenant_id)
    else:
        config = settings()
    if not config.live_calls_enabled or tenant_id != config.telephony_tenant_id:
        raise Problem(409, "live_calls_disabled_for_tenant")
    if route == "multicom_ami":
        if not config.ami_secret.get_secret_value():
            raise Problem(
                409, "ami_secret_required",
                "Сохраните пароль выделенного AMI-аккаунта в настройках Asterisk ATOS.",
            )
        numbers = (
            db.scalars(select(CallerNumber).where(CallerNumber.tenant_id == tenant_id)).all()
            if db is not None
            else []
        )
        compatible = [
            number for number in numbers
            if not config.ami_cli_endpoint_prefix or number.provider == "multicom_ami"
        ]
        if numbers and not compatible:
            raise Problem(
                409, "caller_number_provider_mismatch",
                "Выберите для А-номера провайдера «Мультиком SIP / Asterisk» и подтвердите номер.",
            )
        eligible = any(
            number.enabled and number.verified and number.enabled_outgoing and not number.quarantined
            for number in compatible
        )
        if numbers and not eligible:
            if any(number.enabled and not number.verified for number in compatible):
                raise Problem(
                    409, "caller_number_not_verified",
                    "Подтвердите активный А-номер в его карточке в пуле после проверки у Мультикома.",
                )
            raise Problem(
                409, "caller_pool_unavailable",
                "Включите подтверждённый А-номер, разрешите исходящие вызовы и проверьте карантин.",
            )
        if not numbers and not config.ami_caller_id:
            raise Problem(
                409, "caller_pool_empty",
                "Добавьте и подтвердите А-номер Мультикома в пуле либо настройте разрешённый Caller ID.",
            )
        if (
            config.ami_answer_context == "ats-beta-echo"
            and mode is not None and (mode != "message" or template_id)
        ):
            raise Problem(
                409, "echo_test_requires_plain_message",
                "Для теста Echo выберите «Информатор» и «Без шаблона». "
                "Для обычной кампании верните обработку после ответа «Сценарий кампании».",
            )
        mobile = None
        if mode == "operator" and db is not None and (
            operator_id or (config.callback_enabled and not template_id)
        ):
            query = select(Operator).join(User, User.id == Operator.user_id).where(
                Operator.tenant_id == tenant_id, Operator.connection_method == "mobile",
                User.active, User.role == "operator",
            )
            if operator_id:
                query = query.where(Operator.id == operator_id)
            mobile = db.scalar(query.order_by(Operator.id).limit(1))
        if mobile:
            from ats.callbacks import validate_mobile

            if template_id:
                raise Problem(409, "callback_template_unsupported", "Для соединения через личный телефон выберите «Без шаблона».")
            validate_mobile(config, mobile)
            if not eligible:
                raise Problem(409, "callback_requires_verified_pool", "Для телефонного соединения включите подтверждённый А-номер в пуле.")
            if db.get(Tenant, tenant_id).max_channels < 2:
                raise Problem(409, "callback_needs_two_channels", "Телефонному соединению нужны два внешних канала в общих лимитах ATOS.")
        if mode is not None and (mode != "message" or template_id) and not config.media_enabled and not mobile:
            raise Problem(
                409, "media_not_configured",
                "Для перевода на операторов, ИИ-сценария и шаблона требуется медиарежим Asterisk. "
                "Для одиночной проверки выберите «Информатор» и «Без шаблона».",
            )
    if route == "megafon_vats" and not all(
        (
            config.megafon_url,
            config.megafon_user or not require_megafon_user,
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
    from ats.connections import settings_for_tenant

    config = settings_for_tenant(db, tenant_id)
    repo = TenantRepository(db, tenant_id)
    hour = now().astimezone(ZoneInfo(tenant.timezone)).hour
    active = db.scalar(
        select(func.coalesce(func.sum(Attempt.channel_units), 0))
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
                # One-shot campaigns should leave the active campaigns view as
                # soon as every item has a terminal outcome.  Archiving is a
                # visibility flag only: attempts, events and recordings keep
                # their campaign foreign key and remain available in history.
                campaign.archived = True
                repo.add(Audit, actor_id=None, action="campaign.completed", entity_id=campaign.id)
            continue
        contact = repo.get(Contact, item.contact_id, lock=True)
        blacklisted = db.scalar(
            repo.query(BlacklistEntry).where(BlacklistEntry.phone == contact.phone)
        )
        if not contact.consent or contact.blocked or contact.archived or blacklisted:
            item.status = "blocked"
            return True
        validate_route(
            tenant_id,
            campaign.route,
            db,
            require_megafon_user=not (
                campaign.route == "megafon_vats" and campaign.mode == "operator"
            ),
            mode=campaign.mode,
            template_id=campaign.template_id,
            operator_id=(campaign.schedule or {}).get("operator_id"),
        )
        operator = None
        if campaign.route in {"multicom_ami", "megafon_vats"} and (
            campaign.mode == "operator"
            or (campaign.mode == "agent" and campaign.connect_on_qualify)
        ):
            operator_query = repo.query(Operator).join(
                User, (User.id == Operator.user_id) & (User.tenant_id == Operator.tenant_id)
            ).where(Operator.status == "free", User.active, User.role == "operator")
            requested_operator_id = (campaign.schedule or {}).get("operator_id")
            if requested_operator_id:
                operator_query = operator_query.where(Operator.id == requested_operator_id)
            if campaign.route == "multicom_ami":
                if campaign.mode != "operator" or campaign.template_id or not config.callback_enabled:
                    operator_query = operator_query.where(Operator.connection_method == "sip")
                elif not config.media_enabled:
                    operator_query = operator_query.where(Operator.connection_method == "mobile")
                operator_query = operator_query.where(
                    (Operator.extension != "") | (Operator.connection_method == "mobile")
                )
            operator = db.scalar(operator_query.with_for_update(skip_locked=True).limit(1))
            if not operator:
                return False
        mobile_callback = bool(
            campaign.route == "multicom_ami" and operator and operator.connection_method == "mobile"
        )
        if mobile_callback:
            from ats.callbacks import validate_mobile

            validate_mobile(config, operator, contact.phone)
            if active + 2 > tenant.max_channels:
                return False
        if not config.destination_allowed(contact.phone):
            item.status = "blocked"
            repo.add(Audit, actor_id=None, entity_id=item.id, action="call.destination_blocked")
            return True
        from ats.workspace_service import event, select_caller

        try:
            caller_number = select_caller(
                db, tenant, campaign.route, config, operator, units=2 if mobile_callback else 1,
            )
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
            call_flow="mobile_callback" if mobile_callback else "standard",
            channel_units=2 if mobile_callback else 1,
            route_snapshot={"endpoint": config.ami_cli_endpoint_prefix + caller_number}
            if mobile_callback else {},
        )
        item.status = "active"
        db.flush()
        if mobile_callback:
            for role, destination in [("operator", operator.mobile_phone), ("customer", contact.phone)]:
                repo.add(CallLeg, attempt_id=attempt.id, role=role, phone=destination)
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


def ingest(db, tenant_id, route, event_key, external_id, event, payload=None, *, available_at=None):
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
            available_at=available_at if available_at is not None else now(),
        )
        .on_conflict_do_nothing()
    )


def finish_attempt(db, attempt, event, reason=""):
    old = attempt.status
    if attempt.route == "multicom_ami" and old in TERMINAL:
        return
    attempt.status = next_state(old, event)
    attempt.updated_at = now()
    if reason:
        attempt.reason = reason
    from ats.workspace_service import event as log_event

    if old != attempt.status:
        log_event(db, attempt, attempt.status, attempt.reason)
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
    # Finalize one-shot campaigns in the same transaction as their last
    # terminal attempt.  This removes them from the active campaigns view
    # without deleting the immutable attempt/call history.
    remaining = db.scalar(
        select(func.count())
        .select_from(CampaignItem)
        .where(
            CampaignItem.tenant_id == campaign.tenant_id,
            CampaignItem.campaign_id == campaign.id,
            CampaignItem.status.in_(["queued", "active"]),
        )
    )
    if not remaining:
        campaign.status = "completed"
        if not campaign.archived:
            campaign.archived = True
            repo.add(Audit, actor_id=None, action="campaign.completed", entity_id=campaign.id)
    repo.add(
        Outbox,
        kind="crm",
        dedupe_key=f"crm:{attempt.id}",
        payload={
            "attempt_id": str(attempt.id),
            "amo_responsible_user_id": attempt.agent_result.get("amo_responsible_user_id"),
            "recording_url": attempt.agent_result.get("recording_url"),
        },
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
    if not attempt and row.route == "megafon_vats":
        # MegaFon history callbacks may carry a leg callid different from the
        # id returned by makecall. Match only one recent active attempt for the
        # same tenant and normalized customer number; ambiguity stays orphaned.
        from ats.inbound import normalize_phone

        callback = row.payload or {}
        phone_keys = (
            "phone",
            "telnum",
            "phone_number",
            "customer_phone",
            "client_phone",
            "abonent",
            "dst",
            "destination",
            "destination_number",
            "called",
            "called_number",
            "number",
            "callee",
            "callerid",
        )
        phones = {
            normalized
            for key in phone_keys
            if (normalized := normalize_phone(callback.get(key, "")))
        }
        candidates = []
        if phones:
            query = repo.query(Attempt).where(
                Attempt.route == row.route,
                Attempt.status.in_(ACTIVE),
                Attempt.created_at >= row.received_at - timedelta(minutes=10),
                Attempt.phone.in_(phones),
            )
            candidates = list(
                db.scalars(query.order_by(Attempt.created_at.desc()).limit(2)).all()
            )
        if len(candidates) == 1:
            attempt = db.scalar(
                repo.query(Attempt).where(Attempt.id == candidates[0].id).with_for_update()
            )
    if not attempt:
        # Do not starve newer events; bounded retry window handled by received_at ordering.
        row.status = "unmatched" if now() - row.received_at > timedelta(minutes=5) else "orphan"
        row.available_at = now() + timedelta(seconds=5)
        history_phone = normalize_phone((row.payload or {}).get("phone", ""))
        if (
            row.route == "megafon_vats"
            and row.event in {"completed", "failed"}
            and row.status == "unmatched"
            and history_phone
        ):
            # A VATS account can receive history callbacks for calls that were
            # placed outside ATS. Preserve those terminal calls in the same
            # immutable journal used by the analytics views.
            source_id = hashlib.sha256(f"megafon:{row.external_id}".encode()).hexdigest()
            exists = db.scalar(
                repo.query(HistoricalCall).where(HistoricalCall.source_id == source_id)
            )
            if not exists:
                payload = row.payload or {}
                try:
                    duration = max(0, min(int(payload.get("duration", 0)), 86400))
                except (TypeError, ValueError):
                    duration = 0
                db.add(
                    HistoricalCall(
                        tenant_id=row.tenant_id,
                        contact_name="",
                        phone=history_phone,
                        campaign_name="",
                        route=row.route,
                        direction="in" if str(payload.get("direction", "")).lower() == "in" else "out",
                        status=row.event,
                        result=row.event,
                        duration_seconds=duration,
                        legacy_id=int(source_id[:8], 16) % 2_000_000_000,
                        source_id=source_id,
                        created_at=row.received_at,
                    )
                )
            row.status = "processed"
        return False
    if row.route == "multicom_ami" and row.event == "channel_hangup":
        # A channel's Hangup does not establish the outcome of a multi-leg call.
        # A terminal ATSCall event can arrive later on another AMI connection.
        if attempt.status in ACTIVE and attempt.reason not in EVIDENCE_REQUIRED:
            attempt.status = "unknown"
            attempt.reason = MISSING_AMI_TERMINAL
            attempt.updated_at = now()
            from ats.workspace_service import event as log_event

            log_event(db, attempt, "unknown", attempt.reason)
        row.status = "processed"
        return True
    if attempt.call_flow == "mobile_callback":
        # Provider/legacy events describe one leg; callback aggregation owns the result.
        row.status = "processed"
        return True
    if row.route == "multicom_ami" and attempt.reason == MISSING_AMI_TERMINAL:
        if row.event in {"completed", "failed"}:
            attempt.reason = ""
        else:
            # Delayed progress cannot resolve an observed channel termination.
            if row.event == "answered" and attempt.answered_at is None:
                attempt.answered_at = row.received_at
            row.status = "processed"
            return True
    if "agent_result" in row.payload:
        attempt.agent_result = row.payload["agent_result"]
    if row.payload.get("amo_responsible_user_id"):
        attempt.agent_result = {
            **(attempt.agent_result or {}),
            "amo_responsible_user_id": int(row.payload["amo_responsible_user_id"]),
        }
    if row.payload.get("recording_url"):
        attempt.agent_result = {**(attempt.agent_result or {}), "recording_url": str(row.payload["recording_url"])[:500]}
    finish_attempt(db, attempt, row.event, row.payload.get("reason", ""))
    row.status = "processed"
    return True
