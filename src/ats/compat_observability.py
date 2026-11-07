"""Legacy UI read contracts backed by tenant-scoped PostgreSQL projections.

The v2 names belong to the original React client. No legacy engine or SQLite
connection is used here. Joins/unions perform filtering and pagination before
serialization, so a large call history does not become a browser-sized query.
"""

import base64
import binascii
import io
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, Query, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import (
    Date,
    Integer,
    String,
    case,
    cast,
    extract,
    func,
    literal,
    or_,
    select,
    union_all,
    update,
)

from ats import api_calls
from ats.db import session
from ats.domain import ACTIVE, Problem
from ats.models import (
    Attempt,
    Audit,
    CallerNumber,
    CallEvent,
    Campaign,
    CampaignItem,
    Contact,
    HistoricalCall,
    InboundCall,
    Inbox,
    Operator,
    Outbox,
    QueueEntry,
    Recording,
    RuntimeLog,
    Tenant,
    User,
    now,
)
from ats.repositories import TenantRepository
from ats.security import admin, principal
from ats.workspace_service import audit, csv_document

router = APIRouter(prefix="/api/v2", tags=["legacy interface: journal and reports"])
_SUCCESS = {"completed", "done_ok", "operator_ok", "done_agent", "answered"}
_MISSED = {"missed", "failed", "busy", "no_answer", "not_available", "canceled", "cancelled"}
_LIVE_INBOUND = {"ringing", "answered", "accepted", "connected"}


def legacy_calls_query(p):
    """One compatible row shape for current, incoming and imported calls."""
    duration = case(
        (
            Attempt.answered_at.is_not(None) & Attempt.ended_at.is_not(None),
            func.greatest(0, extract("epoch", Attempt.ended_at - Attempt.answered_at)),
        ),
        else_=0,
    )
    current = (
        select(
            Attempt.id.label("id"),
            literal("attempt").label("source"),
            Attempt.created_at.label("started_at"),
            Attempt.ended_at.label("ended_at"),
            func.coalesce(func.nullif(Attempt.contact_name, ""), Contact.name).label("contact_name"),
            func.coalesce(func.nullif(Attempt.phone, ""), Contact.phone).label("contact_phone"),
            cast(Contact.id, String).label("contact_id"),
            Attempt.caller_number.label("caller_id"),
            cast(Campaign.id, String).label("campaign_id"),
            Campaign.name.label("camp_name"),
            Attempt.route.label("provider"),
            literal("out").label("direction"),
            Attempt.status.label("status"),
            func.coalesce(func.nullif(Attempt.result, ""), Attempt.status).label("result"),
            Attempt.reason.label("detail"),
            duration.label("duration_sec"),
            Attempt.external_id.label("external_call_id"),
        )
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
        .where(Attempt.tenant_id == p.tenant_id, ~Attempt.hidden_from_journal)
    )
    incoming = (
        select(
            InboundCall.id,
            literal("inbound"),
            InboundCall.created_at,
            case((InboundCall.status.not_in(_LIVE_INBOUND), InboundCall.updated_at), else_=None),
            func.coalesce(Contact.name, ""),
            InboundCall.phone,
            cast(InboundCall.contact_id, String),
            InboundCall.destination,
            literal(""),
            literal(""),
            InboundCall.route,
            literal("in"),
            InboundCall.status,
            InboundCall.status,
            literal(""),
            InboundCall.duration_seconds,
            InboundCall.external_id,
        )
        .outerjoin(
            Contact,
            (Contact.id == InboundCall.contact_id) & (Contact.tenant_id == InboundCall.tenant_id),
        )
        .where(InboundCall.tenant_id == p.tenant_id, ~InboundCall.hidden_from_journal)
    )
    historical = select(
        HistoricalCall.id,
        literal("historical"),
        HistoricalCall.created_at,
        HistoricalCall.created_at,
        HistoricalCall.contact_name,
        HistoricalCall.phone,
        literal(""),
        literal(""),
        case(
            (HistoricalCall.campaign_name != "", literal("legacy:") + HistoricalCall.campaign_name),
            else_="",
        ),
        HistoricalCall.campaign_name,
        HistoricalCall.route,
        HistoricalCall.direction,
        HistoricalCall.status,
        func.coalesce(func.nullif(HistoricalCall.result, ""), HistoricalCall.status),
        literal(""),
        HistoricalCall.duration_seconds,
        literal(""),
    ).where(HistoricalCall.tenant_id == p.tenant_id, ~HistoricalCall.hidden_from_journal)
    return union_all(current, incoming, historical).subquery("legacy_calls")


def call_row(row):
    item = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    item["campaign_id"] = item["campaign_id"] or 0
    item["duration_sec"] = max(0, int(item["duration_sec"] or 0))
    item["recording"] = item.get("recording", "")
    return jsonable_encoder(item)


