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
    UniqueConstraint,
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
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "idempotency_key"),
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
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("item_id"),
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
