from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    max_channels: Mapped[int] = mapped_column(default=3)
    daily_limit: Mapped[int] = mapped_column(default=30)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    __table_args__ = (CheckConstraint("max_channels BETWEEN 1 AND 100"),)


class Owned:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)


class User(Owned, Base):
    __tablename__ = "users"
    login: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(20), default="admin")
    active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "login"),
        CheckConstraint("role IN ('admin','operator','viewer')"),
    )


class LoginBucket(Base):
    __tablename__ = "login_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Session(Owned, Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[UUID]
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
    )


class Contact(Owned, Base):
    __tablename__ = "contacts"
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(16))
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_source: Mapped[str] = mapped_column(String(256), default="")
    blocked: Mapped[bool] = mapped_column(default=False)
    crm_contact_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    group: Mapped[str] = mapped_column(String(120), default="", server_default="")
    email: Mapped[str] = mapped_column(String(254), default="", server_default="")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    archived: Mapped[bool] = mapped_column(default=False, server_default="false")
    crm_sync_state: Mapped[str] = mapped_column(String(20), default="idle", server_default="idle")
    crm_sync_token: Mapped[UUID | None]
    crm_sync_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone"),
        UniqueConstraint("tenant_id", "id"),
    )


class Campaign(Owned, Base):
    __tablename__ = "campaigns"
    name: Mapped[str] = mapped_column(String(120))
    route: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    max_channels: Mapped[int] = mapped_column(default=1)
    window_start: Mapped[int] = mapped_column(default=9)
    window_end: Mapped[int] = mapped_column(default=18)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    archived: Mapped[bool] = mapped_column(default=False, server_default="false")
    template_id: Mapped[UUID | None]
    mode: Mapped[str] = mapped_column(String(20), default="message", server_default="message")
    connect_on_qualify: Mapped[bool] = mapped_column(default=True, server_default="true")
    weekdays: Mapped[list] = mapped_column(
        JSON, default=lambda: list(range(7)), server_default="[0,1,2,3,4,5,6]"
    )
    retry_max: Mapped[int] = mapped_column(default=0, server_default="0")
    retry_delay_minutes: Mapped[int] = mapped_column(default=30, server_default="30")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["templates.tenant_id", "templates.id"],
            name="fk_campaign_template",
        ),
        CheckConstraint("status IN ('draft','running','paused','completed')"),
        CheckConstraint("route IN ('sim','multicom_ami','megafon_vats')"),
        CheckConstraint("max_channels BETWEEN 1 AND 100"),
        CheckConstraint("window_start >= 0 AND window_start < window_end AND window_end <= 24"),
    )


class CampaignItem(Owned, Base):
    __tablename__ = "campaign_items"
    campaign_id: Mapped[UUID]
    contact_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(24), default="queued")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("campaign_id", "contact_id"),
        ForeignKeyConstraint(["tenant_id", "campaign_id"], ["campaigns.tenant_id", "campaigns.id"]),
        ForeignKeyConstraint(["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"]),
        Index("ix_items_queue", "tenant_id", "campaign_id", "status"),
    )


class Attempt(Owned, Base):
    __tablename__ = "call_attempts"
    item_id: Mapped[UUID]
    route: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="prepared")
    external_id: Mapped[str | None] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ordinal: Mapped[int] = mapped_column(default=1, server_default="1")
    caller_number: Mapped[str] = mapped_column(String(16), default="", server_default="")
    channel: Mapped[str] = mapped_column(String(160), default="", server_default="")
    phone: Mapped[str] = mapped_column(String(16), default="", server_default="")
    contact_name: Mapped[str] = mapped_column(String(120), default="", server_default="")
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(String(120), default="", server_default="")
    agent_result: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("item_id", "ordinal", name="uq_attempt_item_ordinal"),
        UniqueConstraint("tenant_id", "route", "external_id"),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["campaign_items.tenant_id", "campaign_items.id"]
        ),
        CheckConstraint(
            "status IN ('prepared','dispatching','accepted','answered','completed','failed','unknown')"
        ),
        Index("ix_attempts_active", "tenant_id", "status", "created_at"),
    )


class Outbox(Owned, Base):
    __tablename__ = "outbox"
    kind: Mapped[str] = mapped_column(String(24))
    dedupe_key: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    last_error: Mapped[str] = mapped_column(String(80), default="")
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key"),
        Index("ix_outbox_due", "kind", "status", "available_at"),
    )


class Inbox(Owned, Base):
    __tablename__ = "inbox"
    route: Mapped[str] = mapped_column(String(24))
    event_key: Mapped[str] = mapped_column(String(160))
    external_id: Mapped[str] = mapped_column(String(120))
    event: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("tenant_id", "route", "event_key"),
        Index("ix_inbox_pending", "status", "received_at"),
    )


class Audit(Owned, Base):
    __tablename__ = "audit_log"
    actor_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)