def legacy_calls(
    db, p, *, phone=None, limit=500, offset=0, only_campaigns=False, status="", campaign_id=None,
    scope="all",
):
    rows = legacy_calls_query(p)
    query = select(rows)
    if phone:
        query = query.where(rows.c.contact_phone == phone)
    if only_campaigns:
        query = query.where(rows.c.campaign_id != "")
    if status:
        query = query.where(or_(rows.c.status == status, rows.c.result == status))
    if campaign_id:
        query = query.where(rows.c.campaign_id == str(campaign_id))
    if scope == "external":
        query = query.where(rows.c.direction != "internal")
    elif scope == "internal":
        query = query.where(rows.c.direction == "internal")
    elif scope == "mine":
        mine = (
            select(Attempt.id)
            .join(QueueEntry, (QueueEntry.attempt_id == Attempt.id) & (QueueEntry.tenant_id == Attempt.tenant_id))
            .join(Operator, (Operator.id == QueueEntry.operator_id) & (Operator.tenant_id == Attempt.tenant_id))
            .where(Attempt.tenant_id == p.tenant_id, Operator.user_id == p.user_id)
        )
        query = query.where(rows.c.source == "attempt", rows.c.id.in_(mine))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {
        "ok": True,
        "calls": [
            call_row(row)
            for row in db.execute(
                query.order_by(rows.c.started_at.desc(), rows.c.id).offset(offset).limit(limit)
            )
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


class CallsFilter(BaseModel):
    limit: int = Field(500, ge=1, le=2000)
    offset: int = Field(0, ge=0)
    only_campaigns: bool = False
    status: str = Field("", max_length=120)
    campaign_id: UUID | None = None
    scope: str = Field("all", pattern=r"^(all|external|internal|mine)$")


@router.post("/calls")
def calls_post(data: CallsFilter = Body(default_factory=CallsFilter), p=Depends(principal), db=Depends(session)):
    return legacy_calls(db, p, **data.model_dump())


@router.get("/calls")
def calls_get(
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    only_campaigns: bool = False,
    status: str = Query("", max_length=120),
    campaign_id: UUID | None = None,
    scope: str = Query("all", pattern="^(all|external|internal|mine)$"),
    p=Depends(principal),
    db=Depends(session),
):
    return legacy_calls(
        db, p, limit=limit, offset=offset, only_campaigns=only_campaigns,
        status=status, campaign_id=campaign_id, scope=scope,
    )


@router.post("/calls/clear-non-campaign")
def clear_background(p=Depends(admin), db=Depends(session)):
    incoming = db.execute(
        update(InboundCall)
        .where(
            InboundCall.tenant_id == p.tenant_id,
            ~InboundCall.hidden_from_journal,
            InboundCall.status.not_in(_LIVE_INBOUND),
        )
        .values(hidden_from_journal=True)
    ).rowcount
    historical = db.execute(
        update(HistoricalCall)
        .where(
            HistoricalCall.tenant_id == p.tenant_id,
            ~HistoricalCall.hidden_from_journal,
            HistoricalCall.campaign_name == "",
        )
        .values(hidden_from_journal=True)
    ).rowcount
    audit(db, p, "call.background_cleared")
    db.commit()
    return {"ok": True, "cleared": incoming + historical}

@router.post("/calls/reconcile-stale")
def reconcile_stale_calls(p=Depends(admin), db=Depends(session)):
    """Close provider calls that have had no state update for 15 minutes."""
    cutoff = now() - timedelta(minutes=15)
    rows = db.scalars(select(Attempt).where(Attempt.tenant_id == p.tenant_id, Attempt.status.in_(ACTIVE), Attempt.updated_at < cutoff).with_for_update()).all()
    for row in rows:
        row.status, row.reason, row.result, row.ended_at = "failed", "stale_call_reconciled", "failed", now()
        item = db.get(CampaignItem, row.item_id)
        if item and item.status == "active":
            item.status = "exhausted"
    audit(db, p, "call.stale_reconciled")
    db.commit()
    return {"ok": True, "reconciled": len(rows)}


@router.post("/calls/clear-journal")
def clear_journal(p=Depends(admin), db=Depends(session)):
    """Hide all terminal calls from the journal while retaining audit data."""
    active_attempts = db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.tenant_id == p.tenant_id, Attempt.status.in_(ACTIVE))
    )
    active_inbound = db.scalar(
        select(func.count())
        .select_from(InboundCall)
        .where(InboundCall.tenant_id == p.tenant_id, InboundCall.status.in_(_LIVE_INBOUND))
    )
    if active_attempts or active_inbound:
        raise Problem(409, "active_calls_must_be_reconciled")
    attempts = db.execute(
        update(Attempt)
        .where(
            Attempt.tenant_id == p.tenant_id,
            ~Attempt.hidden_from_journal,
            Attempt.status.not_in(ACTIVE),
        )
        .values(hidden_from_journal=True)
    ).rowcount
    incoming = db.execute(
        update(InboundCall)
        .where(InboundCall.tenant_id == p.tenant_id, ~InboundCall.hidden_from_journal)
        .values(hidden_from_journal=True)
    ).rowcount
    historical = db.execute(
        update(HistoricalCall)
        .where(HistoricalCall.tenant_id == p.tenant_id, ~HistoricalCall.hidden_from_journal)
        .values(hidden_from_journal=True)
    ).rowcount
    audit(db, p, "call.journal_cleared")
    db.commit()
    return {"ok": True, "cleared": attempts + incoming + historical,
            "attempts": attempts, "inbound": incoming, "historical": historical}


