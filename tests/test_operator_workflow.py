from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_mobile_callback import callback as callback_fixture
from test_widget_protocol import browser_agent as browser_agent_fixture
from test_widget_protocol import create, payload
from test_widget_protocol import widget as widget_fixture

from ats.config import settings
from ats.models import (
    Attempt,
    CallerNumber,
    Campaign,
    CampaignItem,
    Contact,
    Operator,
    OperatorDevice,
    QueueEntry,
    Tenant,
    User,
    WidgetCall,
    now,
)
from ats.services import finish_attempt
from ats.workers import Worker

callback, widget, browser_agent = callback_fixture, widget_fixture, browser_agent_fixture


def start(widget, factory, **changes):
    ident = create(widget, payload(**changes))["call_id"]
    assert widget["client"].post(f"/api/v1/amo/widget/v2/calls/{ident}/start", headers=widget["headers"]).status_code == 202
    worker = Worker(factory)
    worker.schedule()
    return ident, worker


def test_dashboard_excludes_archived_disabled_and_non_operator_accounts(logged, widget, factory):
    with factory.begin() as db:
        for role, active, archived in [("operator", False, True), ("operator", False, False), ("viewer", True, False)]:
            user = User(tenant_id=widget["tenant_id"], login=uuid4().hex, role=role,
                        password_hash="fixture", active=active, archived=archived)
            db.add(user)
            db.flush()
            db.add(Operator(tenant_id=user.tenant_id, user_id=user.id, status="free"))
    operators = logged.get("/api/v2/dashboard").json()["operators"]
    assert [row["id"] for row in operators] == [str(widget["operator_id"])]


def test_delete_operator_frees_login_and_extension_but_keeps_call_attribution(logged, widget, factory, browser_agent):
    ident, worker = start(widget, factory, mode='browser', protocol_version=3, session_id=browser_agent)
    with factory.begin() as db:
        operator = db.get(Operator, widget["operator_id"])
        user = db.get(User, operator.user_id)
        user_id, login, extension = user.id, user.login, operator.extension
        token = db.get(WidgetCall, UUID(ident)).operator_token
        finish_attempt(db, db.scalar(select(Attempt)), "failed", "fixture_finished")
    response = logged.post("/api/v2/users/delete", json={"id": str(user_id)})
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.get(User, user_id) is None and db.get(Operator, widget["operator_id"]) is None
        assert not db.scalar(select(OperatorDevice))
        assert db.scalar(select(QueueEntry)).operator_id is None
        assert db.get(WidgetCall, UUID(ident)).operator_id is None
    journal = logged.post("/api/v2/calls", json={}).json()
    assert journal["total"] == 1 and journal["calls"][0]["operator_name"] == "Test manager"
    disconnected = widget['client'].post('/api/v1/amo/widget/v2/agent/disconnect',
        json={'call_id': ident, 'session_id': str(browser_agent), 'token': token},
        headers={'Origin': 'https://omkr.amocrm.ru'})
    assert disconnected.status_code == 202 and disconnected.json()['state'] == 'ended'
    created = logged.post("/api/v2/users/save", json={"login": login, "name": "Replacement",
        "ext": extension, "role": "operator", "active": True, "password": "NewFixturePassword123"})
    assert created.status_code == 200 and created.json()["id"] != str(user_id)
    assert len(logged.get("/api/v2/dashboard").json()["operators"]) == 1


