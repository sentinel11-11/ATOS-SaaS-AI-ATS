"""One PostgreSQL LISTEN connection per API process, bounded fanout to browsers.

NOTIFY contains only a tenant UUID. It is an invalidation signal, not a delivery
log: clients reload their projection after connecting/reconnecting. No worker,
session, or transaction is held for the lifetime of a streaming HTTP request.
"""

import asyncio
import json
import logging
import threading
from collections import defaultdict
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.engine import make_url
from starlette.concurrency import run_in_threadpool

from ats.config import settings
from ats.db import session_factory
from ats.domain import Problem
from ats.security import principal

log = logging.getLogger("ats.events")
router = APIRouter(prefix="/api/v2", tags=["workspace events"])


class EventHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = defaultdict(set)
        self.stop = threading.Event()
        self.thread = None

    def publish(self, tenant=None):
        with self.lock:
            subscribers = [entry for key, group in self.clients.items() if tenant is None or key == tenant for entry in group]
        for loop, queue in subscribers:
            def put(queue=queue):
                if queue.empty():
                    queue.put_nowait(True)
            if not loop.is_closed():
                loop.call_soon_threadsafe(put)

    def listen(self):
        url = make_url(settings().database_url)
        args = url.translate_connect_args(username="user", database="dbname")
        args.update(dict(url.query))
        while not self.stop.is_set():
            try:
                with psycopg.connect(**args, autocommit=True, connect_timeout=5) as connection:
                    connection.execute("LISTEN ats_ui")
                    self.publish()  # Reconcile updates missed during reconnect.
                    while not self.stop.is_set():
                        for notification in connection.notifies(timeout=1, stop_after=1):
                            try:
                                tenant = UUID(notification.payload)
                            except ValueError:
                                continue
                            self.publish(tenant)
            except psycopg.Error:
                log.warning("ui_notification_connection_unavailable")
                self.stop.wait(2)

    @asynccontextmanager
    async def subscribe(self, tenant):
        entry = (asyncio.get_running_loop(), asyncio.Queue(maxsize=1))
        with self.lock:
            self.clients[tenant].add(entry)
            if self.thread is None or not self.thread.is_alive():
                self.stop.clear()
                self.thread = threading.Thread(target=self.listen, name="ats-ui-notifications", daemon=True)
                self.thread.start()
        try:
            yield entry[1]
        finally:
            with self.lock:
                self.clients[tenant].discard(entry)
                if not self.clients[tenant]:
                    self.clients.pop(tenant, None)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)


hub = EventHub()


def authenticate_stream(request):
    with session_factory()() as db:
        return principal(request, db)


@router.get("/events")
async def events(request: Request):
    p = await run_in_threadpool(authenticate_stream, request)
    if not settings().events_enabled:
        raise Problem(503, "events_disabled")

    async def stream():
        async with hub.subscribe(p.tenant_id) as queue:
            yield "retry: 3000\nevent: campaign\ndata: " + json.dumps({"refresh": True}) + "\n\n"
            checked = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                    yield 'event: campaign\ndata: {"refresh":true}\n\n'
                except TimeoutError:
                    yield ": heartbeat\n\n"
                if asyncio.get_running_loop().time() - checked > 30:
                    try:
                        current = await run_in_threadpool(authenticate_stream, request)
                    except Problem:
                        return
                    if current != p:
                        return
                    checked = asyncio.get_running_loop().time()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