@router.get("/calls/{call_id}/timeline")
def timeline(call_id: UUID, p=Depends(principal), db=Depends(session)):
    rows = legacy_calls_query(p)
    call = db.execute(select(rows).where(rows.c.id == call_id)).first()
    if not call:
        raise Problem(404, "not_found")
    call = call_row(call)
    repo = TenantRepository(db, p.tenant_id)
    events = []
    if call["source"] == "attempt":
        for event in db.scalars(
            repo.query(CallEvent).where(CallEvent.attempt_id == call_id)
            .order_by(CallEvent.created_at, CallEvent.id).limit(2000)
        ):
            events.append({
                "fingerprint": str(event.id), "event_type": event.event,
                "received_at": event.created_at, "detail": event.detail,
                "status": "processed", "payload": {"detail": event.detail},
            })
    if call["external_call_id"]:
        for event in db.scalars(
            repo.query(Inbox).where(
                Inbox.external_id == call["external_call_id"], Inbox.route == call["provider"],
            ).order_by(Inbox.received_at, Inbox.id).limit(2000)
        ):
            # Provider payloads can contain tokens and personal metadata. The
            # timeline exposes only the normalized event envelope.
            events.append({
                "fingerprint": event.event_key, "event_type": event.event,
                "external_call_id": event.external_id, "received_at": event.received_at,
                "status": event.status, "payload": {},
            })
    events.sort(key=lambda e: (e["received_at"], e["fingerprint"]))
    recordings = db.scalars(
        repo.query(Recording).where(Recording.attempt_id == call_id)
        .order_by(Recording.created_at).limit(100)
    ).all()
    acd = db.scalars(repo.query(QueueEntry).where(QueueEntry.attempt_id == call_id)).all()
    return {
        "ok": True, "call": call, "events": jsonable_encoder(events),
        "acd": jsonable_encoder(acd),
        "recordings": [
            {"id": r.id, "url": f"/api/v2/recordings/{r.id}", "content_type": r.content_type,
             "size_bytes": r.size_bytes} for r in recordings
        ],
    }


@router.get("/recordings/{recording_id}")
def recording(recording_id: UUID, p=Depends(principal), db=Depends(session)):
    return api_calls.recording_download(recording_id=recording_id, p=p, db=db)


@router.get("/calls/{call_id}/recording")
def call_recording(call_id: UUID, p=Depends(principal), db=Depends(session)):
    repo = TenantRepository(db, p.tenant_id)
    repo.get(Attempt, call_id)
    row = db.scalar(
        repo.query(Recording).where(Recording.attempt_id == call_id)
        .order_by(Recording.created_at.desc()).limit(1)
    )
    if not row:
        raise Problem(404, "recording_not_found")
    return api_calls.recording_download(recording_id=row.id, p=p, db=db)


class RecordingUpload(BaseModel):
    call_id: UUID
    filename: str = Field("recording.wav", max_length=240)
    data_b64: str = Field(max_length=14 * 1024 * 1024)


@router.post("/calls/recording")
async def recording_upload(data: RecordingUpload, p=Depends(admin), db=Depends(session)):
    try:
        content = base64.b64decode(data.data_b64, validate=True)
    except (ValueError, binascii.Error):
        raise Problem(422, "invalid_recording_base64") from None
    if len(content) > 10 * 1024 * 1024:
        raise Problem(413, "recording_too_large")
    row = await api_calls.recording_upload(
        attempt_id=data.call_id, request=None,
        file=UploadFile(filename=data.filename, file=io.BytesIO(content)), p=p, db=db,
    )
    return {"ok": True, "recording": f"/api/v2/recordings/{row.id}", "id": row.id}


def _period(db, p, from_date=None, to_date=None):
    timezone = ZoneInfo(db.get(Tenant, p.tenant_id).timezone)
    today = now().astimezone(timezone).date()
    try:
        start = date.fromisoformat(from_date) if from_date else today
        end = date.fromisoformat(to_date) if to_date else today
    except ValueError:
        raise Problem(400, "bad_range") from None
    if start > end or (end - start).days > 366:
        raise Problem(400, "bad_range")
    return (
        start, end, timezone,
        datetime.combine(start, time.min, timezone),
        datetime.combine(end + timedelta(days=1), time.min, timezone),
    )


@router.get("/dashboard")
def dashboard(p=Depends(principal), db=Depends(session)):
    from ats.connections import settings_for_tenant

    config = settings_for_tenant(db, p.tenant_id)
    _, _, _, start, end = _period(db, p)
    rows = legacy_calls_query(p)
    today = db.execute(
        select(func.count(), func.count().filter(rows.c.result.in_(_SUCCESS)))
        .select_from(rows).where(rows.c.started_at >= start, rows.c.started_at < end)
    ).one()
    active = db.execute(
        select(rows).where(
            or_(
                (rows.c.source == "attempt") & rows.c.status.in_(ACTIVE),
                (rows.c.source == "inbound") & rows.c.status.in_(_LIVE_INBOUND),
            )
        ).order_by(rows.c.started_at.desc()).limit(100)
    ).all()
    repo = TenantRepository(db, p.tenant_id)
    numbers = db.execute(
        select(func.count(), func.count().filter(
            CallerNumber.enabled & CallerNumber.verified & ~CallerNumber.quarantined
        )).where(CallerNumber.tenant_id == p.tenant_id)
    ).one()
    operators = db.scalars(repo.query(Operator).order_by(Operator.name).limit(1000)).all()
    items = dict(db.execute(
        select(CampaignItem.status, func.count()).where(CampaignItem.tenant_id == p.tenant_id)
        .group_by(CampaignItem.status)
    ).all())
    return {
        "ok": True,
        "campaigns_running": db.scalar(select(func.count()).select_from(Campaign).where(
            Campaign.tenant_id == p.tenant_id, Campaign.status == "running", ~Campaign.archived,
        )),
        "calls_today": today[0], "ok_today": today[1], "items": items,
        "active_channels": len(active), "active_channels_count": len(active),
        "active_calls": [call_row(row) for row in active],
        "numbers_summary": {"total_count": numbers[0], "active_count": numbers[1]},
        "operators": jsonable_encoder(operators),
        "acd_queued": db.scalar(select(func.count()).select_from(QueueEntry).where(
            QueueEntry.tenant_id == p.tenant_id, QueueEntry.status == "waiting",
        )),
        "live_calls": config.live_calls_enabled and config.telephony_tenant_id == p.tenant_id,
    }


