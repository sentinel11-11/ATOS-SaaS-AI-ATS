from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ats.config import settings
from ats.models import Attempt, CallerNumber, Campaign, CampaignItem, Contact, Tenant, now
from ats.services import schedule_one
from ats.workers import Worker
from ats.workspace_service import select_caller


def add_numbers(db, tenant_id, *, cooldown=60, weights=(1, 1), daily_limit=30):
    rows = [CallerNumber(
        id=UUID(int=index + 1), tenant_id=tenant_id, phone=f"999000010{index + 1}",
        provider="multicom_ami", verified=True, enabled=True, enabled_outgoing=True,
        cooldown_seconds=cooldown, weight=weight, daily_limit=daily_limit,
    ) for index, weight in enumerate(weights)]
    db.add_all(rows)
    db.flush()
    return rows


def add_contacts(db, tenant_id, campaign_id, count):
    for index in range(count):
        contact = Contact(
            tenant_id=tenant_id, name=f"Synthetic {index}", phone=f"+79990001{index:03d}",
            consent=True, consent_source="Synthetic rotation test",
        )
        db.add(contact)
        db.flush()
        db.add(CampaignItem(tenant_id=tenant_id, campaign_id=campaign_id, contact_id=contact.id))


def test_multicom_weights_control_share_of_eligible_numbers(factory, prepared, tenants):
    with factory.begin() as db:
        add_numbers(db, tenants[0], cooldown=0, weights=(3, 1))
        tenant = db.scalar(select(Tenant).where(Tenant.id == tenants[0]).with_for_update())
        item = db.scalar(select(CampaignItem))
        chosen = []
        for ordinal in range(1, 9):
            phone = select_caller(db, tenant, "multicom_ami", settings())
            chosen.append(phone)
            db.add(Attempt(
                tenant_id=tenant.id, item_id=item.id, route="multicom_ami", status="failed",
                ordinal=ordinal, caller_number=phone,
            ))
            db.flush()
        assert Counter(chosen) == {"9990000101": 6, "9990000102": 2}


def test_two_number_rotation_waits_for_cooldown_then_reuses_correct_alias(
    factory, prepared, tenants, provider_config, monkeypatch, logged
):
    timestamp = [now()]
    for module in ["ats.workspace_service", "ats.compat_resources"]:
        monkeypatch.setattr(module + ".now", lambda: timestamp[0])
    monkeypatch.setenv("ATS_AMI_CLI_ENDPOINT_PREFIX", "mcm-cli-")
    settings.cache_clear()
    provider_config.endpoints.add("mcm-cli-9990000102")
    with factory.begin() as db:
        add_numbers(db, tenants[0])
        db.get(Campaign, UUID(prepared[0])).max_channels = 1
        add_contacts(db, tenants[0], UUID(prepared[0]), 2)
    worker = Worker(factory)

    def dial_and_finish():
        with factory.begin() as db:
            assert schedule_one(db, tenants[0])
        assert worker.telephony()
        with factory() as db:
            attempt = db.scalar(select(Attempt).where(Attempt.status == "accepted"))
            attempt_id = str(attempt.id)
        worker.ami_event({
            "Event": "UserEvent", "UserEvent": "ATSCall", "AttemptID": attempt_id,
            "State": "completed",
        })
        worker.inbox()
        with factory() as db:
            assert db.get(Attempt, UUID(attempt_id)).status == "completed"

    dial_and_finish()
    dial_and_finish()
    rows = logged.get("/api/v2/numbers").json()["numbers"]
    assert all(row["cooling"] for row in rows)
    for elapsed in [0, 59]:
        timestamp[0] += timedelta(seconds=elapsed)
        with factory.begin() as db:
            assert not schedule_one(db, tenants[0])
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Attempt)) == 2
            assert db.scalar(select(func.count()).select_from(CampaignItem).where(
                CampaignItem.status == "queued"
            )) == 1
    timestamp[0] += timedelta(seconds=1)
    assert all(not row["cooling"] for row in logged.get("/api/v2/numbers").json()["numbers"])
    dial_and_finish()
    assert provider_config.endpoint_checks == [
        "mcm-cli-9990000101", "mcm-cli-9990000102", "mcm-cli-9990000101",
    ]
    assert [frame["Variable"][-2] for frame in provider_config.originates] == [
        "__ATS_TRUNK=mcm-cli-9990000101", "__ATS_TRUNK=mcm-cli-9990000102",
        "__ATS_TRUNK=mcm-cli-9990000101",
    ]


