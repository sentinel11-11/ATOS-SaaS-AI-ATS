"""Exercise the actual Alembic chain against populated 0001 schema, not just create_all."""

import os
import subprocess
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def test_upgrade_preserves_beta_contacts_and_calls(factory):
    base = make_url(os.environ["ATS_TEST_DATABASE_URL"])
    name = "ats_test_upgrade_" + uuid.uuid4().hex[:12]
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        db.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(base.set(database=name))
    env = {
        **os.environ,
        "ATS_DATABASE_URL": base.set(database=name).render_as_string(hide_password=False),
    }
    try:
        subprocess.run(
            [".venv/bin/alembic", "upgrade", "0001"], env=env, check=True, capture_output=True
        )
        tid, cid, camp, item, attempt = [uuid.uuid4() for _ in range(5)]
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO tenants VALUES (:id,'Upgrade test',3,30,'Europe/Moscow')"),
                {"id": tid},
            )
            db.execute(
                text(
                    "INSERT INTO contacts(id,tenant_id,name,phone,consent,consent_source,blocked,created_at) VALUES(:id,:tid,'Original','+79990000101',true,'written',false,now())"
                ),
                {"id": cid, "tid": tid},
            )
            db.execute(
                text(
                    "INSERT INTO campaigns(id,tenant_id,name,route,status,max_channels,window_start,window_end,idempotency_key,request_hash,created_at) VALUES(:id,:tid,'Original campaign','sim','paused',1,0,24,'upgrade-key','hash',now())"
                ),
                {"id": camp, "tid": tid},
            )
            db.execute(
                text(
                    "INSERT INTO campaign_items(id,tenant_id,campaign_id,contact_id,status) VALUES(:id,:tid,:campaign,:contact,'completed')"
                ),
                {"id": item, "tid": tid, "campaign": camp, "contact": cid},
            )
            db.execute(
                text(
                    "INSERT INTO call_attempts(id,tenant_id,item_id,route,status,reason,created_at,updated_at) VALUES(:id,:tid,:item,'sim','completed','',now(),now())"
                ),
                {"id": attempt, "tid": tid, "item": item},
            )
        subprocess.run(
            [".venv/bin/alembic", "upgrade", "head"], env=env, check=True, capture_output=True
        )
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
            assert (
                db.scalar(text("SELECT name FROM contacts WHERE id=:id"), {"id": cid}) == "Original"
            )
            assert (
                db.scalar(text("SELECT phone FROM call_attempts WHERE id=:id"), {"id": attempt})
                == "+79990000101"
            )
            assert db.scalar(text("SELECT count(*) FROM campaigns")) == 1
        subprocess.run([".venv/bin/alembic", "check"], env=env, check=True, capture_output=True)
    finally:
        engine.dispose()
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