@router.get("/reports")
def reports(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    p=Depends(principal), db=Depends(session),
):
    start_d, end_d, timezone, start, end = _period(db, p, from_date, to_date)
    rows = legacy_calls_query(p)
    period = select(rows).where(rows.c.started_at >= start, rows.c.started_at < end).subquery()
    n, ok, dur = db.execute(select(
        func.count(), func.count().filter(period.c.result.in_(_SUCCESS)),
        func.coalesce(func.sum(period.c.duration_sec), 0),
    ).select_from(period)).one()
    result_rows = db.execute(
        select(period.c.result, func.count().label("c"))
        .group_by(period.c.result).order_by(func.count().desc())
    ).all()
    by_campaign = db.execute(select(
        period.c.campaign_id, period.c.camp_name.label("name"), func.count().label("n"),
        func.count().filter(period.c.result.in_(_SUCCESS)).label("ok"),
        func.count().filter(period.c.status == "failed").label("bad"),
        func.sum(period.c.duration_sec).label("dur"),
    ).group_by(period.c.campaign_id, period.c.camp_name).order_by(func.count().desc()).limit(25)).all()
    by_number = db.execute(select(
        period.c.caller_id, func.count().label("n"),
        func.count().filter(period.c.result.in_(_SUCCESS)).label("ok"),
    ).group_by(period.c.caller_id).order_by(func.count().desc()).limit(25)).all()
    custom = bool(from_date or to_date)
    trend_start = start_d if custom else start_d - timedelta(days=6)
    trend_query_start = datetime.combine(trend_start, time.min, timezone)
    day = cast(func.timezone(str(timezone), rows.c.started_at), __import__("sqlalchemy").Date)
    day_counts = {
        d: (count, success) for d, count, success in db.execute(select(
            day.label("day"), func.count(), func.count().filter(rows.c.result.in_(_SUCCESS)),
        ).where(rows.c.started_at >= trend_query_start, rows.c.started_at < end).group_by(day))
    }
    step = 7 if custom and (end_d - start_d).days >= 31 else 1
    trend = []
    cursor = trend_start
    while cursor <= end_d:
        chunk_end = min(end_d, cursor + timedelta(days=step - 1))
        days = [cursor + timedelta(days=i) for i in range((chunk_end - cursor).days + 1)]
        trend.append({
            "date": cursor.isoformat(),
            "label": cursor.strftime("%d.%m") if step == 1 else
            f"{cursor:%d.%m}–{chunk_end:%d.%m}",
            "n": sum(day_counts.get(d, (0, 0))[0] for d in days),
            "ok": sum(day_counts.get(d, (0, 0))[1] for d in days),
        })
        cursor = chunk_end + timedelta(days=1)
    return jsonable_encoder({
        "ok": True,
        "period": {"from": start_d, "to": end_d, "custom": custom,
                   "label": f"{start_d} – {end_d}" if custom else "сегодня"},
        "today": {"n": n, "ok": ok, "ok_rate": round(100 * ok / n, 1) if n else 0,
                  "avg_dur": round(float(dur) / n, 1) if n else 0},
        "by_result": [dict(r._mapping) for r in result_rows],
        "by_campaign": [dict(r._mapping) for r in by_campaign],
        "by_number": [dict(r._mapping) for r in by_number], "trend": trend,
    })


def _analytics_result(rows):
    return func.lower(func.coalesce(func.nullif(rows.c.result, ""), rows.c.status))


def _analytics_direction(rows):
    return func.lower(func.coalesce(func.nullif(rows.c.direction, ""), "out"))


def _analytics_period(
    rows, p, start, end, *, direction="", route="", result_filter="", number="",
    operator_id=None, campaign_id=None,
):
    query = select(rows).where(rows.c.started_at >= start, rows.c.started_at < end)
    direction_expr = _analytics_direction(rows)
    result_expr = _analytics_result(rows)
    if direction:
        query = query.where(direction_expr == direction)
    if route:
        query = query.where(rows.c.provider == route)
    if result_filter:
        query = query.where(result_expr == result_filter)
    if number:
        query = query.where(or_(rows.c.contact_phone.contains(number), rows.c.caller_id.contains(number)))
    if campaign_id:
        query = query.where(rows.c.campaign_id == str(campaign_id))
    if operator_id:
        operator_attempts = (
            select(Attempt.id)
            .join(
                QueueEntry,
                (QueueEntry.attempt_id == Attempt.id) & (QueueEntry.tenant_id == Attempt.tenant_id),
            )
            .where(
                Attempt.tenant_id == p.tenant_id,
                QueueEntry.operator_id == operator_id,
            )
        )
        query = query.where(rows.c.source == "attempt", rows.c.id.in_(operator_attempts))
    return query.subquery("analytics_period")


