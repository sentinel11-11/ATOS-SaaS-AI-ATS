from functools import lru_cache

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from ats.config import settings


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def session_factory():
    return sessionmaker(engine(), expire_on_commit=False)


def session():
    with session_factory()() as db:
        yield db


@event.listens_for(Session, "after_flush")
def _queue_ui_notifications(db, _flush_context):
    """Send one tenant invalidation per transaction after a successful flush.

    The browser stream is an invalidation channel: it carries no business data,
    only the tenant UUID. PostgreSQL emits NOTIFY on commit, so a rolled-back
    transaction cannot make the UI refresh for data that never existed.
    """
    tenants = db.info.setdefault("ats_ui_tenants", set())
    for obj in (*db.new, *db.dirty, *db.deleted):
        tenant_id = getattr(obj, "tenant_id", None)
        if tenant_id is not None:
            tenants.add(str(tenant_id))
    if not tenants:
        return
    sent = db.info.setdefault("ats_ui_notified", set())
    for tenant_id in tenants - sent:
        db.connection().execute(text("SELECT pg_notify('ats_ui', :tenant)"), {"tenant": tenant_id})
        sent.add(tenant_id)


@event.listens_for(Session, "after_transaction_end")
def _clear_ui_notifications(db, transaction):
    # Nested savepoints share the outer transaction and must not clear the
    # pending set before the outer commit/rollback has completed.
    if transaction.parent is None:
        db.info.pop("ats_ui_tenants", None)
        db.info.pop("ats_ui_notified", None)
