import os
import subprocess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from ats.config import settings
from ats.models import Base, Tenant, User
from ats.security import hasher

PASSWORD = "FixturePasswordOnly.123"


@pytest.fixture(scope="session")
def factory():
    url = os.environ.get("ATS_TEST_DATABASE_URL", "")
    if not url:
        pytest.fail("Set ATS_TEST_DATABASE_URL to a disposable PostgreSQL ats_test database")
    if not (make_url(url).database or "").startswith("ats_test"):
        pytest.fail("Refusing to delete data in a database without ats_test prefix")
    os.environ.update(
        ATS_DATABASE_URL=url,
        ATS_SECURE_COOKIES="false",
        ATS_PUBLIC_ORIGIN="http://testserver",
        ATS_LIVE_CALLS_ENABLED="false",
    )
    settings.cache_clear()
    subprocess.run([".venv/bin/alembic", "upgrade", "head"], check=True)
    engine = create_engine(url)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def clean(factory):
    settings.cache_clear()
    with factory.begin() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
    yield
    settings.cache_clear()


@pytest.fixture(scope="session")
def password_hash():
    return hasher.hash(PASSWORD)


@pytest.fixture
def tenants(factory, password_hash):
    ids = [uuid4(), uuid4()]
    with factory.begin() as db:
        for tid in ids:
            db.add(Tenant(id=tid, name=f"Tenant {tid}", max_channels=2, daily_limit=30))
        db.flush()
        for tid in ids:
            db.add(User(tenant_id=tid, login="admin", password_hash=password_hash, role="admin"))
    return ids


@pytest.fixture
def client(factory):
    from ats.api import app
    from ats.db import session

    def override():
        with factory() as db:
            yield db

    app.dependency_overrides[session] = override
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def logged(client, tenants):
    response = client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenants[0]), "login": "admin", "password": PASSWORD},
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["ats_csrf"]
    return client


@pytest.fixture
def prepared(logged):
    contact = logged.post(
        "/api/v1/contacts",
        json={
            "name": "Тест",
            "phone": "+79990000101",
            "consent": True,
            "consent_source": "Written beta test consent",
            "crm_contact_id": 123,
        },
    )
    assert contact.status_code == 201
    data = {
        "name": "Beta",
        "contact_ids": [contact.json()["id"]],
        "window_start": 0,
        "window_end": 24,
    }
    response = logged.post(
        "/api/v1/campaigns", json=data, headers={"Idempotency-Key": "fixture-key-01"}
    )
    assert response.status_code == 201
    campaign_id = response.json()["id"]
    assert (
        logged.post(
            f"/api/v1/campaigns/{campaign_id}/state", json={"status": "running"}
        ).status_code
        == 200
    )
    return campaign_id, contact.json()["id"], data