def _analytics_summary(db, period):
    result = _analytics_result(period)
    direction = _analytics_direction(period)
    success = result.in_(_SUCCESS)
    missed = result.in_(_MISSED)
    unique_phone = func.nullif(period.c.contact_phone, "")
    incoming_phone = case((direction == "in", unique_phone))
    missed_phone = case((missed, unique_phone))
    total, successful, missed_count, duration, unique_numbers, incoming, unique_incoming, unique_missed = db.execute(
        select(
            func.count(),
            func.count().filter(success),
            func.count().filter(missed),
            func.coalesce(func.sum(period.c.duration_sec), 0),
            func.count(func.distinct(unique_phone)),
            func.count().filter(direction == "in"),
            func.count(func.distinct(incoming_phone)),
            func.count(func.distinct(missed_phone)),
        ).select_from(period)
    ).one()
    total = int(total or 0)
    successful = int(successful or 0)
    missed_count = int(missed_count or 0)
    duration = int(duration or 0)
    unique_numbers = int(unique_numbers or 0)
    incoming = int(incoming or 0)
    unique_incoming = int(unique_incoming or 0)
    unique_missed = int(unique_missed or 0)
    outgoing = total - incoming
    return {
        "total": total,
        "successful": successful,
        "missed": missed_count,
        "pending": max(0, total - successful - missed_count),
        "incoming": incoming,
        "outgoing": outgoing,
        "internal": int(db.scalar(
            select(func.count()).select_from(period).where(direction == "internal")
        ) or 0),
        "duration_seconds": duration,
        "duration_minutes": round(duration / 60, 1),
        "average_duration_seconds": round(duration / total, 1) if total else 0,
        "answer_rate": round(100 * successful / total, 1) if total else 0,
        "unique_numbers": unique_numbers,
        "unique_incoming": unique_incoming,
        "unique_missed": unique_missed,
    }


def _analytics_delta(current, previous):
    keys = ("total", "successful", "missed", "duration_seconds", "unique_numbers", "answer_rate")
    result = {}
    for key in keys:
        old, new = float(previous.get(key) or 0), float(current.get(key) or 0)
        result[key] = round((new - old) / old * 100, 1) if old else (100.0 if new else 0.0)
    return result


