"""Reference data and compact attribution context for amoCRM contacts."""


def reference_data(crm, contacts):
    pipeline_rows = crm.pipelines()
    pipelines = {int(row["id"]): str(row.get("name") or "") for row in pipeline_rows if row.get("id")}
    pipeline_ids = {int(pid) for contact in contacts for pid in contact.get("pipeline_ids", [])}
    statuses = {}
    for pipeline_id in pipeline_ids:
        for row in crm.pipeline_statuses(pipeline_id):
            if row.get("id"):
                statuses[int(row["id"])] = str(row.get("name") or "")
    users = {
        int(row["id"]): str(row.get("name") or row.get("email") or "")
        for row in crm.users()
        if row.get("id")
    }
    return pipelines, statuses, users


def contact_context(raw, pipelines, statuses, users):
    pipeline_ids = [int(value) for value in raw.get("pipeline_ids", [])]
    stage_ids = [int(value) for value in raw.get("status_ids", [])]
    responsible_id = raw.get("responsible_user_id")
    try:
        responsible_id = int(responsible_id) if responsible_id else None
    except (TypeError, ValueError):
        responsible_id = None
    pipeline_name = pipelines.get(pipeline_ids[0], "") if pipeline_ids else ""
    stage_name = statuses.get(stage_ids[0], "") if stage_ids else ""
    if not pipeline_name and raw.get("pipeline_names"):
        pipeline_name = raw["pipeline_names"][0]
    if not stage_name and raw.get("status_names"):
        stage_name = raw["status_names"][0]
    return {
        "pipeline_id": pipeline_ids[0] if pipeline_ids else None,
        "pipeline_name": pipeline_name,
        "stage_id": stage_ids[0] if stage_ids else None,
        "stage_name": stage_name,
        "responsible_id": responsible_id,
        "responsible_name": users.get(responsible_id, "") if responsible_id else "",
        "pipeline_ids": pipeline_ids,
        "stage_ids": stage_ids,
        "pipeline_stages": raw.get("pipeline_stages", []),
        "tags": list(dict.fromkeys(raw.get("tag_names", []))),
    }
