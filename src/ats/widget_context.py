"""Live CRM card ownership and safe manager information for widget requests."""

from ats.adapters import Rejected, Retryable
from ats.domain import Problem
from ats.models import now
from ats.schemas import phone_from_import
from ats.webrtc import browser_operator_available, device_for, devices_for, pending_calls
from ats.widget_auth import mapped_operator


def read_responsible(crm, entity_type, entity_id):
    if entity_type not in {"leads", "contacts", "companies"} or not entity_id:
        raise Problem(422, "entity_id_required")
    entity = crm.read(f"/api/v4/{entity_type}/{int(entity_id)}", {"with": "contacts"})
    if int(entity.get("id", 0)) != int(entity_id) or entity.get("is_deleted") or entity.get("deleted_at"):
        raise Problem(409, "crm_card_unavailable")
    responsible = int(entity.get("responsible_user_id") or 0)
    if responsible <= 0:
        raise Problem(409, "responsible_user_missing")
    return entity, responsible


def read_call_card(crm, entity_type, entity_id, contact_id, phone):
    entity, responsible = read_responsible(crm, entity_type, entity_id or (
        contact_id if entity_type == "contacts" else None
    ))
    if entity_type == "contacts":
        if int(entity["id"]) != contact_id:
            raise Problem(403, "contact_not_in_card")
        raw = entity
    else:
        linked = entity.get("_embedded", {}).get("contacts", [])
        if contact_id not in {int(row["id"]) for row in linked}:
            # Embedded lists can be incomplete. Derive every links URL locally;
            # a CRM response must never redirect credentialed reads to another host.
            found = False
            for page in range(1, 21):
                response = crm.read(f"/api/v4/{entity_type}/{int(entity_id)}/links", {"page": page, "limit": 250})
                rows = response.get("_embedded", {}).get("links", [])
                if any(row.get("to_entity_type") == "contacts" and int(row.get("to_entity_id") or 0) == contact_id for row in rows):
                    found = True
                    break
                if not response.get("_links", {}).get("next") and len(rows) < 250:
                    break
            if not found:
                raise Problem(403, "contact_not_in_card")
        raw = crm.read(f"/api/v4/contacts/{contact_id}")
    if int(raw.get("id", 0)) != contact_id or raw.get("is_deleted") or raw.get("deleted_at"):
        raise Problem(409, "crm_contact_not_synced")
    phones = set()
    for field in raw.get("custom_fields_values") or []:
        if str(field.get("field_code") or "").upper() != "PHONE":
            continue
        for value in field.get("values") or []:
            try:
                phones.add(phone_from_import(str(value.get("value", ""))))
            except ValueError:
                continue
    if phone not in phones:
        raise Problem(409, "contact_phone_mismatch")
    return entity, responsible, crm.normalize_contact(raw)


def browser_online(device):
    return bool(device and device.agent_session_id and device.agent_registered
                and device.agent_expires_at and device.agent_expires_at > now())


def available_browser_device(db, operator, session_id=None):
    occupied = {call.browser_session_id for call in pending_calls(db, operator)}
    candidates = [device for device in devices_for(db, operator.tenant_id, operator.id)
                  if browser_online(device) and device.agent_session_id not in occupied
                  and (session_id is None or device.agent_session_id == session_id)]
    return max(candidates, key=lambda device: device.agent_expires_at, default=None)


def manager_context(db, config, tenant_id, responsible_id, crm, session_id=None):
    operator = None
    reason = ""
    try:
        operator, _ = mapped_operator(db, tenant_id, responsible_id)
    except Problem as exc:
        if exc.code != "operator_mapping_missing":
            raise
        reason = "responsible_mapping_missing"
    name = operator.name if operator else ""
    try:
        user = crm.read(f"/api/v4/users/{responsible_id}")
        name = str(user.get("name") or name)[:120]
    except (Rejected, Retryable):
        pass
    device = device_for(db, tenant_id, operator.id) if operator else None
    phone = operator.mobile_phone if operator else ""
    status = operator.status if operator else "unconfigured"
    online = bool(config.webrtc_enabled and operator and any(
        browser_online(row) for row in devices_for(db, tenant_id, operator.id)))
    browser_can_call = bool(online and browser_operator_available(db, operator)
                            and available_browser_device(db, operator, session_id))
    return operator, {
        "amo_user_id": responsible_id, "operator_id": str(operator.id) if operator else None,
        "operator_name": name or f"Сотрудник amoCRM #{responsible_id}", "status": status,
        "forward_number_masked": phone[:2] + "***" + phone[-4:] if phone else "",
        "forward_available": bool(phone), "browser_available": bool(config.webrtc_enabled and device),
        "browser_online": online, "browser_can_call": browser_can_call, "reason": reason,
    }


def verify_responsible(db, call, config, crm_class):
    """Recheck the live card before dispatch, without holding allocation locks."""
    context = call.crm_context or {}
    crm = crm_class(config)
    try:
        _, responsible = read_responsible(crm, context.get("entity_type", "contacts"),
                                          context.get("entity_id") or context.get("contact_id"))
        operator, _ = mapped_operator(db, call.tenant_id, responsible)
        if (responsible != (context.get("responsible_user_id") or context.get("amo_user_id"))
            or operator.id != call.operator_id):
            raise Problem(409, "responsible_changed")
        if call.browser_session_id:
            device = device_for(db, call.tenant_id, call.operator_id, call.browser_session_id)
            if not browser_online(device) or device.agent_session_id != call.browser_session_id:
                raise Problem(409, "responsible_browser_offline")
    finally:
        crm.close()