@router.get("/analytics")
def analytics(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    direction: str = Query("", pattern="^(|in|out|internal)$"),
    route: str = Query("", max_length=32),
    result_filter: str = Query("", alias="result", max_length=120),
    number: str = Query("", max_length=32),
    operator_id: UUID | None = Query(None),
    campaign_id: UUID | None = Query(None),
    p=Depends(principal), db=Depends(session),
):
    """MegaFon-style tenant analytics over every normalized ATS call source."""
    start_d, end_d, timezone, start, end = _period(db, p, from_date, to_date)
    rows = legacy_calls_query(p)
    period = _analytics_period(
        rows, p, start, end, direction=direction, route=route, result_filter=result_filter.lower(),
        number=number, operator_id=operator_id, campaign_id=campaign_id,
    )
    local_started = func.timezone(str(timezone), period.c.started_at)
    result = _analytics_result(period)
    direction_expr = _analytics_direction(period)
    success = result.in_(_SUCCESS)
    missed = result.in_(_MISSED)
    month_expr = cast(func.date_trunc("month", local_started), Date)
    day_expr = cast(local_started, Date)
    number_expr = func.coalesce(
        func.nullif(case((direction_expr == "in", period.c.caller_id), else_=period.c.contact_phone), ""),
        "—",
    )
    incoming_phone = case((direction_expr == "in", func.nullif(period.c.contact_phone, "")))
    missed_phone = case((missed, func.nullif(period.c.contact_phone, "")))

    summary = _analytics_summary(db, period)
    span_days = (end_d - start_d).days + 1
    previous_end_d = start_d - timedelta(days=1)
    previous_start_d = previous_end_d - timedelta(days=span_days - 1)
    previous_period = _analytics_period(
        rows, p,
        datetime.combine(previous_start_d, time.min, timezone),
        datetime.combine(previous_end_d + timedelta(days=1), time.min, timezone),
        direction=direction, route=route, result_filter=result_filter.lower(),
        number=number, operator_id=operator_id, campaign_id=campaign_id,
    )
    previous = _analytics_summary(db, previous_period)

    direction_rows = db.execute(
        select(
            direction_expr.label("direction"),
            func.count().label("calls"),
            func.count().filter(success).label("successful"),
            func.count().filter(missed).label("missed"),
            func.count().filter(direction_expr == "in").label("incoming"),
            func.count().filter(direction_expr == "out").label("outgoing"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        ).select_from(period).group_by(direction_expr).order_by(func.count().desc())
    ).all()
    direction_map = {str(row.direction): dict(row._mapping) for row in direction_rows}
    direction_rows_json = [
        direction_map.get(key, {
            "direction": key, "calls": 0, "successful": 0, "missed": 0,
            "incoming": 0, "outgoing": 0, "duration_seconds": 0,
        })
        for key in ("in", "out", "internal")
    ]

    result_rows = db.execute(
        select(result.label("result"), func.count().label("calls"),
               func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"))
        .select_from(period).group_by(result).order_by(func.count().desc())
    ).all()
    month_rows = db.execute(
        select(
            month_expr.label("month"), func.count().label("calls"),
            func.count().filter(success).label("successful"), func.count().filter(missed).label("missed"),
            func.count().filter(direction_expr == "in").label("incoming"),
            func.count().filter(direction_expr == "out").label("outgoing"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        ).select_from(period).group_by(month_expr).order_by(month_expr)
    ).all()
    month_map = {str(row.month)[:7]: dict(row._mapping) for row in month_rows}
    months = []
    cursor = date(start_d.year, start_d.month, 1)
    while cursor <= end_d:
        key = cursor.strftime("%Y-%m")
        row = month_map.get(key, {"month": cursor, "calls": 0, "successful": 0, "missed": 0,
                                  "incoming": 0, "outgoing": 0, "duration_seconds": 0})
        row["month"] = key
        row["minutes"] = round(float(row.get("duration_seconds") or 0) / 60, 1)
        months.append(row)
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)

    day_rows = db.execute(
        select(
            day_expr.label("day"), func.count().label("calls"),
            func.count().filter(success).label("successful"), func.count().filter(missed).label("missed"),
            func.count().filter(direction_expr == "in").label("incoming"),
            func.count().filter(direction_expr == "out").label("outgoing"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        ).select_from(period).group_by(day_expr).order_by(day_expr)
    ).all()
    day_map = {str(row.day): dict(row._mapping) for row in day_rows}
    days = []
    for index in range(span_days):
        day = start_d + timedelta(days=index)
        row = day_map.get(day.isoformat(), {"day": day, "calls": 0, "successful": 0,
                                             "missed": 0, "incoming": 0, "outgoing": 0,
                                             "duration_seconds": 0})
        row["day"] = day.isoformat()
        row["minutes"] = round(float(row.get("duration_seconds") or 0) / 60, 1)
        days.append(row)

    number_rows = db.execute(
        select(
            number_expr.label("number"), func.count().label("calls"),
            func.count().filter(success).label("successful"),
            func.count().filter(direction_expr == "in").label("incoming"),
            func.count().filter(missed).label("missed"),
            func.count(func.distinct(incoming_phone)).label("unique_incoming"),
            func.count(func.distinct(missed_phone)).label("unique_missed"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        ).select_from(period).group_by(number_expr).order_by(func.count().desc()).limit(500)
    ).all()

    hour_expr = cast(extract("hour", local_started), Integer)
    dow_expr = cast(extract("dow", local_started), Integer)
    hour_rows = db.execute(
        select(
            dow_expr.label("weekday_provider"), hour_expr.label("hour"), func.count().label("calls"),
            func.count().filter(success).label("successful"), func.count().filter(missed).label("missed"),
            func.count().filter(direction_expr == "in").label("incoming"),
            func.count().filter(direction_expr == "out").label("outgoing"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        ).select_from(period).group_by(dow_expr, hour_expr).order_by(dow_expr, hour_expr)
    ).all()
    heatmap = {
        (day, hour): {"weekday": day, "hour": hour, "calls": 0, "successful": 0,
                      "missed": 0, "incoming": 0, "outgoing": 0, "duration_seconds": 0}
        for day in range(7) for hour in range(24)
    }
    for row in hour_rows:
        weekday = (int(row.weekday_provider) + 6) % 7
        item = dict(row._mapping)
        item.pop("weekday_provider", None)
        item["weekday"] = weekday
        heatmap[(weekday, int(row.hour))] = item
    heatmap_rows = [heatmap[key] for key in sorted(heatmap)]
    positive = [int(item["calls"]) for item in heatmap_rows if item["calls"]]
    peak = max(heatmap_rows, key=lambda item: item["calls"]) if positive else None
    weekday_totals = [sum(item["calls"] for item in heatmap_rows if item["weekday"] == day) for day in range(7)]
    hour_totals = [sum(item["calls"] for item in heatmap_rows if item["hour"] == hour) for hour in range(24)]

    employee_rows = []
    if direction != "in" and direction != "internal":
        employee_result = func.lower(func.coalesce(func.nullif(Attempt.result, ""), Attempt.status))
        talk_duration = case(
            (Attempt.answered_at.is_not(None) & Attempt.ended_at.is_not(None),
             func.greatest(0, extract("epoch", Attempt.ended_at - Attempt.answered_at))),
            else_=0,
        )
        employee_query = (
            select(
                Operator.id.label("id"), Operator.name, Operator.extension,
                func.count(Attempt.id).label("calls"),
                func.count(Attempt.id).filter(employee_result.in_(_SUCCESS)).label("successful"),
                func.coalesce(func.sum(talk_duration), 0).label("duration_seconds"),
            ).select_from(Attempt)
            .join(QueueEntry, (QueueEntry.attempt_id == Attempt.id) & (QueueEntry.tenant_id == Attempt.tenant_id))
            .join(Operator, (Operator.id == QueueEntry.operator_id) & (Operator.tenant_id == Attempt.tenant_id))
            .where(Attempt.tenant_id == p.tenant_id, Attempt.created_at >= start, Attempt.created_at < end)
            .group_by(Operator.id, Operator.name, Operator.extension)
            .order_by(func.count(Attempt.id).desc()).limit(200)
        )
        if route:
            employee_query = employee_query.where(Attempt.route == route)
        if result_filter:
            employee_query = employee_query.where(employee_result == result_filter.lower())
        if number:
            employee_query = employee_query.where(Attempt.phone.contains(number))
        if operator_id:
            employee_query = employee_query.where(QueueEntry.operator_id == operator_id)
        if campaign_id:
            employee_query = employee_query.where(Attempt.item_id.in_(
                select(CampaignItem.id).where(CampaignItem.tenant_id == p.tenant_id,
                                               CampaignItem.campaign_id == campaign_id)
            ))
        rows = db.execute(employee_query).all()
        total_days = max(1, span_days)
        for rank, row in enumerate(rows, 1):
            calls = int(row.calls or 0)
            successful_calls = int(row.successful or 0)
            seconds = int(row.duration_seconds or 0)
            employee_rows.append({
                "rank": rank, "id": row.id, "name": row.name or "Без имени",
                "extension": row.extension or "—", "calls": calls,
                "total_successful": successful_calls, "incoming_successful": 0,
                "outgoing_successful": successful_calls, "calls_per_day": round(calls / total_days, 1),
                "calls_per_hour": round(calls / (total_days * 24), 2),
                "duration_seconds": seconds, "average_duration_seconds": round(seconds / calls, 1) if calls else 0,
                "load_percent": round(100 * seconds / (total_days * 8 * 3600), 1),
            })
    average_calls = round(sum(row["calls"] for row in employee_rows) / len(employee_rows), 1) if employee_rows else 0
    for row in employee_rows:
        row["above_average"] = row["calls"] > average_calls
    average_employee = {
        "name": "Средний сотрудник", "calls": round(average_calls),
        "total_successful": round(sum(row["total_successful"] for row in employee_rows) / len(employee_rows)) if employee_rows else 0,
        "incoming_successful": 0,
        "outgoing_successful": round(sum(row["outgoing_successful"] for row in employee_rows) / len(employee_rows)) if employee_rows else 0,
        "calls_per_day": round(sum(row["calls_per_day"] for row in employee_rows) / len(employee_rows), 1) if employee_rows else 0,
        "calls_per_hour": round(sum(row["calls_per_hour"] for row in employee_rows) / len(employee_rows), 2) if employee_rows else 0,
        "load_percent": round(sum(row["load_percent"] for row in employee_rows) / len(employee_rows), 1) if employee_rows else 0,
    }

    return jsonable_encoder({
        "ok": True,
        "period": {"from": start_d, "to": end_d, "days": span_days, "timezone": str(timezone)},
        "filters": {"direction": direction, "route": route, "result": result_filter, "number": number,
                    "operator_id": operator_id, "campaign_id": campaign_id},
        "summary": summary,
        "previous": {"period": {"from": previous_start_d, "to": previous_end_d}, "summary": previous,
                      "delta_percent": _analytics_delta(summary, previous)},
        "by_month": months,
        "by_direction": direction_rows_json,
        "by_direction_month": [dict(row._mapping) for row in db.execute(
            select(direction_expr.label("direction"), month_expr.label("month"), func.count().label("calls"),
                   func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"))
            .select_from(period).group_by(direction_expr, month_expr).order_by(month_expr, direction_expr)
        ).all()],
        "by_result": [dict(row._mapping) for row in result_rows],
        "by_number": [dict(row._mapping) for row in number_rows],
        "by_day": days,
        "by_hour": heatmap_rows,
        "hour_summary": {"weekday_totals": weekday_totals, "hour_totals": hour_totals,
                         "max": max(positive, default=0), "min_positive": min(positive, default=0),
                         "peak": peak},
        "by_employee": employee_rows,
        "employee_average": average_employee,
    })


@router.get("/export/analytics.csv")
def export_analytics(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    direction: str = Query("", pattern="^(|in|out|internal)$"),
    route: str = Query("", max_length=32),
    result_filter: str = Query("", alias="result", max_length=120),
    number: str = Query("", max_length=32),
    operator_id: UUID | None = Query(None), campaign_id: UUID | None = Query(None),
    p=Depends(admin), db=Depends(session),
):
    report = analytics(
        from_date, to_date, direction, route, result_filter, number, operator_id, campaign_id, p, db,
    )
    sections = [
        ("По месяцам", ["month", "calls", "successful", "missed", "incoming", "outgoing", "minutes"], report["by_month"]),
        ("По направлениям", ["direction", "calls", "successful", "missed", "duration_seconds"], report["by_direction"]),
        ("Рейтинг сотрудников", ["rank", "name", "extension", "calls", "total_successful", "calls_per_day", "calls_per_hour", "load_percent"], report["by_employee"]),
        ("По номерам", ["number", "incoming", "missed", "unique_incoming", "unique_missed", "calls"], report["by_number"]),
        ("Нагрузка по часам", ["weekday", "hour", "calls", "incoming", "outgoing", "successful", "missed"], report["by_hour"]),
    ]
    csv_rows = []
    for title, headers, items in sections:
        csv_rows.append([title] + headers)
        csv_rows.extend([[title] + [item.get(key, "") for key in headers] for item in items])
        csv_rows.append([])
    content = csv_document(["section", "metric", "value", "value_2", "value_3", "value_4", "value_5", "value_6", "value_7"], csv_rows)
    audit(db, p, "export.analytics.csv")
    db.commit()
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="analytics.csv"'})


@router.get("/export/calls.csv")
def export_calls(p=Depends(admin), db=Depends(session)):
    rows = legacy_calls_query(p)
    query = select(rows).order_by(rows.c.started_at.desc(), rows.c.id).limit(100000)
    content = csv_document(
        ["id", "started_at", "ended_at", "name", "phone", "caller_id", "campaign_id",
         "direction", "result", "detail", "duration_sec", "recording"],
        ([r.id, r.started_at, r.ended_at or "", r.contact_name, r.contact_phone, r.caller_id,
          r.campaign_id or 0, r.direction, r.result, r.detail, int(r.duration_sec or 0), ""]
         for r in db.execute(query.execution_options(yield_per=1000))),
    )
    audit(db, p, "export.calls.csv")
    db.commit()
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="calls.csv"'})


def _event_category(action):
    return case(
        (action.like("user.%"), "auth"),
        (action.like("number.%"), "settings"),
        (action.like("template.%"), "campaign"),
        (action.like("list.%"), "contact"),
        (action.like("queue.%"), "operator"),
        (action.like("diagnostic.%"), "integration"),
        (action.like("directory.%"), "integration"),
        (action.like("job.%"), "integration"),
        else_=func.split_part(action, ".", 1),
    )


def _logs_query(p):
    success = select(
        Audit.id.label("id"), literal("audit").label("source"),
        Audit.created_at.label("created"), func.coalesce(User.login, "").label("actor_login"),
        func.coalesce(User.role, "").label("actor_role"),
        _event_category(Audit.action).label("event_type"), Audit.action.label("action"),
        func.split_part(Audit.action, ".", 1).label("entity_type"),
        cast(Audit.entity_id, String).label("entity_id"),
        literal("success").label("status"), literal("").label("error_code"),
        literal("").label("path"), literal("").label("method"),
        literal("").label("request_id"), literal(None).cast(String).label("http_status"),
    ).outerjoin(User, (User.id == Audit.actor_id) & (User.tenant_id == Audit.tenant_id)).where(
        Audit.tenant_id == p.tenant_id
    )
    runtime = select(
        RuntimeLog.id, literal("runtime"), RuntimeLog.created_at, literal(""), literal(""),
        case((RuntimeLog.component == "api", "auth"), else_="integration"),
        RuntimeLog.code, RuntimeLog.component, cast(RuntimeLog.entity_id, String),
        case((RuntimeLog.level.in_(["ERROR", "WARNING"]), "failure"), else_="success"),
        case((RuntimeLog.level.in_(["ERROR", "WARNING"]), RuntimeLog.code), else_=""),
        RuntimeLog.path, RuntimeLog.method, cast(RuntimeLog.request_id, String),
        cast(RuntimeLog.http_status, String),
    ).where(RuntimeLog.tenant_id == p.tenant_id)
    failures = select(
        Outbox.id, literal("outbox"), Outbox.available_at, literal(""), literal(""),
        literal("integration"), literal("integration.delivery_failed"), Outbox.kind,
        cast(Outbox.id, String), literal("failure"), Outbox.last_error,
        literal(""), literal(""), literal(""), literal("")
    ).where(Outbox.tenant_id == p.tenant_id, Outbox.status.in_(["dead", "unknown"]))
    return union_all(success, runtime, failures).subquery("legacy_audit")


@router.get("/audit/logs")
def audit_logs(
    q: str = Query("", max_length=120), event: str = Query("", max_length=32),
    status: str = Query("", max_length=16),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    p=Depends(admin), db=Depends(session),
):
    rows = _logs_query(p)
    query = select(rows)
    if q:
        query = query.where(or_(
            rows.c.action.ilike(f"%{q}%"), rows.c.actor_login.ilike(f"%{q}%"),
            rows.c.error_code.ilike(f"%{q}%"), rows.c.request_id.contains(q),
        ))
    if event:
        query = query.where(rows.c.event_type == event)
    if status:
        if status not in {"success", "failure"}:
            raise Problem(422, "invalid_log_status")
        query = query.where(rows.c.status == status)
    if from_date or to_date:
        _, _, _, start, end = _period(db, p, from_date, to_date)
        query = query.where(rows.c.created >= start, rows.c.created < end)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    logs = []
    for row in db.execute(query.order_by(rows.c.created.desc(), rows.c.id).offset(offset).limit(limit)):
        item = dict(row._mapping)
        item["id"] = f"{item['source']}:{item['id']}"
        item["details"] = {key: item[key] for key in ("request_id", "method", "http_status") if item[key]}
        logs.append(item)
    return jsonable_encoder({"ok": True, "logs": logs, "total": total, "offset": offset, "limit": limit})
