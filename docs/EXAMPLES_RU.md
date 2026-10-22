# Примеры из работающего проекта

Выдержки из модулей, исполнявшихся в тестах на PostgreSQL. Полные импорты и контекст — в исходных файлах. Commit выполняет API или worker unit of work; repository не коммитит скрыто.

## API: валидация

Источник: [src/ats/schemas.py](../src/ats/schemas.py).

```python
class CampaignCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    route: Literal["sim", "multicom_ami", "megafon_vats"] = "sim"
    contact_ids: list[UUID] = Field(min_length=1, max_length=1000)
    max_channels: int = Field(default=1, ge=1, le=100)
    window_start: int = Field(default=9, ge=0, le=23)
    window_end: int = Field(default=18, ge=1, le=24)

    @model_validator(mode="after")
    def valid_window(self):
        if self.window_start >= self.window_end:
            raise ValueError("Use a daytime window with start < end")
        if len(set(self.contact_ids)) != len(self.contact_ids):
            raise ValueError("Duplicate contacts")
        return self
```


## API: endpoint и ошибки

Источник: [src/ats/api.py](../src/ats/api.py).

```python
@app.post("/api/v1/campaigns", status_code=201)
def add_campaign(
    data: schemas.CampaignCreate,
    response: Response,
    idempotency_key: str = Header(min_length=8, max_length=128),
    p=Depends(admin),
    db=Depends(session),
):
    row, created = create_campaign(db, p, data, idempotency_key)
    db.commit()
    response.status_code = 201 if created else 200
    return row

@app.exception_handler(Problem)
async def problem_handler(request, exc):
    return JSONResponse(
        {"error": exc.code, "request_id": request.state.request_id}, status_code=exc.status
    )

@app.exception_handler(SQLAlchemyError)
async def database_handler(request, exc):
    log.error(
        "database_unavailable request_id=%s type=%s", request.state.request_id, type(exc).__name__
    )
    return JSONResponse(
        {"error": "storage_unavailable", "request_id": request.state.request_id}, status_code=503
    )
```


## Service: атомарные campaign + items + audit

Источник: [src/ats/services.py](../src/ats/services.py).

```python
def create_campaign(db, principal, data, key):
    """Caller owns the transaction. Campaign + items + audit commit together."""
    tenant = db.scalar(select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update())
    repo = TenantRepository(db, principal.tenant_id)
    fingerprint = hashlib.sha256(
        json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    existing = db.scalar(repo.query(Campaign).where(Campaign.idempotency_key == key))
    if existing:
        if existing.request_hash != fingerprint:
            raise Problem(409, "idempotency_key_reused")
        return existing, False
    if data.max_channels > tenant.max_channels:
        raise Problem(422, "tenant_channel_limit")
    contacts = db.scalars(repo.query(Contact).where(Contact.id.in_(data.contact_ids))).all()
    if len(contacts) != len(data.contact_ids):
        raise Problem(404, "contacts_not_found")
    if any(not c.consent or c.blocked or not c.consent_source for c in contacts):
        raise Problem(422, "contacts_not_callable")
    campaign = repo.add(
        Campaign,
        id=uuid4(),
        idempotency_key=key,
        request_hash=fingerprint,
        **data.model_dump(exclude={"contact_ids"}),
    )
    db.flush()
    for contact in contacts:
        repo.add(CampaignItem, campaign_id=campaign.id, contact_id=contact.id)
    repo.add(Audit, actor_id=principal.user_id, action="campaign.created", entity_id=campaign.id)
    db.flush()
    return campaign, True
```


## Worker: retry/backoff и неизвестный исход

Источник: [src/ats/workers.py](../src/ats/workers.py).

```python
def backoff(attempts, retry_after=0):
    return max(retry_after, min(3600, 2 ** min(attempts, 11)) + random.uniform(0, 1))

    def crm_job(self):
        with self.factory.begin() as db:
            job = claim(db, "crm")
        if not job:
            return False
        job_id, token, tenant_id, payload, attempts = job
        try:
            if not settings().crm_enabled and not self.injected_crm:
                raise Rejected("crm_disabled")
            if not self.injected_crm and tenant_id != settings().amo_tenant_id:
                raise Rejected("crm_tenant_not_bound")
            with self.factory() as db:
                repo = TenantRepository(db, tenant_id)
                attempt = repo.get(Attempt, UUID(payload["attempt_id"]))
                item = repo.get(CampaignItem, attempt.item_id)
                contact = repo.get(Contact, item.contact_id)
                crm_id, status, attempt_id = contact.crm_contact_id, attempt.status, attempt.id
            if not crm_id:
                raise Rejected("crm_contact_mapping_missing")
            if self.crm is None:
                self.crm = AmoCRM(settings())
            self.crm.send(attempt_id, crm_id, status)
        except Retryable as e:
            with self.factory.begin() as db:
                complete_job(
                    db,
                    job_id,
                    token,
                    "pending" if attempts < 8 else "dead",
                    e.code,
                    backoff(attempts, e.retry_after),
                )
        except Rejected as e:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "dead", e.code)
        except DeliveryUnknown:
            with self.factory.begin() as db:
                complete_job(db, job_id, token, "unknown", "crm_write_outcome_unknown")
        else:
            with self.factory.begin() as db:
                complete_job(db, job_id, token)
        return True
```


HTTP: 401 — сессия; 403 — роль/CSRF/Origin; 404 — недоступный tenant-scoped объект; 409 — конфликт; 422 — валидация/согласие/лимит; 503 — DB. В ответ не попадают пароли, SQL и raw payload.

Lease fencing и dedupe защищают локальную очередь. Exactly-once внешнего API не обещается: неоднозначный POST требует reconciliation. Повторы применяются только к определённо повторяемым ошибкам.
