import argparse
import logging
import random
import signal
import threading
from datetime import timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ats.adapters import AMI, AmoCRM, DeliveryUnknown, Megafon, Rejected, Retryable
from ats.config import settings
from ats.db import session_factory
from ats.domain import Problem, next_state
from ats.models import (
    Attempt,
    BlacklistEntry,
    CallerNumber,
    Campaign,
    CampaignItem,
    Contact,
    InboundCall,
    Operator,
    Outbox,
    QueueEntry,
    RuntimeLog,
    Template,
    Tenant,
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

    def driver(self, route):
        if route not in self.drivers:
            if route == "multicom_ami":
                self.drivers[route] = AMI(settings(), self.ami_event)
            elif route == "megafon_vats":
                self.drivers[route] = Megafon(settings())
        return self.drivers[route]

    def schedule(self):
        with self.factory() as db:
            tenants = list(db.scalars(select(Tenant.id)).all())
        for tenant_id in tenants:
            try:
                with self.factory.begin() as db:
                    schedule_one(db, tenant_id)
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
        job_id, token, tenant_id, payload, _ = job
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
            operator_ext = (
                repo.get(Operator, queue.operator_id).extension
                if queue and queue.operator_id
                else ""
            )
            if route == "sim" and template and campaign.mode == "agent":
                from ats.workspace_service import run_scenario

                attempt.agent_result = run_scenario(
                    template.scenario,
                    {q["id"]: "1" for q in template.scenario.get("questions", [])},
                    contact.name,
                )
            try:
                validate_route(tenant_id, route, db)
                if route == "multicom_ami":
                    number = db.scalar(
                        repo.query(CallerNumber).where(CallerNumber.phone == caller_number)
                    )
                    if (
                        number and (not number.enabled or not number.verified or number.quarantined)
                    ) or (not number and caller_number != settings().ami_caller_id):
                        raise Problem(409, "caller_number_unavailable")
                if route != "sim" and phone not in settings().destinations:
                    raise Problem(409, "destination_not_allowed")
            except Problem as e:
                finish_attempt(db, attempt, "failed")
                attempt.reason = e.code
                complete_job(db, job_id, token, "dead", e.code)
                return True
            attempt.status, attempt.updated_at = "dispatching", now()
            from ats.workspace_service import event

            event(db, attempt, "dispatching")
            if route in {"sim", "multicom_ami"}:
                attempt.external_id = str(attempt_id)
        # Crash from here until persisted response is ambiguous. No automatic redial.
        try:
            media = False
            if media_inputs:
                from ats.media import prepare_media

                media = prepare_media(*media_inputs)
            if route == "sim":
                external_id = str(attempt_id)
            else:
                driver = self.driver(route)
                if isinstance(driver, Megafon) and megafon_user:
                    driver.user = megafon_user
                external_id = (
                    driver.originate(
                        attempt_id,
                        phone,
                        caller_id=caller_number,
                        media=media,
                        operator_ext=operator_ext,
                    )
                    if route == "multicom_ami"
                    else driver.originate(attempt_id, phone)
                )
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
            if route == "sim":
                finish_attempt(db, row, "completed")
            complete_job(db, job_id, token)
        return True

    def call_control(self):
        with self.factory.begin() as db:
            job = claim(db, "control")
        if not job:
            return False
        job_id, token, tenant_id, payload, _ = job
        try:
            with self.factory() as db:
                call = TenantRepository(db, tenant_id).get(Attempt, UUID(payload["attempt_id"]))
                channel, status = call.channel, call.status
            if status not in {"completed", "failed"}:
                if tenant_id != settings().telephony_tenant_id or not channel.startswith(
                    ("Local/", "PJSIP/", "SIP/")
                ):
                    raise Rejected("call_channel_unavailable")
                driver = self.driver("multicom_ami")
                driver.connect()
                driver.action("Hangup", [("Channel", channel)])
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
            if not settings().crm_enabled and not self.injected_crm:
                raise Rejected("crm_disabled")
            if not self.injected_crm and tenant_id != settings().amo_tenant_id:
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
            if self.crm is None:
                self.crm = AmoCRM(settings())
            if not crm_id and options.get("amo_auto_create_contacts") and not self.injected_crm:
                crm_id = self.resolve_crm_contact(
                    tenant_id, contact_id, name, phone, options.get("amo_responsible_user_id")
                )
            if not crm_id:
                raise Rejected("crm_contact_mapping_missing")
            self.crm.send(attempt_id, crm_id, status)
            if not self.injected_crm:
                self.crm.register_call(
                    attempt_id,
                    phone,
                    status,
                    created_at,
                    duration,
                    options.get("amo_responsible_user_id"),
                    direction,
                )
                if options.get("amo_auto_create_tasks"):
                    self.crm.create_task(
                        attempt_id,
                        crm_id,
                        options.get("task_due_hours", 24),
                        options.get("amo_responsible_user_id"),
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
        else:
            with self.factory.begin() as db:
                complete_job(db, job_id, token)
        return True

    def resolve_crm_contact(self, tenant_id, contact_id, name, phone, responsible):
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
            crm_id = self.crm.ensure_contact(name, phone, responsible)
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
            rows = db.scalars(
                select(Attempt)
                .where(
                    Attempt.status.in_(["accepted", "answered"]),
                    Attempt.updated_at < now() - timedelta(minutes=45),
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            ).all()
            for row in rows:
                row.status, row.reason = "unknown", "provider_terminal_event_missing"

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
        job_id, token, tenant_id, payload, _ = job
        provider, config, code, status = payload["provider"], settings(), "connection_ok", "done"
        try:
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
        except (Rejected, Retryable, DeliveryUnknown, httpx.HTTPError) as exc:
            status, code = "dead", getattr(exc, "code", "provider_unreachable")
        with self.factory.begin() as db:
            complete_job(db, job_id, token, status, "" if status == "done" else code)
            db.add(
                RuntimeLog(
                    tenant_id=tenant_id,
                    level="INFO" if status == "done" else "ERROR",
                    component=provider,
                    code=code,
                    entity_id=job_id,
                )
            )
        return True

    def sync_directory(self, job):
        from ats.provider_sync import fetch_directory, store_directory

        job_id, token, tenant_id, payload, _ = job
        provider, config = payload["provider"], settings()
        try:
            binding = config.amo_tenant_id if provider == "amocrm" else config.telephony_tenant_id
            if tenant_id != binding:
                raise Rejected("provider_not_bound_to_tenant")
            values = fetch_directory(config, provider, payload["sync"])
        except (Rejected, Retryable) as exc:
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
            if isinstance(driver, AMI):
                driver.close()


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
                else:
                    worker.inbox()
            except Exception as e:
                log.error("worker_iteration_failed role=%s type=%s", args.role, type(e).__name__)
            stop.wait(settings().worker_poll_seconds)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
