"""The polling worker must leave the web server responsive and reuse logins."""
import asyncio
import time
from contextlib import nullcontext, suppress

from app import database, main


def test_sync_io_does_not_block_dashboard_requests(monkeypatch):
    class SlowSyncer:
        def __init__(self, db):
            self.db = db
        def run_sync_cycle(self):
            time.sleep(0.2)

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(main, "SyncOrchestrator", SlowSyncer)

    async def probe():
        task = asyncio.create_task(main.sync_loop())
        started = time.monotonic()
        await asyncio.sleep(0.01)
        elapsed = time.monotonic() - started
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return elapsed

    assert asyncio.run(probe()) < 0.1


def test_polling_reuses_clients_instead_of_logging_in_each_minute(monkeypatch):
    instances = []
    cycles = []
    class CountingSyncer:
        def __init__(self, db):
            self.db = db
            instances.append(self)
        def run_sync_cycle(self):
            cycles.append(1)

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(main, "SyncOrchestrator", CountingSyncer)
    monkeypatch.setattr(main.settings, "SYNC_INTERVAL_SECONDS", 0.01)

    async def probe():
        task = asyncio.create_task(main.sync_loop())
        deadline = time.monotonic() + 2
        while len(cycles) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(probe())
    assert len(cycles) >= 2
    assert len(instances) == 1
