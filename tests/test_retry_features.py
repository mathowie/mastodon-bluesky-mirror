"""Retry behavior and durable recovery are tested at real SQLite/SDK boundaries."""
import asyncio
import json
from types import SimpleNamespace
from contextlib import nullcontext, suppress

import pytest
from fastapi import FastAPI
import httpx
from atproto.exceptions import BadRequestError, InvokeTimeoutError
from atproto_client.models.common import XrpcError
from atproto_client.request import Response

from app import database, main
from app.database import SyncMap
from app.web.routes import router
from test_sync_regressions import db, status, bluesky_client, syncer, DID, CID

ID = "100000000000000001"


def api(db):
    app = FastAPI()
    app.include_router(router, prefix="/dashboard")
    def session():
        yield db
    app.dependency_overrides[database.get_db] = session
    app.state.sync_wakeup = SimpleNamespace(set=lambda: None)
    return app


def rejection(message="Invalid reply reference"):
    return BadRequestError(Response(False, 400, XrpcError("InvalidRequest", message), {}))


def test_retry_enqueues_without_deleting_mapping(db):
    # Run the route coroutine in this thread, keeping real SQLite thread ownership.
    from app.web.routes import retry_post
    row = SyncMap(mastodon_id=ID, status="failed", bluesky_uri=f"at://{DID}/app.bsky.feed.post/root", bluesky_cid=CID)
    db.add(row); db.commit()
    import inspect
    args = {"mastodon_id": ID, "db": db}
    wakeups = []
    if "request" in inspect.signature(retry_post).parameters:
        args["request"] = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sync_wakeup=SimpleNamespace(set=lambda: wakeups.append(1)))))
    result = asyncio.run(retry_post(**args))
    remaining = db.get(SyncMap, ID)
    assert remaining is not None
    assert remaining.status == "queued"
    assert remaining.bluesky_uri.endswith("/root")
    assert remaining.bluesky_cid == CID
    assert wakeups == [1]


def test_retry_rejects_success_instead_of_erasing_duplicate_protection(db):
    from app.web.routes import retry_post
    from fastapi import HTTPException
    import inspect
    db.add(SyncMap(mastodon_id=ID, status="success", bluesky_uri="at://existing")); db.commit()
    args = {"mastodon_id": ID, "db": db}
    if "request" in inspect.signature(retry_post).parameters:
        args["request"] = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(retry_post(**args))
    assert exc.value.status_code == 409
    assert db.get(SyncMap, ID).status == "success"


def test_partial_thread_retry_resumes_instead_of_republishing_root(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    create = bsky.client.app.bsky.feed.post.create
    failing = [True]
    def unreliable(repo, record, **kwargs):
        if records and failing[0]:
            raise rejection()
        return create(repo, record, **kwargs)
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "create", unreliable)
    post = status(content="<p>" + "long post " * 45 + "</p>")
    runner = syncer(db, bsky, [post])
    runner.run_sync_cycle()
    assert len(records) == 1
    failed = db.get(SyncMap, ID)
    assert failed.status == "failed"
    assert "InvalidRequest" in failed.error_message
    assert "Invalid reply reference" in failed.error_message
    root_uri = records.uris[0]
    failed.status = "queued"; db.commit()
    failing[0] = False
    runner.run_sync_cycle()
    assert len(records) == 2
    assert records[1].reply.root.uri == root_uri
    assert records[1].reply.parent.uri == root_uri
    assert db.get(SyncMap, ID).status == "success"
    assert db.get(SyncMap, ID).error_message is None
    assert db.query(database.SyncPart).count() == 2


def test_timeout_after_remote_commit_is_reconciled_with_no_duplicate(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    create = bsky.client.app.bsky.feed.post.create
    once = [True]
    def committed_but_lost_response(repo, record, **kwargs):
        response = create(repo, record, **kwargs)
        if once[0]:
            once[0] = False
            raise InvokeTimeoutError()
        return response
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "create", committed_but_lost_response)
    syncer(db, bsky, [status()]).run_sync_cycle()
    assert len(records) == 1
    assert db.get(SyncMap, ID).status == "success"