def test_rotation_concurrent_schedulers_respect_one_cooldown_reservation_per_number(
    factory, prepared, tenants
):
    with factory.begin() as db:
        add_numbers(db, tenants[0])
        db.get(Campaign, UUID(prepared[0])).max_channels = 2
        add_contacts(db, tenants[0], UUID(prepared[0]), 3)

    def allocate(_):
        with factory.begin() as db:
            return schedule_one(db, tenants[0])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(allocate, range(16)))
    # SKIP LOCKED callers may yield while another scheduler owns the tenant.
    # Normal subsequent iterations fill available capacity after the race.
    allocate(None)
    allocate(None)
    with factory() as db:
        rows = db.scalars(select(Attempt)).all()
        assert len(rows) == 2
        assert {row.caller_number for row in rows} == {"9990000101", "9990000102"}


def test_weight_does_not_override_number_eligibility_or_quota(factory, prepared, tenants):
    with factory.begin() as db:
        rows = add_numbers(db, tenants[0], cooldown=0, weights=(100, 100, 100, 100, 100, 1))
        rows[0].enabled = False
        rows[1].verified = False
        rows[2].quarantined = True
        rows[3].enabled_outgoing = False
        rows[4].daily_limit = 1
        item = db.scalar(select(CampaignItem))
        db.add(Attempt(
            tenant_id=tenants[0], item_id=item.id, route="multicom_ami", status="failed",
            caller_number=rows[4].phone,
        ))
        db.flush()
        tenant = db.scalar(select(Tenant).where(Tenant.id == tenants[0]).with_for_update())
        assert select_caller(db, tenant, "multicom_ami", settings()) == rows[5].phone


def test_rotation_uses_tenant_midnight_for_daily_quota(factory, prepared, tenants, monkeypatch):
    current = datetime(2026, 11, 21, 21, 5, tzinfo=UTC)
    monkeypatch.setattr("ats.workspace_service.now", lambda: current)
    with factory.begin() as db:
        rows = add_numbers(db, tenants[0], cooldown=0, daily_limit=1)
        tenant = db.scalar(select(Tenant).where(Tenant.id == tenants[0]).with_for_update())
        tenant.timezone = "Europe/Moscow"
        item = db.scalar(select(CampaignItem))
        for ordinal, row in enumerate(rows, 1):
            db.add(Attempt(
                tenant_id=tenant.id, item_id=item.id, route="multicom_ami", status="failed",
                ordinal=ordinal, caller_number=row.phone,
                created_at=current - timedelta(minutes=10 if ordinal == 1 else 1),
            ))
        db.flush()
        assert select_caller(db, tenant, "multicom_ami", settings()) == rows[0].phone


def test_rolled_back_allocation_does_not_consume_cooldown_or_quota(factory, prepared, tenants):
    with factory.begin() as db:
        add_numbers(db, tenants[0])
    with pytest.raises(RuntimeError):
        with factory.begin() as db:
            tenant = db.scalar(select(Tenant).where(Tenant.id == tenants[0]).with_for_update())
            selected = select_caller(db, tenant, "multicom_ami", settings())
            item = db.scalar(select(CampaignItem))
            db.add(Attempt(
                tenant_id=tenant.id, item_id=item.id, route="multicom_ami", caller_number=selected,
            ))
            db.flush()
            raise RuntimeError("Synthetic failure before commit")
    with factory.begin() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.id == tenants[0]).with_for_update())
        assert select_caller(db, tenant, "multicom_ami", settings()) == selected
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
