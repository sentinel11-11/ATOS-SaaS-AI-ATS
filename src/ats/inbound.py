"""Normalize provider events without granting outbound consent or dial permission."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ats.domain import Problem
from ats.models import Audit, Contact, InboundCall, Outbox, Tenant, now
from ats.schemas import phone_e164


def normalize_phone(raw):
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    try:
        return phone_e164("+" + digits)
    except ValueError:
        return ""


def ingest_inbound(db, tenant_id, route, external_id, phone, destination, state, duration=0):
    if not tenant_id or not external_id or len(external_id) > 120:
        raise Problem(422, "invalid_inbound_event")
    if state not in {"ringing", "answered", "completed", "failed"}:
        raise Problem(422, "invalid_inbound_state")
    phone = normalize_phone(phone)
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    contact = (
        db.scalar(select(Contact).where(Contact.tenant_id == tenant_id, Contact.phone == phone))
        if phone
        else None
    )
    if phone and not contact:
        contact = Contact(tenant_id=tenant_id, phone=phone, name="Входящий звонок", consent=False)
        db.add(contact)
        db.flush()
    db.execute(
        insert(InboundCall)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            route=route,
            external_id=external_id,
            phone=phone,
            destination=str(destination)[:32],
            contact_id=contact.id if contact else None,
            status="ringing",
            duration_seconds=0,
            created_at=now(),
            updated_at=now(),
        )
        .on_conflict_do_nothing()
    )
    row = db.scalar(
        select(InboundCall)
        .where(
            InboundCall.tenant_id == tenant_id,
            InboundCall.route == route,
            InboundCall.external_id == external_id,
        )
        .with_for_update()
    )
    if row.status not in {"completed", "failed"}:
        old = row.status
        if row.status != "answered" or state in {"completed", "failed"}:
            row.status = state
        row.duration_seconds, row.updated_at = max(0, min(int(duration), 86400)), now()
        if row.status != old:
            db.add(Audit(tenant_id=tenant_id, action=f"inbound.{row.status}", entity_id=row.id))
            if row.status in {"completed", "failed"}:
                db.add(
                    Outbox(
                        tenant_id=tenant_id,
                        kind="crm",
                        dedupe_key=f"crm-inbound:{row.id}",
                        payload={"inbound_id": str(row.id)},
                    )
                )
    return row