def test_process_restart_reuses_the_planned_key_after_remote_commit(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    create = bsky.client.app.bsky.feed.post.create
    def crash_after_commit(repo, record, **kwargs):
        create(repo, record, **kwargs)
        raise SystemExit("Simulated process crash")
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "create", crash_after_commit)
    post = status(content="<p>" + "long post " * 45 + "</p>")
    with pytest.raises(SystemExit):
        syncer(db, bsky, [post]).run_sync_cycle()
    db.rollback()
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "create", create)
    runner = syncer(db, bsky, [post])
    runner.run_sync_cycle()
    assert len(records) == 2
    assert records[1].reply.root.uri == records.uris[0]
    assert db.get(SyncMap, ID).status == "success"


def test_queued_retry_fetches_status_outside_latest_twenty(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    db.add(SyncMap(mastodon_id=ID, status="queued")); db.commit()
    runner = syncer(db, bsky, [])
    runner.masto.get_status = lambda ident: status(id=ident)
    runner.run_sync_cycle()
    assert len(records) == 1
    assert db.get(SyncMap, ID).status == "success"


def test_retry_rechecks_source_privacy(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    db.add(SyncMap(mastodon_id=ID, status="queued")); db.commit()
    runner = syncer(db, bsky, [])
    runner.masto.get_status = lambda ident: status(id=ident, visibility="private")
    runner.run_sync_cycle()
    assert records == []
    assert db.get(SyncMap, ID).status == "skipped"


def test_server_error_details_are_useful_but_redacted(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    def invalid(*args, **kwargs):
        raise rejection("Invalid index; token=test-mastodon-secret password=test-app-secret")
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "create", invalid)
    syncer(db, bsky, [status()]).run_sync_cycle()
    detail = db.get(SyncMap, ID).error_message
    assert "InvalidRequest" in detail
    assert "Invalid index" in detail
    assert "test-mastodon-secret" not in detail
    assert "test-app-secret" not in detail
    assert "[REDACTED]" in detail


def test_thread_progress_and_error_are_visible_in_history(db, monkeypatch):
    from app.web.routes import get_history, get_stats
    bsky, records = bluesky_client(monkeypatch)
    create = bsky.client.app.bsky.feed.post.create
    def second_fails(repo, record, **kwargs):
        if records: raise rejection()
        return create(repo, record, **kwargs)
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "create", second_fails)
    syncer(db, bsky, [status(content="<p>" + "long post " * 45 + "</p>")]).run_sync_cycle()
    history = asyncio.run(get_history(db=db))
    assert history[0]["parts_published"] == 1
    assert history[0]["parts_total"] == 2
    assert "InvalidRequest" in history[0]["error"]
    assert history[0]["synced_at"].endswith("Z")
    stats = asyncio.run(get_stats(db=db))
    assert stats["total_synced"] == 0
    assert stats["failed"] == 1


def test_retry_wakes_the_worker_without_waiting_for_poll_interval(monkeypatch):
    cycles = []
    class Runner:
        def __init__(self, db): self.db = db
        def run_sync_cycle(self): cycles.append(1)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(main, "SyncOrchestrator", Runner)
    monkeypatch.setattr(main.settings, "SYNC_INTERVAL_SECONDS", 60)
    async def probe():
        event = asyncio.Event()
        task = asyncio.create_task(main.sync_loop(event))
        try:
            for _ in range(100):
                if cycles: break
                await asyncio.sleep(0.005)
            assert len(cycles) == 1
            event.set()
            for _ in range(100):
                if len(cycles) >= 2: break
                await asyncio.sleep(0.005)
            assert len(cycles) == 2
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError): await task
    asyncio.run(probe())


def test_failed_record_lookup_never_causes_a_blind_create(db, monkeypatch):
    from atproto.exceptions import NetworkError
    bsky, records = bluesky_client(monkeypatch)
    lookups = []
    def unavailable(*args, **kwargs):
        lookups.append(1)
        raise NetworkError()
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "get", unavailable)
    import time
    monkeypatch.setattr(time, "sleep", lambda delay: None)
    syncer(db, bsky, [status()]).run_sync_cycle()
    assert records == []
    assert 1 <= len(lookups) <= 3
    assert db.get(SyncMap, ID).status == "failed"