class LegacyMap(Base):
    __tablename__ = "legacy_map"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), primary_key=True)
    legacy_id: Mapped[int] = mapped_column(primary_key=True)
    new_id: Mapped[UUID]


class ContactList(Owned, Base):
    __tablename__ = "contact_lists"
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "name"))


class ListMember(Owned, Base):
    __tablename__ = "list_members"
    list_id: Mapped[UUID]
    contact_id: Mapped[UUID]
    __table_args__ = (
        UniqueConstraint("list_id", "contact_id"),
        ForeignKeyConstraint(
            ["tenant_id", "list_id"], ["contact_lists.tenant_id", "contact_lists.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"]),
    )


class Template(Owned, Base):
    __tablename__ = "templates"
    name: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text, default="")
    scenario: Mapped[dict] = mapped_column(JSON, default=dict)
    archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)


class CallerNumber(Owned, Base):
    __tablename__ = "caller_numbers"
    phone: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    verified: Mapped[bool] = mapped_column(default=False)
    quarantined: Mapped[bool] = mapped_column(default=False)
    daily_limit: Mapped[int] = mapped_column(default=30)
    cooldown_seconds: Mapped[int] = mapped_column(default=60)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "phone"),)


class BlacklistEntry(Owned, Base):
    __tablename__ = "blacklist"
    phone: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("tenant_id", "phone"),)


class Operator(Owned, Base):
    __tablename__ = "operators"
    user_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(120), default="")
    extension: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(20), default="break")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("user_id"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        CheckConstraint("status IN ('free','busy','break','offline')"),
    )


class CallEvent(Owned, Base):
    __tablename__ = "call_events"
    attempt_id: Mapped[UUID]
    event: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "attempt_id"], ["call_attempts.tenant_id", "call_attempts.id"]
        ),
        Index("ix_call_events_timeline", "tenant_id", "attempt_id", "created_at"),
    )


class QueueEntry(Owned, Base):
    __tablename__ = "acd_queue"
    attempt_id: Mapped[UUID]
    operator_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    outcome: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("attempt_id"),
        ForeignKeyConstraint(
            ["tenant_id", "attempt_id"], ["call_attempts.tenant_id", "call_attempts.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "operator_id"], ["operators.tenant_id", "operators.id"]),
        CheckConstraint("status IN ('waiting','accepted','completed','cancelled')"),
    )


class WorkspaceSettings(Owned, Base):
    __tablename__ = "workspace_settings"
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(default=1)
    __table_args__ = (UniqueConstraint("tenant_id"),)


class RuntimeLog(Owned, Base):
    __tablename__ = "runtime_logs"
    level: Mapped[str] = mapped_column(String(10))
    component: Mapped[str] = mapped_column(String(32))
    code: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[UUID | None]
    request_id: Mapped[UUID | None]
    method: Mapped[str] = mapped_column(String(12), default="", server_default="")
    path: Mapped[str] = mapped_column(String(200), default="", server_default="")
    http_status: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_runtime_logs_tenant_created", "tenant_id", "created_at"),)


class Recording(Owned, Base):
    __tablename__ = "recordings"
    attempt_id: Mapped[UUID]
    storage_key: Mapped[str] = mapped_column(String(40), unique=True)
    content_type: Mapped[str] = mapped_column(String(40))
    size_bytes: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_recording_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "attempt_id"], ["call_attempts.tenant_id", "call_attempts.id"]
        ),
    )


class InboundCall(Owned, Base):
    __tablename__ = "inbound_calls"
    external_id: Mapped[str] = mapped_column(String(120))
    route: Mapped[str] = mapped_column(String(24))
    phone: Mapped[str] = mapped_column(String(32))
    destination: Mapped[str] = mapped_column(String(32), default="")
    contact_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(24), default="ringing")
    duration_seconds: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("tenant_id", "route", "external_id"),
        ForeignKeyConstraint(["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"]),
        Index("ix_inbound_tenant_created", "tenant_id", "created_at"),
    )


class HistoricalCall(Owned, Base):
    __tablename__ = "historical_calls"
    contact_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32))
    campaign_name: Mapped[str] = mapped_column(String(120), default="")
    route: Mapped[str] = mapped_column(String(32), default="legacy")
    direction: Mapped[str] = mapped_column(String(10), default="out")
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(500), default="")
    duration_seconds: Mapped[int] = mapped_column(default=0)
    legacy_id: Mapped[int]
    source_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "legacy_id"),
        Index("ix_history_tenant_created", "tenant_id", "created_at"),
    )


class DirectoryEntry(Owned, Base):
    __tablename__ = "provider_directory"
    provider: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(24))
    external_id: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "kind", "external_id"),)


class RecordingShare(Owned, Base):
    __tablename__ = "recording_shares"
    recording_id: Mapped[UUID]
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "recording_id"], ["recordings.tenant_id", "recordings.id"]
        ),
    )
