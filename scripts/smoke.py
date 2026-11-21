"""Exercise actual HTTP and separate worker processes, only in an ats_smoke database."""

import json
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median, quantiles
from uuid import uuid4

import httpx
from ami_fixture import AMIFixture
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from ats.cli import bootstrap
from ats.db import session_factory
from ats.models import Attempt, Inbox, Outbox


def main():
    if not (make_url(os.environ["ATS_DATABASE_URL"]).database or "").startswith("ats_smoke"):
        raise SystemExit("Use a disposable database with ats_smoke prefix")
    if os.environ.get("ATS_LIVE_CALLS_ENABLED", "false").lower() != "false":
        raise SystemExit("Live calls must be disabled for smoke testing")
    with AMIFixture(secrets.token_urlsafe(24)) as ami:
        run(ami)


def run(ami):
    tenant_id, password = uuid4(), secrets.token_urlsafe(24)
    bootstrap(session_factory(), tenant_id, "Smoke test", "admin", password)
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    result_path = artifacts / "http-smoke.json"
    result_path.unlink(missing_ok=True)
    # Enable the real route only in these children, bound to the local fixture.
    # The parent caller cannot redirect the smoke to a remote AMI or a real DID.
    child_env = {
        **os.environ,
        "ATS_DEPLOYMENT": "preprod",
        "ATS_PUBLIC_ORIGIN": "http://127.0.0.1:8017",
        "ATS_SECURE_COOKIES": "false",
        "ATS_LIVE_CALLS_ENABLED": "true",
        "ATS_TELEPHONY_TENANT_ID": str(tenant_id),
        "ATS_TEST_DESTINATIONS": "+79990000101",
        "ATS_AMI_HOST": "127.0.0.1",
        "ATS_AMI_PORT": str(ami.port),
        "ATS_AMI_TLS": "false",
        "ATS_AMI_USER": "smoke-fixture",
        "ATS_AMI_SECRET": ami.secret,
        "ATS_AMI_CALLER_ID": "9990000101",
        "ATS_AMI_CLI_ENDPOINT_PREFIX": "mcm-cli-",
        "ATS_AMI_OPERATOR_NUMBER_MAP": "{}",
        "ATS_INBOUND_ENABLED": "false",
        "ATS_MEDIA_ENABLED": "false",
        "ATS_CRM_ENABLED": "false",
        "ATS_WORKER_POLL_SECONDS": "0.1",
    }
    processes, files = [], []
    try:
        commands = [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "ats.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8017",
                "--no-access-log",
            ]
        ]
        commands += [
            [sys.executable, "-m", "ats.workers", role]
            for role in ("scheduler", "telephony", "inbox", "crm")
        ]
        for i, command in enumerate(commands):
            stream = (artifacts / f"smoke-process-{i}.log").open("w")
            files.append(stream)
            processes.append(
                subprocess.Popen(command, env=child_env, stdout=stream, stderr=subprocess.STDOUT)
            )
        with httpx.Client(
            base_url="http://127.0.0.1:8017",
            headers={"Origin": "http://127.0.0.1:8017"},
            timeout=10,
        ) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("API did not become ready")
            response = client.post(
                "/api/v1/auth/login",
                json={"tenant_id": str(tenant_id), "login": "admin", "password": password},
            )
            response.raise_for_status()
            client.headers["X-CSRF-Token"] = client.cookies["ats_csrf"]
            contact = client.post(
                "/api/v1/contacts",
                json={
                    "name": "Smoke fixture",
                    "phone": "+79990000101",
                    "consent": True,
                    "consent_source": "Synthetic smoke fixture",
                },
            )
            contact.raise_for_status()
            campaign = client.post(
                "/api/v1/campaigns",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "name": "Smoke campaign",
                    "route": "multicom_ami",
                    "mode": "message",
                    "retry_max": 0,
                    "contact_ids": [contact.json()["id"]],
                    "window_start": 0,
                    "window_end": 24,
                },
            )
            campaign.raise_for_status()
            client.post(
                f"/api/v1/campaigns/{campaign.json()['id']}/state", json={"status": "running"}
            ).raise_for_status()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                attempts = client.get("/api/v1/attempts").json()
                if attempts and attempts[0]["status"] == "completed":
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Separate workers did not complete the AMI fixture call")
            attempt_id = attempts[0]["id"]
            assert len(ami.originates) == 1
            frame = ami.originates[0]
            assert frame["ActionID"] == [attempt_id]
            assert frame["CallerID"] == ["9990000101"]
            assert "__ATS_TRUNK=mcm-cli-9990000101" in frame["Variable"]
            with session_factory()() as db:
                assert db.scalar(
                    select(func.count()).select_from(Attempt).where(Attempt.tenant_id == tenant_id)
                ) == 1
                assert db.scalar(
                    select(func.count()).select_from(Inbox).where(Inbox.tenant_id == tenant_id)
                ) == 2
                assert db.scalar(
                    select(func.count()).select_from(Outbox).where(
                        Outbox.tenant_id == tenant_id, Outbox.kind == "crm"
                    )
                ) == 1
            assert client.get("/").status_code == 200
            assert "ATS" in client.get("/").text
            if os.environ.get("ATS_BROWSER_SMOKE") == "true":
                # The canonical ZIP interface is the supported browser contract.
                # The old smoke runner targeted the discarded replacement UI.
                subprocess.run(["node", "scripts/legacy-parity-smoke.mjs"], check=True)
            workspace = client.get("/api/v1/workspace/calls")
            workspace.raise_for_status()
            assert workspace.json()["total"] == 1
            assert client.get("/api/v1/workspace/reports").json()["total"] == 1
            cookie = client.cookies["ats_session"]

            def request(_):
                start = time.perf_counter()
                result = httpx.get(
                    "http://127.0.0.1:8017/api/v1/workspace/calls",
                    cookies={"ats_session": cookie},
                    timeout=10,
                )
                assert result.status_code == 200
                assert result.json()["total"] == 1
                assert result.json()["items"][0]["id"] == attempt_id
                return (time.perf_counter() - start) * 1000

            with ThreadPoolExecutor(max_workers=8) as pool:
                latency = list(pool.map(request, range(100)))
            assert all(process.poll() is None for process in processes)
            result = {
                "requests": 100,
                "concurrency": 8,
                "errors": 0,
                "median_ms": round(median(latency), 2),
                "p95_ms": round(quantiles(latency, n=100)[94], 2),
                "tenant_id": str(tenant_id),
                "attempt_id": attempt_id,
                "call": "ami_fixture_completed",
                "originate_count": len(ami.originates),
                "duplicate_events_deduplicated": True,
                "separate_processes": len(processes),
                "live_telephony_tested": False,
            }
            result_path.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in files:
            stream.close()


if __name__ == "__main__":
    main()