def test_a_deleted_published_prefix_is_not_silently_recreated(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    create = bsky.client.app.bsky.feed.post.create
    failing = [True]
    def fail_second(repo, record, **kwargs):
        if records and failing[0]: raise rejection()
        return create(repo, record, **kwargs)
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, "create", fail_second)
    post = status(content="<p>" + "long post " * 45 + "</p>")
    runner = syncer(db, bsky, [post])
    runner.run_sync_cycle()
    assert len(records) == 1
    records.by_key.clear()  # Simulate the user deleting the already-published root.
    db.get(SyncMap, ID).status = "queued"; db.commit()
    failing[0] = False
    runner.run_sync_cycle()
    assert len(records) == 1
    assert db.get(SyncMap, ID).status == "failed"
    assert "missing" in db.get(SyncMap, ID).error_message.lower()


def test_verified_recovery_journal_is_imported_idempotently(db, tmp_path):
    from app import database
    importer = getattr(database, "import_recovery_journals", None)
    assert callable(importer), "A recovered live thread must repair its dashboard mapping"
    db.add(SyncMap(mastodon_id=ID, status="failed", error_message="BadRequestError")); db.commit()
    root_uri=f"at://{DID}/app.bsky.feed.post/2222222222222"
    child_uri=f"at://{DID}/app.bsky.feed.post/2222222222223"
    root_record={"$type":"app.bsky.feed.post","text":"First","createdAt":"2026-10-29T12:00:00Z"}
    child_record={"$type":"app.bsky.feed.post","text":"Second","createdAt":"2026-10-29T12:00:01Z","reply":{"root":{"uri":root_uri,"cid":CID},"parent":{"uri":root_uri,"cid":CID}}}
    journal={"mastodon_id":ID,"repo":DID,"source_images":[],"parts":[
        {"record_key":"2222222222222","text":"First","state":"published","uri":root_uri,"cid":CID,"record":root_record},
        {"record_key":"2222222222223","text":"Second","state":"published","uri":child_uri,"cid":CID,"record":child_record},
    ]}
    (tmp_path/(ID+".json")).write_text(json.dumps(journal))
    baseline=db.get(database.SettingsStore,"sync_started_at").value
    assert importer(db,tmp_path) == 1
    assert db.get(SyncMap,ID).status == "success"
    assert db.get(SyncMap,ID).bluesky_uri == child_uri
    assert db.get(SyncMap,ID).error_message is None
    assert db.query(database.SyncPart).count() == 2
    assert importer(db,tmp_path) == 0
    assert db.get(database.SettingsStore,"sync_started_at").value == baseline


@pytest.mark.parametrize("initial,code,final", [
    ("failed",202,"queued"),("queued",202,"queued"),("pending",409,"pending"),
    ("success",409,"success"),("skipped",409,"skipped"),
])
def test_retry_http_status_and_persistence(db, initial, code, final):
    db.add(SyncMap(mastodon_id=ID,status=initial,bluesky_uri=f"at://{DID}/app.bsky.feed.post/root",bluesky_cid=CID));db.commit()
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api(db)),base_url="http://testserver") as client:
            return await client.post(f"/dashboard/retry/{ID}")
    response=asyncio.run(send())
    assert response.status_code == code
    assert db.get(SyncMap,ID).status == final
    assert db.get(SyncMap,ID).bluesky_cid == CID


def test_retry_unknown_status_returns_404_without_creating_a_job(db):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api(db)),base_url="http://testserver") as client:
            return await client.post(f"/dashboard/retry/{ID}")
    assert asyncio.run(send()).status_code == 404
    assert db.query(SyncMap).count() == 0


def test_retry_endpoint_remains_password_protected():
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="http://testserver") as client:
            return await client.post(f"/dashboard/retry/{ID}")
    response=asyncio.run(send())
    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower() == "basic"


def test_long_rate_limit_delay_is_not_retried_prematurely(db,monkeypatch):
    from atproto.exceptions import RateLimitExceededError
    bsky,records=bluesky_client(monkeypatch)
    reads=[]
    def limited(*args,**kwargs):
        reads.append(1)
        raise RateLimitExceededError(Response(False,429,XrpcError("RateLimitExceeded","Try later"),{"retry-after":"120"}))
    monkeypatch.setattr(bsky.client.app.bsky.feed.post,"get",limited)
    import time
    monkeypatch.setattr(time,"sleep",lambda seconds: None)
    syncer(db,bsky,[status()]).run_sync_cycle()
    assert records == []
    assert len(reads) == 1
    assert db.get(SyncMap,ID).status == "failed"