def test_delete_contact_keeps_journal_and_pending_crm_delivery_and_allows_recreation(logged, widget, factory):
    _, worker = start(widget, factory)
    with factory.begin() as db:
        finish_attempt(db, db.scalar(select(Attempt)), "failed", "fixture_finished")
    ident = str(widget["contact_id"])
    response = logged.post("/api/v2/contacts/delete", json={"id": ident})
    assert response.status_code == 200, response.text
    assert logged.get("/api/v1/workspace/contacts/" + ident).status_code == 404
    journal = logged.post("/api/v2/calls", json={}).json()
    assert journal["total"] == 1 and journal["calls"][0]["contact_phone"] == "+79990000101"
    with factory() as db:
        attempt = db.scalar(select(Attempt))
        campaign_id = db.get(CampaignItem, attempt.item_id).campaign_id
        attempt_id = attempt.id
    detail = logged.get(f'/api/v2/campaigns/{campaign_id}')
    assert detail.status_code == 200 and detail.json()['calls'][0]['contact_id'] is None
    assert detail.json()['calls'][0]['contact_phone'] == '+79990000101'
    assert logged.get(f'/api/v1/workspace/calls/{attempt_id}').status_code == 200
    delivered = []

    class CRM:
        def send(self, *args):
            delivered.append(args)

    worker.crm, worker.injected_crm = CRM(), True
    assert worker.crm_job()
    assert delivered[0][1] == 123
    created = logged.post("/api/v2/contacts/save", json={"phone": "+79990000101", "name": "New contact", "consent": True})
    assert created.status_code == 200 and created.json()["id"] != ident
    assert logged.post("/api/v2/calls", json={}).json()["total"] == 1


@pytest.mark.parametrize("target", ["contact", "operator"])
@pytest.mark.parametrize("status", ["prepared", "unknown"])
def test_delete_refuses_active_or_unreconciled_call(logged, widget, factory, target, status):
    start(widget, factory)
    with factory.begin() as db:
        db.scalar(select(Attempt)).status = status
        operator = db.get(Operator, widget["operator_id"])
        user_id = operator.user_id
    if target == "contact":
        response = logged.post("/api/v2/contacts/delete", json={"id": str(widget["contact_id"])})
    else:
        response = logged.post("/api/v2/users/delete", json={"id": str(user_id)})
    assert response.status_code == 409, response.text
    with factory() as db:
        assert db.get(Contact, widget["contact_id"]) and db.get(User, user_id)
        assert db.scalar(select(Attempt)).status == status


def test_deleting_a_queued_contact_cancels_request_without_fake_call(logged, widget, factory):
    ident = create(widget)["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{ident}/start", headers=widget["headers"])
    assert logged.post("/api/v2/contacts/delete", json={"id": str(widget["contact_id"])}).status_code == 200
    with factory() as db:
        call = db.get(WidgetCall, UUID(ident))
        assert call.cancel_requested and db.get(Campaign, call.campaign_id).status == "completed"
        assert not db.scalar(select(CampaignItem)) and not db.scalar(select(Attempt))
    assert logged.post("/api/v2/calls", json={}).json()["total"] == 0


@pytest.mark.parametrize("mode", ["forward", "browser"])
def test_widget_dispatches_outside_working_hours_and_beyond_former_global_limit(factory, widget, browser_agent, mode):
    _, worker = start(widget, factory)
    with factory.begin() as db:
        finish_attempt(db, db.scalar(select(Attempt)), "failed", "fixture_finished")
        db.get(Tenant, widget["tenant_id"]).daily_limit = 1
    ident = create(widget, payload(mode=mode, protocol_version=3, session_id=browser_agent))["call_id"]
    widget["client"].post(f"/api/v1/amo/widget/v2/calls/{ident}/start", headers=widget["headers"])
    with factory.begin() as db:
        call = db.get(WidgetCall, UUID(ident))
        campaign = db.get(Campaign, call.campaign_id)
        campaign.window_start, campaign.window_end, campaign.weekdays = 0, 1, []
        campaign.schedule = {"operator_id": str(call.operator_id), "connection_method": "browser" if mode == "browser" else "mobile"}
    worker.schedule()
    for _ in range(3):
        worker.telephony()
    assert len(widget["provider"].originates) == 1
    with factory() as db:
        assert len(db.scalars(select(Attempt)).all()) == 2


