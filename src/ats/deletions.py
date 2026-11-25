"""Delete address-book and login records while keeping independent call history."""

from sqlalchemy import delete, func, select, update

from ats.domain import ACTIVE, Problem
from ats.inbound_callback import expire_routes, is_operator_reserved
from ats.models import (
    AmoSyncItem,
    Attempt,
    Campaign,
    CampaignItem,
    Contact,
    InboundCall,
    InboundCallback,
    ListMember,
    Operator,
    OperatorDevice,
    QueueEntry,
    Session,
    Tenant,
    User,
    WidgetCall,
)
from ats.repositories import TenantRepository
from ats.workspace_service import audit


def complete_empty_campaigns(db, tenant_id, identities):
    for campaign in db.scalars(select(Campaign).where(Campaign.tenant_id == tenant_id, Campaign.id.in_(identities))):
        pending = db.scalar(select(CampaignItem.id).where(
            CampaignItem.tenant_id == tenant_id, CampaignItem.campaign_id == campaign.id,
            CampaignItem.status.in_(["queued", "active"]),
        ).limit(1))
        if not pending:
            campaign.status = "completed"


def cancel_widget(db, call, reason):
    if db.get(Campaign, call.campaign_id).status == "completed":
        return
    call.cancel_requested = True
    call.crm_context = {**call.crm_context, "failure_reason": reason}
    for item in db.scalars(select(CampaignItem).where(
        CampaignItem.tenant_id == call.tenant_id, CampaignItem.campaign_id == call.campaign_id,
        CampaignItem.status == "queued",
    )):
        item.status = "cancelled"


def delete_contact(db, p, ident):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    repo = TenantRepository(db, p.tenant_id)
    contact = repo.get(Contact, ident, lock=True)
    items = db.scalars(repo.query(CampaignItem).where(CampaignItem.contact_id == ident).with_for_update()).all()
    item_ids = [item.id for item in items]
    active = db.scalar(repo.query(Attempt).where(Attempt.item_id.in_(item_ids), Attempt.status.in_(ACTIVE)).limit(1))
    inbound = db.scalars(repo.query(InboundCall).where(InboundCall.contact_id == ident)).all()
    if active or any(call.status in {"ringing", "answered", "accepted", "connected"} for call in inbound):
        raise Problem(409, "finish_contact_calls_first")
    campaign_ids = {item.campaign_id for item in items}
    for call in db.scalars(repo.query(WidgetCall).where(WidgetCall.campaign_id.in_(campaign_ids))):
        cancel_widget(db, call, "contact_deleted")
    for item in items:
        attempts = db.scalars(repo.query(Attempt).where(Attempt.item_id == item.id)).all()
        for attempt in attempts:
            attempt.phone = attempt.phone or contact.phone
            attempt.contact_name = attempt.contact_name or contact.name
            attempt.agent_result = {**attempt.agent_result, "deleted_contact_id": str(ident),
                                    "crm_contact_id": contact.crm_contact_id}
        if attempts:
            item.contact_id = None
            if item.status in {"queued", "active"}:
                item.status = "cancelled"
        else:
            db.delete(item)
    for call in inbound:
        call.contact_snapshot = {"name": contact.name, "crm_contact_id": contact.crm_contact_id,
                                 "deleted_contact_id": str(ident)}
        call.contact_id = None
    db.execute(delete(ListMember).where(ListMember.tenant_id == p.tenant_id, ListMember.contact_id == ident))
    for preview in db.scalars(repo.query(AmoSyncItem).where(AmoSyncItem.contact_id == ident)):
        preview.contact_id = None
    db.flush()
    complete_empty_campaigns(db, p.tenant_id, campaign_ids)
    audit(db, p, "contact.deleted", ident)
    db.delete(contact)
    db.flush()


def delete_user(db, p, ident):
    db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
    expire_routes(db, p.tenant_id)
    repo = TenantRepository(db, p.tenant_id)
    user = repo.get(User, ident, lock=True)
    if user.role == "admin" and user.active:
        count = db.scalar(select(func.count()).select_from(User).where(
            User.tenant_id == p.tenant_id, User.role == "admin", User.active, ~User.archived,
        ))
        if count <= 1:
            raise Problem(409, "last_admin_required")
    operator = db.scalar(repo.query(Operator).where(Operator.user_id == ident).with_for_update())
    if operator:
        busy = db.scalar(repo.query(Attempt).join(QueueEntry, QueueEntry.attempt_id == Attempt.id).where(
            QueueEntry.operator_id == operator.id, Attempt.status.in_(ACTIVE),
        ).limit(1))
        widget_busy = db.scalar(repo.query(Attempt).join(CampaignItem, CampaignItem.id == Attempt.item_id)
            .join(WidgetCall, WidgetCall.campaign_id == CampaignItem.campaign_id).where(
                WidgetCall.operator_id == operator.id, Attempt.status.in_(ACTIVE),
            ).limit(1))
        if busy or widget_busy or is_operator_reserved(db, operator):
            raise Problem(409, "finish_operator_calls_first")
        for entry in db.scalars(repo.query(QueueEntry).where(QueueEntry.operator_id == operator.id)):
            attempt = repo.get(Attempt, entry.attempt_id)
            attempt.route_snapshot = {**attempt.route_snapshot, "operator_name": operator.name,
                                      "operator_extension": operator.extension}
            entry.operator_id = None
        for call in db.scalars(repo.query(WidgetCall).where(WidgetCall.operator_id == operator.id)):
            call.crm_context = {**call.crm_context, "responsible_name": call.crm_context.get("responsible_name") or operator.name,
                                "deleted_operator_id": str(operator.id)}
            cancel_widget(db, call, "operator_deleted")
            call.operator_id = None
        affected = []
        for campaign in db.scalars(repo.query(Campaign).where(
            Campaign.schedule["operator_id"].as_string() == str(operator.id),
        )):
            affected.append(campaign.id)
            for item in db.scalars(repo.query(CampaignItem).where(
                CampaignItem.campaign_id == campaign.id, CampaignItem.status == "queued",
            )):
                item.status = "cancelled"
        db.execute(delete(OperatorDevice).where(OperatorDevice.tenant_id == p.tenant_id,
                                               OperatorDevice.operator_id == operator.id))
        db.execute(update(InboundCallback).where(
            InboundCallback.tenant_id == p.tenant_id, InboundCallback.operator_id == operator.id,
        ).values(operator_id=None, browser_session_id=None, browser_token=None))
        db.flush()
        complete_empty_campaigns(db, p.tenant_id, affected)
        db.delete(operator)
    for call in db.scalars(repo.query(WidgetCall).where(WidgetCall.initiator_user_id == ident)):
        call.initiator_user_id = None
    db.execute(delete(Session).where(Session.tenant_id == p.tenant_id, Session.user_id == ident))
    audit(db, p, "user.deleted", ident)
    db.flush()
    db.delete(user)
    db.flush()
