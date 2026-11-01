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
        .where(Attempt.tenant_id == p.tenant_id)
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


@router.get("/analytics")
def analytics(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    p=Depends(principal), db=Depends(session),
):
    """Tenant-scoped call analytics used by the workspace statistics tabs.

    The provider UI exposes several views over the same call history.  Keep
    one normalized query here so totals, direction, employee, caller-number
    and hourly reports cannot drift apart or expose another tenant's rows.
    """
    start_d, end_d, timezone, start, end = _period(db, p, from_date, to_date)
    rows = legacy_calls_query(p)
    period = select(rows).where(rows.c.started_at >= start, rows.c.started_at < end).subquery()
    success = period.c.result.in_(_SUCCESS)
    local_started = func.timezone(str(timezone), period.c.started_at)

    total, successful, missed, duration, unique_numbers = db.execute(
        select(
            func.count(),
            func.count().filter(success),
            func.count().filter(period.c.result.in_(_MISSED)),
            func.coalesce(func.sum(period.c.duration_sec), 0),
            func.count(func.distinct(func.nullif(period.c.contact_phone, ""))),
        ).select_from(period)
    ).one()
    direction_rows = db.execute(
        select(
            period.c.direction,
            func.count().label("calls"),
            func.count().filter(success).label("successful"),
            func.count().filter(period.c.result.in_(_MISSED)).label("missed"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        )
        .select_from(period)
        .group_by(period.c.direction)
        .order_by(func.count().desc())
    ).all()
    result_rows = db.execute(
        select(period.c.result, func.count().label("calls"))
        .select_from(period)
        .group_by(period.c.result)
        .order_by(func.count().desc())
    ).all()
    number_rows = db.execute(
        select(
            func.coalesce(func.nullif(period.c.contact_phone, ""), "—").label("number"),
            func.count().label("calls"),
            func.count().filter(success).label("successful"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        )
        .select_from(period)
        .group_by(func.coalesce(func.nullif(period.c.contact_phone, ""), "—"))
        .order_by(func.count().desc())
        .limit(200)
    ).all()
    day_rows = db.execute(
        select(
            cast(local_started, Date).label("day"),
            func.count().label("calls"),
            func.count().filter(success).label("successful"),
            func.coalesce(func.sum(period.c.duration_sec), 0).label("duration_seconds"),
        )
        .select_from(period)
        .group_by(cast(local_started, Date))
        .order_by(cast(local_started, Date))
    ).all()
    hour_rows = db.execute(
        select(
            cast(extract("dow", local_started), Integer).label("weekday"),
            cast(extract("hour", local_started), Integer).label("hour"),
            func.count().label("calls"),
        )
        .select_from(period)
        .group_by(
            cast(extract("dow", local_started), Integer),
            cast(extract("hour", local_started), Integer),
        )
        .order_by("weekday", "hour")
    ).all()
    employee_rows = db.execute(
        select(
            Operator.id.label("id"),
            Operator.name,
            Operator.extension,
            func.count(Attempt.id).label("calls"),
            func.count(Attempt.id).filter(
                func.coalesce(func.nullif(Attempt.result, ""), Attempt.status).in_(_SUCCESS)
            ).label("successful"),
            func.coalesce(func.sum(
                case(
                    (Attempt.answered_at.is_not(None) & Attempt.ended_at.is_not(None),
                     func.greatest(0, extract("epoch", Attempt.ended_at - Attempt.answered_at))),
                    else_=0,
                )
            ), 0).label("duration_seconds"),
        )
        .select_from(Attempt)
        .join(QueueEntry, (QueueEntry.attempt_id == Attempt.id) & (QueueEntry.tenant_id == Attempt.tenant_id))
        .join(Operator, (Operator.id == QueueEntry.operator_id) & (Operator.tenant_id == Attempt.tenant_id))
        .where(Attempt.tenant_id == p.tenant_id, Attempt.created_at >= start, Attempt.created_at < end)
        .group_by(Operator.id, Operator.name, Operator.extension)
        .order_by(func.count(Attempt.id).desc())
        .limit(200)
    ).all()

    return jsonable_encoder({
        "ok": True,
        "period": {"from": start_d, "to": end_d, "timezone": str(timezone)},
        "summary": {
            "total": total,
            "successful": successful,
            "missed": missed,
            "duration_seconds": duration,
            "average_duration_seconds": round(float(duration) / total, 1) if total else 0,
            "unique_numbers": unique_numbers,
        },
        "by_direction": [dict(r._mapping) for r in direction_rows],
        "by_result": [dict(r._mapping) for r in result_rows],
        "by_number": [dict(r._mapping) for r in number_rows],
        "by_day": [dict(r._mapping) for r in day_rows],
        "by_hour": [dict(r._mapping) for r in hour_rows],
        "by_employee": [dict(r._mapping) for r in employee_rows],
    })


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
