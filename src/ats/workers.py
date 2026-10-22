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
from ats.models import Attempt, Campaign, CampaignItem, Contact, Outbox, Tenant, now
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


class Worker:
    def __init__(self, factory=None, drivers=None, crm=None):
        self.factory = factory or session_factory()
        self.drivers = drivers or {}
        self.crm = crm
        self.injected_crm = crm is not None

    def ami_event(self, frame):
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
        try:
            with self.factory.begin() as db:
                ingest(
                    db,
                    settings().telephony_tenant_id,
                    "multicom_ami",
                    f"{external}:{event}",
                    external,
                    event,
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

    def telephony(self):
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
            if not contact.consent or contact.blocked:
                finish_attempt(db, attempt, "failed")
                attempt.reason = "consent_withdrawn"
                complete_job(db, job_id, token)
                return True
            route, phone = attempt.route, contact.phone
            try:
                validate_route(tenant_id, route)
                if route != "sim" and phone not in settings().destinations:
                    raise Problem(409, "destination_not_allowed")
            except Problem as e:
                finish_attempt(db, attempt, "failed")
                attempt.reason = e.code
                complete_job(db, job_id, token, "dead", e.code)
                return True
            attempt.status, attempt.updated_at = "dispatching", now()
            if route in {"sim", "multicom_ami"}:
                attempt.external_id = str(attempt_id)
        # Crash from here until persisted response is ambiguous. No automatic redial.
        try:
            if route == "sim":
                external_id = str(attempt_id)
            else:
                external_id = self.driver(route).originate(attempt_id, phone)
        except (DeliveryUnknown, Retryable, Rejected) as e:
            with self.factory.begin() as db:
                row = TenantRepository(db, tenant_id).get(Attempt, attempt_id, lock=True)
                if isinstance(e, (Retryable, Rejected)):
                    finish_attempt(db, row, "failed")
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
                attempt = repo.get(Attempt, UUID(payload["attempt_id"]))
                item = repo.get(CampaignItem, attempt.item_id)
                contact = repo.get(Contact, item.contact_id)
                crm_id, status, attempt_id = contact.crm_contact_id, attempt.status, attempt.id
            if not crm_id:
                raise Rejected("crm_contact_mapping_missing")
            if self.crm is None:
                self.crm = AmoCRM(settings())
            self.crm.send(attempt_id, crm_id, status)
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
                    if settings().live_calls_enabled and settings().ami_secret.get_secret_value():
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
