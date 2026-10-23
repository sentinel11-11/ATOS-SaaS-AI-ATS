"""Exercise actual HTTP and separate worker processes, only in an ats_smoke database."""

import json
import os
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median, quantiles
from uuid import uuid4

import httpx
from sqlalchemy.engine import make_url

from ats.cli import bootstrap
from ats.db import session_factory


def main():
    if not (make_url(os.environ["ATS_DATABASE_URL"]).database or "").startswith("ats_smoke"):
        raise SystemExit("Use a disposable database with ats_smoke prefix")
    if os.environ.get("ATS_LIVE_CALLS_ENABLED", "false").lower() != "false":
        raise SystemExit("Live calls must be disabled for smoke testing")
    tenant_id, password = uuid4(), secrets.token_urlsafe(24)
    bootstrap(session_factory(), tenant_id, "Smoke test", "admin", password)
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    processes, files = [], []
    try:
        commands = [
            [
                ".venv/bin/uvicorn",
                "ats.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8017",
                "--no-access-log",
            ]
        ]
        commands += [
            [".venv/bin/python", "-m", "ats.workers", role]
            for role in ("scheduler", "telephony", "inbox", "crm")
        ]
        for i, command in enumerate(commands):
            stream = (artifacts / f"smoke-process-{i}.log").open("w")
            files.append(stream)
            processes.append(subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT))
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
                raise RuntimeError("Separate workers did not complete simulated call")
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
                    "http://127.0.0.1:8017/api/v1/campaigns",
                    cookies={"ats_session": cookie},
                    timeout=10,
                )
                assert result.status_code == 200
                assert result.json()[0]["id"] == campaign.json()["id"]
                return (time.perf_counter() - start) * 1000

            with ThreadPoolExecutor(max_workers=8) as pool:
                latency = list(pool.map(request, range(100)))
            result = {
                "requests": 100,
                "concurrency": 8,
                "errors": 0,
                "median_ms": round(median(latency), 2),
                "p95_ms": round(quantiles(latency, n=100)[94], 2),
                "call": "simulated_completed",
                "separate_processes": 5,
                "live_telephony_tested": False,
            }
            (artifacts / "http-smoke.json").write_text(json.dumps(result, indent=2) + "\n")
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
