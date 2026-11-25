"""Remember who actually placed the latest operator-assisted outbound call."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ats.models import CallbackAffinity, QueueEntry
from ats.schemas import phone_e164

AFFINITY_TTL = timedelta(days=7)


def remember_outbound(db, attempt, dialed_at):
    try:
        phone = phone_e164(attempt.phone)
    except ValueError:
        return
    operator_id = db.scalar(select(QueueEntry.operator_id).where(
        QueueEntry.tenant_id == attempt.tenant_id, QueueEntry.attempt_id == attempt.id,
    ))
    if not operator_id:
        return
    statement = insert(CallbackAffinity).values(
        tenant_id=attempt.tenant_id, phone=phone, last_operator_id=operator_id,
        call_flow=attempt.call_flow, dialed_at=dialed_at, expires_at=dialed_at + AFFINITY_TTL,
    )
    db.execute(statement.on_conflict_do_update(
        index_elements=[CallbackAffinity.tenant_id, CallbackAffinity.phone],
        set_={
            key: statement.excluded[key]
            for key in ("last_operator_id", "call_flow", "dialed_at", "expires_at")
        },
        where=CallbackAffinity.dialed_at < statement.excluded.dialed_at,
    ))
