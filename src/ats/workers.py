import argparse
import logging
import random
import signal
import threading
from datetime import timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from ats.adapters import AMI, AmoCRM, DeliveryUnknown, Megafon, Rejected, Retryable
from ats.amo_context import contact_context, reference_data
from ats.config import settings
from ats.db import session_factory
from ats.domain import ACTIVE, Problem, next_state
from ats.models import (
    AmoSyncJob,
    Attempt,
    BlacklistEntry,
    CallerNumber,
    Campaign,
    CampaignItem,
    Contact,
    InboundCall,
    Inbox,
    Operator,
    Outbox,
    QueueEntry,
    RuntimeLog,
    Template,
    Tenant,
    User,
    WorkspaceSettings,
    now,
)
from ats.repositories import TenantRepository
from ats.services import drain_inbox, finish_attempt, ingest, schedule_one, validate_route

log = logging.getLogger("ats.worker")


def backoff(attempts, retry_after=0):
    return max(retry_after, min(3600, 2 ** min(attempts, 11)) + random.uniform(0, 1))


def claim(db, kind):
    row = db.scalar(
        select(Outbox)
        .where(
            Outbox.kind == kind,
            ((Outbox.status == "pending") & (Outbox.available_at <= now()))
            | ((Outbox.status == "leased") & (Outbox.lease_until < now())),
        )
        .order_by(Outbox.available_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not row:
        return None
    # For CRM an expired lease may mean an acknowledged POST wasn't saved locally.
    if kind == "crm" and row.status == "leased":
        row.status, row.last_error = "unknown", "worker_lost_after_claim"
        return None
    row.status, row.lease_token = "leased", uuid4()
    row.lease_until = now() + timedelta(seconds=60)
    row.attempts += 1
    db.flush()
    return row.id, row.lease_token, row.tenant_id, row.payload, row.attempts


def complete_job(db, job_id, token, status="done", error="", delay=0):
    row = db.scalar(
        select(Outbox).where(Outbox.id == job_id, Outbox.lease_token == token).with_for_update()
    )
    if row is None or row.status != "leased":
        return
    row.status, row.last_error = status, error
    row.available_at = now() + timedelta(seconds=delay)
    row.lease_until, row.lease_token = None, None
    if error:
        db.add(
            RuntimeLog(
                tenant_id=row.tenant_id,
                level="WARNING" if status == "pending" else "ERROR",
                component=row.kind,
                code=error,
                entity_id=row.id,
            )
        )


class Worker:
    def __init__(self, factory=None, drivers=None, crm=None):
        self.factory = factory or session_factory()
        self.drivers = drivers or {}
        self.crm = crm
        self.injected_crm = crm is not None
        self.crm_drivers = {}

    def ami_event(self, frame):
        config = settings()
        if config.inbound_enabled and config.telephony_tenant_id:
            from ats.inbound import ingest_inbound

            kind, state = frame.get("Event"), None
            if frame.get("Context") in {s.strip() for s in config.inbound_contexts.split(",")}:
                state = (
                    "ringing"
                    if kind == "Newchannel"
                    else "answered"
                    if kind == "Newstate" and frame.get("ChannelStateDesc") == "Up"
                    else None
                )
            if kind == "Hangup":
                # Only match a previously observed inbound leg; never capture arbitrary channels.
                from ats.models import InboundCall

                with self.factory() as db:
                    existing = db.scalar(
                        select(InboundCall).where(
                            InboundCall.tenant_id == config.telephony_tenant_id,
                            InboundCall.route == "multicom_ami",
                            InboundCall.external_id == frame.get("Uniqueid"),
                        )
                    )
                    if existing:
                        state = "completed" if existing.status == "answered" else "failed"
            if state and frame.get("Uniqueid"):
                with self.factory.begin() as db:
                    ingest_inbound(
                        db,
                        config.telephony_tenant_id,
                        "multicom_ami",
                        frame["Uniqueid"],
                        frame.get("CallerIDNum", ""),
                        frame.get("Exten", ""),
                        state,
                    )
                return
        if frame.get("Event") == "OriginateResponse" and frame.get("ActionID"):
            try:
                with self.factory.begin() as db:
                    attempt = db.scalar(
                        select(Attempt)
                        .where(
                            Attempt.id == UUID(frame["ActionID"]),
                            Attempt.tenant_id == settings().telephony_tenant_id,
                        )
                        .with_for_update()
                    )
                    if attempt and frame.get("Channel") and len(frame["Channel"]) <= 160:
                        attempt.channel = frame["Channel"]
                        attempt.updated_at = now()
            except (ValueError, TypeError):
                pass
        if frame.get("Event") == "Hangup":
            channels = {str(frame.get("Channel") or ""), str(frame.get("Linkedid") or "")}
            channels.discard("")
            if channels:
                try:
                    with self.factory.begin() as db:
                        attempt = db.scalar(
                            select(Attempt)
                            .where(
                                Attempt.tenant_id == settings().telephony_tenant_id,
                                Attempt.channel.in_(channels),
                                Attempt.status.in_(ACTIVE),
                            )
                            .with_for_update()
                        )
                        if attempt:
                            event = "completed" if attempt.status == "answered" else "failed"
                            ingest(
                                db,
                                attempt.tenant_id,
                                "multicom_ami",
                                f"{attempt.id}:hangup:{frame.get('Uniqueid', '')}",
                                str(attempt.id),
                                event,
                                {"reason": "ami_hangup_fallback"},
                            )
                    return
                except (ValueError, TypeError):
                    pass
        external = event = None
        if frame.get("Event") == "UserEvent" and frame.get("UserEvent") == "ATSCall":
            external, event = frame.get("AttemptID"), frame.get("State")
        elif frame.get("Event") == "OriginateResponse" and frame.get("Response") == "Failure":
            external, event = frame.get("ActionID"), "failed"
        if event not in {"answered", "completed", "failed"} or not external:
            return
        try:
            UUID(external)
        except ValueError:
            return
        payload = {}
        if frame.get("ReasonCode") in {"media_failed", "playback_failed", "operator_unavailable"}:
            payload["reason"] = frame["ReasonCode"]
        if frame.get("Event") == "OriginateResponse" and frame.get("Reason") == "5":
            payload["reason"] = "busy"
        if frame.get("ResultB64"):
            import base64
            import json

            try:
                if len(frame["ResultB64"]) > 6000:
                    raise ValueError("media result limit")
                result = json.loads(base64.b64decode(frame["ResultB64"], validate=True))
                if not isinstance(result, dict) or not isinstance(result.get("qualified"), bool):
                    raise ValueError("media result shape")
                transcript = result.get("transcript", [])
                if not isinstance(transcript, list):
                    raise ValueError("transcript shape")
                payload["agent_result"] = {
                    "qualified": result["qualified"],
                    "transcript": transcript[:10],
                }
            except (ValueError, TypeError, KeyError):
                payload["reason"] = "invalid_media_result"
        if frame.get("DialStatus") in {"BUSY", "NOANSWER"}:
            payload["reason"] = "busy" if frame["DialStatus"] == "BUSY" else "no_answer"
        try:
            with self.factory.begin() as db:
                if frame.get("Channel") and len(frame["Channel"]) <= 160:
                    call = db.scalar(
                        select(Attempt)
                        .where(
                            Attempt.id == UUID(external),
                            Attempt.tenant_id == settings().telephony_tenant_id,
                        )
                        .with_for_update()
                    )
                    if call:
                        call.channel = frame["Channel"]
                ingest(
                    db,
                    settings().telephony_tenant_id,
                    "multicom_ami",
                    f"{external}:{event}",
                    external,
                    event,
                    payload,
                )
        except Exception as e:
            # AMI doesn't replay events: stale attempts will become unknown, never be redialed.
            log.error("ami_event_persist_failed type=%s", type(e).__name__)

    def driver(self, route, tenant_id=None, config=None):
        key = (route, str(tenant_id or "deployment"))
        # Preserve dependency injection used by provider contract tests and
        # operators' dry-run adapters while production instances remain tenant keyed.
        if key not in self.drivers and route in self.drivers:
            return self.drivers[route]
        if key not in self.drivers:
            config = config or settings()
            if route == "multicom_ami":
                self.drivers[key] = AMI(config, self.ami_event)
            elif route == "megafon_vats":
                self.drivers[key] = Megafon(config)
        return self.drivers[key]

    def schedule(self):
        with self.factory() as db:
            tenants = list(db.scalars(select(Tenant.id)).all())
        for tenant_id in tenants:
            try:
                with self.factory.begin() as db:
                    schedule_one(db, tenant_id)
                    options = db.scalar(
                        TenantRepository(db, tenant_id).query(WorkspaceSettings)
                    )
                    values = options.values if options else {}
                    if values.get("amo_sync_enabled"):
                        latest = db.scalar(
                            select(AmoSyncJob)
                            .where(AmoSyncJob.tenant_id == tenant_id)
                            .order_by(AmoSyncJob.created_at.desc())
                            .limit(1)
                        )
                        interval = int(values.get("amo_sync_interval_minutes", 15))
                        if latest is None or latest.created_at < now() - timedelta(minutes=interval):
                            job = AmoSyncJob(tenant_id=tenant_id, mode="scheduled", status="queued", counts={})
                            db.add(job)
                            db.flush()
                            db.add(Outbox(tenant_id=tenant_id, kind="crm_sync", dedupe_key=f"amo-sync:{job.id}", payload={"job_id": str(job.id)}))
            except Problem as e:
                log.warning("allocation_blocked tenant=%s code=%s", tenant_id, e.code)

    def inbox(self):
        for _ in range(100):
            with self.factory.begin() as db:
                if not drain_inbox(db):
                    break
        self.diagnostic_job()
        from ats.media import attach_recordings

        attach_recordings(self.factory)

    def telephony(self):
        self.call_control()
        with self.factory.begin() as db:
            job = claim(db, "telephony")
        if not job:
            return False
        job_id, token, tenant_id, payload, attempts = job
        attempt_id = UUID(payload["attempt_id"])
        with self.factory.begin() as db:
            # Serialize with pause/allocation. No network request holds this lock.
            tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
            repo = TenantRepository(db, tenant_id)
            attempt = repo.get(Attempt, attempt_id, lock=True)
            if attempt.status != "prepared":
                if attempt.status == "dispatching":
                    attempt.status, attempt.reason = "unknown", "dispatch_worker_lost"
                complete_job(db, job_id, token)
                return True
            item = repo.get(CampaignItem, attempt.item_id)
            contact = repo.get(Contact, item.contact_id, lock=True)
            campaign = repo.get(Campaign, item.campaign_id)
            hour = now().astimezone(ZoneInfo(tenant.timezone)).hour
            if campaign.status != "running" or not (
                campaign.window_start <= hour < campaign.window_end
            ):
                complete_job(db, job_id, token, "pending", delay=5)
                return True
            if (
                not contact.consent
                or contact.blocked
                or contact.archived
                or db.scalar(
                    repo.query(BlacklistEntry).where(BlacklistEntry.phone == contact.phone)
                )
            ):
                finish_attempt(db, attempt, "failed")
                attempt.reason = "consent_withdrawn"
                complete_job(db, job_id, token)
                return True
            route, phone = attempt.route, contact.phone
            if attempt.phone and phone != attempt.phone:
                finish_attempt(db, attempt, "failed", "contact_phone_changed")
                complete_job(db, job_id, token)
                return True
            caller_number = attempt.caller_number
            provider_options = db.scalar(repo.query(WorkspaceSettings))
            megafon_user = (provider_options.values if provider_options else {}).get("megafon_user")
            template = repo.get(Template, campaign.template_id) if campaign.template_id else None
            media_inputs = (
                (attempt, campaign, template, contact) if route == "multicom_ami" else None
            )
            queue = db.scalar(repo.query(QueueEntry).where(QueueEntry.attempt_id == attempt.id))
            operator_ext, megafon_operator_user = "", ""
            if queue and queue.operator_id:
                operator = repo.get(Operator, queue.operator_id)
                operator_ext = operator.extension
                if route == "megafon_vats":
                    operator_user = repo.get(User, operator.user_id)
                    megafon_operator_user = operator.megafon_user.strip() or operator_user.login
            try:
                from ats.connections import settings_for_tenant

                runtime_config = settings_for_tenant(db, tenant_id)
                if route == "megafon_vats" and campaign.mode == "operator" and not megafon_operator_user:
                    raise Problem(409, "megafon_operator_not_assigned")
                validate_route(
                    tenant_id,
                    route,
                    db,
                    require_megafon_user=not bool(megafon_operator_user),
                )
                if route == "multicom_ami":
                    number = db.scalar(
                        repo.query(CallerNumber).where(CallerNumber.phone == caller_number)
                    )
                    if (
                        number and (not number.enabled or not number.verified or number.quarantined)
                    ) or (not number and caller_number != runtime_config.ami_caller_id):
                        raise Problem(409, "caller_number_unavailable")
                if not runtime_config.destination_allowed(phone):
                    raise Problem(409, "destination_not_allowed")
            except Problem as e:
                finish_attempt(db, attempt, "failed")
                attempt.reason = e.code
                complete_job(db, job_id, token, "dead", e.code)
                return True
            attempt.status, attempt.updated_at = "dispatching", now()
            from ats.workspace_service import event

            event(db, attempt, "dispatching")
            if route == "multicom_ami":
                attempt.external_id = str(attempt_id)
        # Crash from here until persisted response is ambiguous. No automatic redial.
        try:
            from ats.connections import settings_for_tenant

            with self.factory() as config_db:
                runtime_config = settings_for_tenant(config_db, tenant_id)
            media = False
            if media_inputs:
                from ats.media import prepare_media

                media = prepare_media(*media_inputs, config=runtime_config)
            driver = self.driver(route, tenant_id, runtime_config)
            external_id = (
                driver.originate(
                    attempt_id,
                    phone,
                    caller_id=caller_number,
                    media=media,
                    operator_ext=operator_ext,
                )
                if route == "multicom_ami"
                else driver.originate(
                    attempt_id, phone, user=megafon_operator_user or megafon_user
                )
                )
        except Problem as e:
            with self.factory.begin() as db:
                row = TenantRepository(db, tenant_id).get(Attempt, attempt_id, lock=True)
                finish_attempt(db, row, "failed", e.code)
                complete_job(db, job_id, token, "dead", e.code)
            return True
        except (DeliveryUnknown, Retryable, Rejected) as e:
            with self.factory.begin() as db:
                row = TenantRepository(db, tenant_id).get(Attempt, attempt_id, lock=True)
                if isinstance(e, (Retryable, Rejected)):
                    finish_attempt(db, row, "failed", getattr(e, "code", "provider_rejected"))
                elif row.status not in {"completed", "failed"}:
                    row.status = "unknown"
                row.reason = getattr(e, "code", "provider_delivery_unknown")
                complete_job(
                    db, job_id, token, "unknown" if row.status == "unknown" else "done", row.reason
                )
            return True
        with self.factory.begin() as db:
            row = TenantRepository(db, tenant_id).get(Attempt, attempt_id, lock=True)
            row.external_id = external_id
            row.status, row.updated_at = next_state(row.status, "accepted"), now()
            complete_job(db, job_id, token)
        return True

    def call_control(self):
        with self.factory.begin() as db:
            job = claim(db, "control")
        if not job:
            return False
        job_id, token, tenant_id, payload, attempts = job
        try:
            with self.factory() as db:
                call = TenantRepository(db, tenant_id).get(Attempt, UUID(payload["attempt_id"]))
                channel, status = call.channel, call.status
            if status not in {"completed", "failed"}:
                from ats.connections import settings_for_tenant

                with self.factory() as config_db:
                    runtime_config = settings_for_tenant(config_db, tenant_id)
                if tenant_id != runtime_config.telephony_tenant_id or not channel.startswith(
                    ("Local/", "PJSIP/", "SIP/")
                ):
                    raise Rejected("call_channel_unavailable")
                driver = self.driver("multicom_ami", tenant_id, runtime_config)
                driver.connect()
                driver.action("Hangup", [("Channel", channel)])
        except Problem as exc:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "dead", exc.code)
        except (Rejected, Retryable, DeliveryUnknown) as exc:
            with self.factory.begin() as db:
                complete_job(
                    db,
                    job_id,
                    token,
                    "unknown" if isinstance(exc, DeliveryUnknown) else "dead",
                    getattr(exc, "code", "hangup_outcome_unknown"),
                )
        else:
            with self.factory.begin() as db:
                complete_job(db, job_id, token)
        return True

    def crm_job(self):
        with self.factory.begin() as db:
            job = claim(db, "crm")
        if not job:
            return False
        job_id, token, tenant_id, payload, attempts = job
        try:
            from ats.connections import settings_for_tenant

            with self.factory() as config_db:
                runtime_config = settings_for_tenant(config_db, tenant_id)
            if not runtime_config.crm_enabled and not self.injected_crm:
                raise Rejected("crm_disabled")
            if not self.injected_crm and tenant_id != runtime_config.amo_tenant_id:
                raise Rejected("crm_tenant_not_bound")
            with self.factory() as db:
                repo = TenantRepository(db, tenant_id)
                if "inbound_id" in payload:
                    attempt = repo.get(InboundCall, UUID(payload["inbound_id"]))
                    if not attempt.contact_id:
                        raise Rejected("inbound_contact_missing")
                    contact = repo.get(Contact, attempt.contact_id)
                    direction, duration = "inbound", attempt.duration_seconds
                else:
                    attempt = repo.get(Attempt, UUID(payload["attempt_id"]))
                    item = repo.get(CampaignItem, attempt.item_id)
                    contact = repo.get(Contact, item.contact_id)
                    direction = "outbound"
                    duration = (
                        max(0, int((attempt.ended_at - attempt.answered_at).total_seconds()))
                        if attempt.ended_at and attempt.answered_at
                        else 0
                    )
                crm_id, status, attempt_id = contact.crm_contact_id, attempt.status, attempt.id
                contact_id, phone, name = contact.id, contact.phone, contact.name
                created_at = attempt.created_at
                provider = db.scalar(repo.query(WorkspaceSettings))
                options = provider.values if provider else {}
            crm = self.crm
            if crm is None:
                crm = self.crm_drivers.get(str(tenant_id))
                if crm is None:
                    crm = self.crm_drivers[str(tenant_id)] = AmoCRM(runtime_config)
            if not crm_id and options.get("amo_auto_create_contacts") and not self.injected_crm:
                crm_id = self.resolve_crm_contact(
                    tenant_id,
                    contact_id,
                    name,
                    phone,
                    options.get("amo_responsible_user_id"),
                    crm,
                )
            if not crm_id:
                raise Rejected("crm_contact_mapping_missing")
            responsible = payload.get("amo_responsible_user_id") or options.get("amo_responsible_user_id")
            crm.send(attempt_id, crm_id, status)
            if not self.injected_crm:
                crm.register_call(
                    attempt_id,
                    phone,
                    status,
                    created_at,
                    duration,
                    responsible,
                    direction,
                    payload.get("recording_url"),
                    source=getattr(attempt, "route", "ATS"),
                )
                if options.get("amo_auto_create_tasks"):
                    crm.create_task(
                        attempt_id,
                        crm_id,
                        options.get("task_due_hours", 24),
                        responsible,
                    )
        except Retryable as e:
            with self.factory.begin() as db:
                complete_job(
                    db,
                    job_id,
                    token,
                    "pending" if attempts < 8 else "dead",
                    e.code,
                    backoff(attempts, e.retry_after),
                )
        except Rejected as e:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "dead", e.code)
        except DeliveryUnknown:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "unknown", "crm_write_outcome_unknown")
        except Problem as exc:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "dead", exc.code)
        else:
            with self.factory.begin() as db:
                complete_job(db, job_id, token)
        return True

    def crm_sync_job(self):
        """Apply queued amoCRM webhook/scheduled contact changes idempotently."""
        with self.factory.begin() as db:
            job = claim(db, "crm_sync")
        if not job:
            return False
        job_id, token, tenant_id, payload, attempts = job
        try:
            from ats.connections import settings_for_tenant
            from ats.schemas import phone_from_import

            with self.factory() as config_db:
                runtime_config = settings_for_tenant(config_db, tenant_id)
            if not runtime_config.crm_enabled or runtime_config.amo_tenant_id != tenant_id:
                raise Rejected("crm_tenant_not_bound")
            crm = self.crm_drivers.get(str(tenant_id))
            if crm is None:
                crm = self.crm_drivers[str(tenant_id)] = AmoCRM(runtime_config)
            raw_contacts = []
            for page in range(1, 21):
                batch = crm.lead_contacts(page=page, limit=250)
                raw_contacts.extend(batch)
                if len(batch) < 250:
                    break
            pipelines, statuses, users = reference_data(crm, raw_contacts)
            with self.factory.begin() as db:
                repo = TenantRepository(db, tenant_id)
                row_job = repo.get(AmoSyncJob, UUID(payload["job_id"]), lock=True)
                row_job.status = "running"
                counts = {"created": 0, "updated": 0, "archived": 0, "skipped": 0}
                event_payload = payload.get("payload") or {}
                deleted = event_payload.get("contacts", {}).get("delete", []) if isinstance(event_payload, dict) else []
                for entry in deleted if isinstance(deleted, list) else []:
                    try:
                        deleted_id = int(entry.get("id") if isinstance(entry, dict) else entry)
                    except (TypeError, ValueError):
                        continue
                    deleted_row = db.scalar(repo.query(Contact).where(Contact.crm_contact_id == deleted_id).with_for_update())
                    if deleted_row:
                        deleted_row.archived, deleted_row.blocked, deleted_row.consent = True, True, False
                        counts["archived"] += 1
                seen = set()
                for raw in raw_contacts:
                    crm_id = int(raw["crm_contact_id"])
                    existing = db.scalar(repo.query(Contact).where(Contact.crm_contact_id == crm_id).with_for_update())
                    if raw.get("archived"):
                        if existing:
                            existing.archived, existing.blocked, existing.consent = True, True, False
                            counts["archived"] += 1
                        continue
                    try:
                        phone = phone_from_import(raw.get("phone", ""))
                    except ValueError:
                        counts["skipped"] += 1
                        continue
                    if phone in seen:
                        counts["skipped"] += 1
                        continue
                    seen.add(phone)
                    if existing is None:
                        existing = db.scalar(repo.query(Contact).where(Contact.phone == phone).with_for_update())
                    phone_owner = db.scalar(repo.query(Contact).where(Contact.phone == phone).with_for_update())
                    if phone_owner is not None and (existing is None or phone_owner.id != existing.id):
                        counts["skipped"] += 1
                        continue
                    if existing is None:
                        existing = repo.add(Contact, name=raw["name"], phone=phone, email=raw["email"], group=raw["group"], source="amoCRM", consent=True, consent_source="amoCRM", crm_contact_id=crm_id, crm_context=contact_context(raw, pipelines, statuses, users))
                        counts["created"] += 1
                    else:
                        existing.name, existing.phone, existing.email, existing.group = raw["name"], phone, raw["email"], raw["group"]
                        existing.crm_contact_id, existing.source, existing.archived = crm_id, "amoCRM", False
                        existing.crm_context = contact_context(raw, pipelines, statuses, users)
                        counts["updated"] += 1
                row_job.counts, row_job.status, row_job.updated_at = counts, "completed", now()
                if payload.get("event_key"):
                    event_row = db.scalar(repo.query(Inbox).where(Inbox.event_key == payload["event_key"]).with_for_update())
                    if event_row:
                        event_row.status = "processed"
                complete_job(db, job_id, token)
        except (Retryable, Rejected) as exc:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "pending" if attempts < 8 else "dead", exc.code, backoff(attempts, getattr(exc, "retry_after", 0)))
        except Exception:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "pending" if attempts < 8 else "dead", "crm_sync_failed", backoff(attempts))
        return True

    def resolve_crm_contact(self, tenant_id, contact_id, name, phone, responsible, crm=None):
        """Fence creation per contact; a lost write is reconciled rather than posted again."""
        token = uuid4()
        with self.factory.begin() as db:
            contact = TenantRepository(db, tenant_id).get(Contact, contact_id, lock=True)
            if contact.crm_contact_id:
                return contact.crm_contact_id
            if contact.crm_sync_state == "creating":
                if contact.crm_sync_until and contact.crm_sync_until > now():
                    raise Retryable("crm_contact_creation_busy", 30)
                raise DeliveryUnknown()
            contact.crm_sync_state, contact.crm_sync_token = "creating", token
            contact.crm_sync_until = now() + timedelta(minutes=2)
        try:
            crm_id = (crm or self.crm).ensure_contact(name, phone, responsible)
        except (Retryable, Rejected):
            with self.factory.begin() as db:
                contact = TenantRepository(db, tenant_id).get(Contact, contact_id, lock=True)
                if contact.crm_sync_token == token:
                    contact.crm_sync_state, contact.crm_sync_token, contact.crm_sync_until = (
                        "idle",
                        None,
                        None,
                    )
            raise
        with self.factory.begin() as db:
            contact = TenantRepository(db, tenant_id).get(Contact, contact_id, lock=True)
            if contact.crm_sync_token != token:
                raise DeliveryUnknown()
            contact.crm_contact_id, contact.crm_sync_state = crm_id, "mapped"
            contact.crm_sync_token, contact.crm_sync_until = None, None
        return crm_id

    def watchdog(self):
        with self.factory.begin() as db:
            tenants = list(db.scalars(select(Tenant.id)).all())
            for tenant_id in tenants:
                options = db.scalar(
                    select(WorkspaceSettings).where(WorkspaceSettings.tenant_id == tenant_id)
                )
                basic = (options.values if options else {}).get("_legacy_basic", {})
                try:
                    watchdog_minutes = max(1, min(1440, int(basic.get("watchdog_timeout_min", 20))))
                except (TypeError, ValueError):
                    watchdog_minutes = 20
                try:
                    conversation_minutes = max(0, min(1440, int(basic.get("vats_conversation_timeout_min", 30))))
                except (TypeError, ValueError):
                    conversation_minutes = 30
                current = now()
                stale_cutoff = current - timedelta(minutes=watchdog_minutes)
                unknown_cutoff = (
                    current - timedelta(minutes=conversation_minutes)
                    if conversation_minutes
                    else None
                )
                stale_filter = (
                    Attempt.tenant_id == tenant_id,
                    or_(
                        (
                            Attempt.status.in_(
                                ["prepared", "dispatching", "accepted", "answered"]
                            )
                            & (Attempt.updated_at < stale_cutoff)
                        ),
                        (
                            (Attempt.status == "unknown")
                            & (Attempt.updated_at < unknown_cutoff)
                            if unknown_cutoff is not None
                            else False
                        ),
                    ),
                )
                rows = db.scalars(
                    select(Attempt)
                    .where(*stale_filter)
                    .with_for_update(skip_locked=True)
                    .limit(100)
                ).all()
                for row in rows:
                    if row.status == "dispatching":
                        # The provider may have accepted the request, so do not
                        # redial. Keep it fenced as unknown until the longer
                        # conversation safety timeout releases the slot.
                        row.status = "unknown"
                        row.reason = "watchdog_dispatch_unknown"
                        row.updated_at = current
                        from ats.workspace_service import event

                        event(db, row, "unknown", row.reason)
                    elif row.status == "unknown":
                        finish_attempt(db, row, "failed", "stale_call_reconciled")
                    else:
                        finish_attempt(db, row, "failed", "provider_terminal_event_missing")

    def diagnostic_job(self):
        """Read-only provider probes run outside API requests and DB transactions."""
        import httpx

        from ats.adapters import https_origin

        with self.factory.begin() as db:
            job = claim(db, "diagnostic")
        if not job:
            return False
        if job[3].get("sync"):
            return self.sync_directory(job)
        job_id, token, tenant_id, payload, attempts = job
        provider, code, status = payload["provider"], "connection_ok", "done"
        try:
            from ats.connections import settings_for_tenant

            with self.factory() as config_db:
                config = settings_for_tenant(config_db, tenant_id)
            if provider == "ami":
                if (
                    tenant_id != config.telephony_tenant_id
                    or not config.ami_secret.get_secret_value()
                ):
                    raise Rejected("ami_not_configured_for_tenant")
                ami = AMI(config, lambda _: None)
                try:
                    ami.connect()
                    ami.action("Ping", [])
                finally:
                    ami.close()
            else:
                if provider == "amocrm":
                    if (
                        tenant_id != config.amo_tenant_id
                        or not config.amo_access_token.get_secret_value()
                    ):
                        raise Rejected("crm_not_configured_for_tenant")
                    url = https_origin(config.amo_url) + "/api/v4/account"
                    headers = {
                        "Authorization": "Bearer " + config.amo_access_token.get_secret_value()
                    }
                else:
                    if (
                        tenant_id != config.telephony_tenant_id
                        or not config.megafon_api_key.get_secret_value()
                    ):
                        raise Rejected("megafon_not_configured_for_tenant")
                    url = https_origin(config.megafon_url) + "/crmapi/v1/users"
                    headers = {"X-API-KEY": config.megafon_api_key.get_secret_value()}
                with httpx.Client(timeout=10, follow_redirects=False) as client:
                    response = client.get(url, headers=headers)
                if response.status_code != 200:
                    raise Rejected(f"provider_http_{response.status_code}")
        except Problem as exc:
            status, code = "dead", exc.code
        except Retryable as exc:
            # A temporary provider/network failure should remain visible and
            # retry with bounded backoff; a single click must not silently
            # consume a sync request forever.
            status, code = "pending", exc.code
        except (Rejected, DeliveryUnknown, httpx.HTTPError) as exc:
            status, code = "dead", getattr(exc, "code", "provider_unreachable")
        with self.factory.begin() as db:
            complete_job(
                db,
                job_id,
                token,
                status,
                "" if status == "done" else code,
                backoff(attempts) if status == "pending" else 0,
            )
            db.add(
                RuntimeLog(
                    tenant_id=tenant_id,
                    level="INFO" if status == "done" else "WARNING" if status == "pending" else "ERROR",
                    component=provider,
                    code=code,
                    entity_id=job_id,
                )
            )
        return True

    def sync_directory(self, job):
        from ats.provider_sync import fetch_directory, store_directory

        job_id, token, tenant_id, payload, attempts = job
        provider = payload["provider"]
        try:
            from ats.connections import settings_for_tenant

            with self.factory() as config_db:
                config = settings_for_tenant(config_db, tenant_id)
            binding = config.amo_tenant_id if provider == "amocrm" else config.telephony_tenant_id
            if tenant_id != binding:
                raise Rejected("provider_not_bound_to_tenant")
            values = fetch_directory(config, provider, payload["sync"])
        except Problem as exc:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "dead", exc.code)
        except Retryable as exc:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "pending", exc.code, backoff(attempts, exc.retry_after))
        except Rejected as exc:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "dead", exc.code)
        else:
            with self.factory.begin() as db:
                store_directory(db, tenant_id, provider, payload["sync"], values)
                complete_job(db, job_id, token)
                db.add(
                    RuntimeLog(
                        tenant_id=tenant_id,
                        level="INFO",
                        component=provider,
                        code="directory_sync_completed",
                        entity_id=job_id,
                    )
                )
        return True

    def close(self):
        for driver in self.drivers.values():
            if hasattr(driver, "close"):
                driver.close()
        for driver in self.crm_drivers.values():
            if hasattr(driver, "close"):
                driver.close()
        if self.injected_crm and hasattr(self.crm, "close"):
            self.crm.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["scheduler", "telephony", "crm", "inbox"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    worker, stop = Worker(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                if args.role == "scheduler":
                    worker.schedule()
                    worker.watchdog()
                elif args.role == "telephony":
                    # Keep AMI subscribed across reconnects and process restarts.
                    if (
                        settings().live_calls_enabled or settings().inbound_enabled
                    ) and settings().ami_secret.get_secret_value():
                        worker.driver("multicom_ami").connect()
                    worker.telephony()
                elif args.role == "crm":
                    worker.crm_job()
                    worker.crm_sync_job()
                else:
                    worker.inbox()
            except Exception:
                # Keep the worker alive, but retain the traceback.  A short
                # error-only message made production failures such as a
                # callback parser crash impossible to diagnose from journald.
                log.exception("worker_iteration_failed role=%s", args.role)
            stop.wait(settings().worker_poll_seconds)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