def test_caller_number_cools_after_its_own_limit_then_starts_new_cycle(factory, widget):
    from ats.domain import Problem
    from ats.workspace_service import select_caller

    with factory.begin() as db:
        first = db.scalar(select(CallerNumber))
        first.daily_limit, first.cooldown_seconds = 2, 0
        db.add(CallerNumber(tenant_id=widget["tenant_id"], phone="9990000102", verified=True,
            enabled=True, provider="multicom_ami", daily_limit=2, cooldown_seconds=0))
    chosen = []
    for ordinal in range(1, 5):
        with factory.begin() as db:
            tenant = db.get(Tenant, widget["tenant_id"])
            phone = select_caller(db, tenant, "multicom_ami", config=settings())
            chosen.append(phone)
            item = db.scalar(select(CampaignItem))
            db.add(Attempt(tenant_id=tenant.id, item_id=item.id, route="multicom_ami", status="failed",
                caller_number=phone, ordinal=ordinal, channel_units=1))
    assert chosen[0] != chosen[1] and chosen[2] != chosen[3]
    with factory.begin() as db:
        with pytest.raises(Problem, match="caller_pool_exhausted"):
            select_caller(db, db.get(Tenant, widget["tenant_id"]), "multicom_ami", config=settings())
        first = db.scalar(select(CallerNumber).where(CallerNumber.phone == chosen[0]))
        assert first.quota_cooldown_until > now() + timedelta(minutes=59)
        first.quota_cooldown_until = now() - timedelta(seconds=1)
    with factory.begin() as db:
        phone = select_caller(db, db.get(Tenant, widget["tenant_id"]), "multicom_ami", config=settings())
        assert phone == chosen[0]
        first = db.scalar(select(CallerNumber).where(CallerNumber.phone == phone))
        assert first.quota_cooldown_until is None and first.quota_started_at > now() - timedelta(seconds=5)


def test_per_number_quota_and_cooldown_are_editable_and_enforced(logged, factory, widget, monkeypatch):
    from ats.workspace_service import select_caller

    with factory() as db:
        number_id = db.scalar(select(CallerNumber.id))
    response = logged.post('/api/v2/numbers/save', json={'id': str(number_id), 'daily_limit': 1,
        'quota_cooldown_seconds': 7200, 'cooldown_seconds': 0})
    assert response.status_code == 200, response.text
    timestamp = now()
    monkeypatch.setattr('ats.workspace_service.now', lambda: timestamp)
    with factory.begin() as db:
        select_caller(db, db.get(Tenant, widget['tenant_id']), 'multicom_ami', config=settings())
        number = db.get(CallerNumber, number_id)
        assert number.quota_cooldown_until == timestamp + timedelta(hours=2)
    projection = logged.get('/api/v2/numbers').json()['numbers'][0]
    assert projection['daily_limit'] == 1 and projection['quota_cooldown_seconds'] == 7200
    assert projection['cooling']


def test_migration_helper_deletes_old_archives_and_scopes_limit_changes(factory, widget):
    import runpy
    from pathlib import Path

    from ats.security import Principal

    update = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/apply-operator-workflow-update.py'))['apply_update']
    with factory.begin() as db:
        operator = db.get(Operator, widget['operator_id'])
        user = db.get(User, operator.user_id)
        user.archived, user.active = True, False
        db.get(Contact, widget['contact_id']).archived = True
        number = db.scalar(select(CallerNumber))
        number.daily_limit, number.quarantined, number.quota_cooldown_seconds = 30, True, 7200
        admin = db.scalar(select(User).where(User.tenant_id == widget['tenant_id'], User.role == 'admin'))
        actor = Principal(admin.tenant_id, admin.id, admin.role, admin.login)
        summary = update(db, widget['tenant_id'], actor, apply=False, caller_limit=150)
        assert summary['archived_users'] == 1 and db.get(User, user.id)
        update(db, widget['tenant_id'], actor, apply=True, caller_limit=150)
        assert db.get(User, user.id) is None and db.get(Contact, widget['contact_id']) is None
        assert number.daily_limit == 150 and number.quarantined and number.quota_cooldown_seconds == 7200
        number.daily_limit = 73
        update(db, widget['tenant_id'], actor, apply=True)
        assert number.daily_limit == 73


