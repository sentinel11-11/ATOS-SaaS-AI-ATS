"""Per-A-number usage cycles, followed by a configurable cooldown."""

from datetime import timedelta

from sqlalchemy import func, select

from ats.models import Attempt, CallerNumber, InboundCallback, now


def reset_expired(number, timestamp):
    if number.quota_cooldown_until and number.quota_cooldown_until <= timestamp:
        number.quota_started_at, number.quota_cooldown_until = timestamp, None


def usages(db, tenant_id, numbers, timestamp=None):
    timestamp = timestamp or now()
    forwards = select(func.count()).where(
        InboundCallback.tenant_id == CallerNumber.tenant_id,
        InboundCallback.caller_number == CallerNumber.phone,
        InboundCallback.created_at >= CallerNumber.quota_started_at,
    ).correlate(CallerNumber).scalar_subquery()
    result = dict(db.execute(select(CallerNumber.phone, func.coalesce(func.sum(Attempt.channel_units), 0) + forwards)
        .outerjoin(Attempt, (Attempt.tenant_id == CallerNumber.tenant_id)
                   & (Attempt.caller_number == CallerNumber.phone)
                   & (Attempt.created_at >= CallerNumber.quota_started_at))
        .where(CallerNumber.tenant_id == tenant_id, CallerNumber.id.in_([number.id for number in numbers]))
        .group_by(CallerNumber.id, CallerNumber.phone)).all())
    for number in numbers:
        if number.quota_cooldown_until and number.quota_cooldown_until <= timestamp:
            result[number.phone] = 0
    return result


def settings_changed(db, number, old_limit, old_seconds, old_until):
    timestamp = now()
    usage = usages(db, number.tenant_id, [number], timestamp).get(number.phone, 0)
    if old_until and old_until > timestamp:
        if number.daily_limit > old_limit and usage < number.daily_limit:
            number.quota_cooldown_until = None  # Keep already-used units in this cycle.
        elif old_seconds != number.quota_cooldown_seconds:
            number.quota_cooldown_until = old_until + timedelta(seconds=number.quota_cooldown_seconds - old_seconds)
    elif number.daily_limit != old_limit and usage >= number.daily_limit:
        number.quota_cooldown_until = timestamp + timedelta(seconds=number.quota_cooldown_seconds)
