"""Verify migration metadata and a dump/restore in disposable smoke databases."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from ats.config import settings
from ats.models import Attempt


def fingerprint(engine):
    result = {}
    with engine.connect() as connection:
        for table in sorted(inspect(engine).get_table_names()):
            quoted = engine.dialect.identifier_preparer.quote(table)
            rows = connection.execute(text(f"SELECT row_to_json(t)::text FROM {quoted} t ORDER BY 1"))
            digest = hashlib.sha256()
            count = 0
            for row in rows.scalars():
                digest.update(row.encode() + b"\n")
                count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="atos-dev-postgres")
    args = parser.parse_args()
    url = make_url(settings().database_url)
    if not (url.database or "").startswith("ats_smoke"):
        raise SystemExit("Use a disposable database with ats_smoke prefix")
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    result_path = artifacts / "database-check.json"
    result_path.unlink(missing_ok=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    subprocess.run([sys.executable, "-m", "alembic", "check"], check=True)
    restored_name = "ats_smoke_restore_" + uuid4().hex
    docker = ["docker", "exec", args.container]
    source = create_engine(url)
    restored = create_engine(url.set(database=restored_name))
    dump = artifacts / "smoke-backup.dump"
    try:
        expected = fingerprint(source)
        if not expected[Attempt.__tablename__]["rows"]:
            raise RuntimeError("Run the HTTP smoke first to provide call history for restore")
        with dump.open("wb") as output:
            subprocess.run(
                [*docker, "pg_dump", "-U", "postgres", "-d", url.database, "-Fc"],
                stdout=output, check=True,
            )
        subprocess.run([*docker, "createdb", "-U", "postgres", restored_name], check=True)
        try:
            restore_started = time.monotonic()
            with dump.open("rb") as input_stream:
                subprocess.run(
                    [
                        "docker", "exec", "-i", args.container, "pg_restore", "-U", "postgres",
                        "--exit-on-error", "--no-owner", "-d", restored_name,
                    ],
                    stdin=input_stream, check=True,
                )
            actual = fingerprint(restored)
            if actual != expected:
                raise RuntimeError("Restored table contents differ from the source database")
            restore_seconds = round(time.monotonic() - restore_started, 3)
            scripts = ScriptDirectory.from_config(Config("alembic.ini"))
            head = scripts.get_revision(scripts.get_current_head())
            restore_env = {
                **os.environ,
                "ATS_DATABASE_URL": url.set(database=restored_name).render_as_string(
                    hide_password=False
                ),
            }
            for command in (["downgrade", head.down_revision], ["upgrade", "head"], ["check"]):
                subprocess.run(
                    [sys.executable, "-m", "alembic", *command], env=restore_env, check=True
                )
            if fingerprint(restored) != expected:
                raise RuntimeError("Migration rollback/upgrade changed the restored data")
            result = {
                "migration_check": "passed",
                "rollback_upgrade": "passed",
                "head": head.revision,
                "rollback_target": head.down_revision,
                "backup_restore": "passed",
                "restore_seconds": restore_seconds,
                "tables": actual,
            }
            result_path.write_text(json.dumps(result, indent=2) + "\n")
            print(f"Migration check and backup/restore passed for {len(actual)} tables")
        finally:
            restored.dispose()
            subprocess.run([*docker, "dropdb", "-U", "postgres", restored_name], check=True)
    finally:
        source.dispose()
        restored.dispose()
        dump.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