@pytest.mark.parametrize('changes,remaining', [
    ({'daily_limit': 3}, None),
    ({'quota_cooldown_seconds': 7200}, 6600),
    ({'quota_cooldown_seconds': 1800}, 1200),
    ({'quota_cooldown_seconds': 0}, -600),
])
def test_editing_an_active_cooldown_preserves_usage_and_applies_new_settings(
    logged, factory, widget, monkeypatch, changes, remaining,
):
    timestamp = now()
    monkeypatch.setattr('ats.caller_quota.now', lambda: timestamp)
    with factory.begin() as db:
        number = db.scalar(select(CallerNumber))
        number_id = number.id
        number.daily_limit, number.cooldown_seconds = 2, 0
        number.quota_started_at = timestamp - timedelta(hours=1)
        number.quota_cooldown_until = timestamp + timedelta(seconds=3000)
        item = db.scalar(select(CampaignItem))
        db.add(Attempt(tenant_id=widget['tenant_id'], item_id=item.id, ordinal=1,
            caller_number=number.phone, channel_units=2, route='multicom_ami', status='failed',
            created_at=timestamp - timedelta(minutes=10)))
    response = logged.post('/api/v2/numbers/save', json={'id': str(number_id), **changes})
    assert response.status_code == 200, response.text
    with factory() as db:
        number = db.get(CallerNumber, number_id)
        assert number.quota_started_at == timestamp - timedelta(hours=1)
        expected = timestamp + timedelta(seconds=remaining) if remaining is not None else None
        assert number.quota_cooldown_until == expected
    projection = logged.get('/api/v2/numbers').json()['numbers'][0]
    assert projection['cycle_count'] == (0 if remaining is not None and remaining <= 0 else 2)
    assert projection['cooling'] == (remaining is not None and remaining > 0)


def test_lowering_number_limit_starts_configured_cooldown_without_removing_history(logged, factory, widget):
    with factory.begin() as db:
        number = db.scalar(select(CallerNumber))
        number_id = number.id
        item = db.scalar(select(CampaignItem))
        db.add(Attempt(tenant_id=widget['tenant_id'], item_id=item.id, ordinal=1,
            caller_number=number.phone, channel_units=2, route='multicom_ami', status='failed'))
    response = logged.post('/api/v2/numbers/save', json={'id': str(number_id), 'daily_limit': 1,
        'quota_cooldown_seconds': 7200})
    assert response.status_code == 200, response.text
    projection = logged.get('/api/v2/numbers').json()['numbers'][0]
    assert projection['cooling'] and 7190 <= projection['cooldown_remaining_seconds'] <= 7200
    assert projection['cycle_count'] == 2 and projection['dialed_total'] == 1


def test_campaign_editor_keeps_widget_routing_and_24_hour_schedule(logged, factory, widget):
    ident = create(widget)['call_id']
    with factory() as db:
        campaign_id = db.get(WidgetCall, UUID(ident)).campaign_id
    before = logged.get('/api/v2/campaigns').json()['campaigns']
    campaign = next(row for row in before if row['id'] == str(campaign_id))
    assert campaign['schedule']['start'] == '00:00' and campaign['schedule']['end'] == '24:00'
    response = logged.post('/api/v2/campaigns/save', json={**campaign,
        'schedule': {'start': '08:00', 'end': '20:00', 'days': [0]}})
    assert response.status_code == 200, response.text
    with factory() as db:
        row = db.get(Campaign, campaign_id)
        assert (row.window_start, row.window_end, row.weekdays) == (0, 24, list(range(7)))
        assert row.schedule['widget_call_id'] == ident
        assert row.schedule['operator_id'] == str(widget['operator_id'])


def test_queued_campaign_can_be_removed_without_sql_or_fabricating_a_call(logged, widget, factory):
    ident = create(widget)['call_id']
    widget['client'].post(f'/api/v1/amo/widget/v2/calls/{ident}/start', headers=widget['headers'])
    with factory() as db:
        campaign_id = db.get(WidgetCall, UUID(ident)).campaign_id
    response = logged.post(f'/api/v2/campaigns/{campaign_id}/delete')
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.get(WidgetCall, UUID(ident)).cancel_requested
        assert not db.scalar(select(CampaignItem).where(CampaignItem.campaign_id == campaign_id, CampaignItem.status == 'queued'))
        assert not db.scalar(select(Attempt))
