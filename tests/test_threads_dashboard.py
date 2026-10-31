"""Destination controls use real SQLite state and never contact a provider."""
import asyncio
from datetime import datetime, timedelta
import sys
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, SettingsStore, SyncMap, SyncPart, ThreadsSyncMap, ThreadsSyncPart, get_db
from app.web.routes import get_history, get_stats, router


ID = "100000000000000101"
ACCOUNT = "900000000000001"
WHEN = datetime(2026, 10, 30, 6)
LATER = datetime(2026, 10, 30, 7)


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "THREADS_ENABLED", True)
    monkeypatch.setattr(settings, "THREADS_ACCESS_TOKEN", "synthetic-threads-token")
    monkeypatch.setattr(settings, "THREADS_APP_SECRET", "synthetic-threads-secret")
    monkeypatch.setattr(settings, "THREADS_TOKEN_FILE", str(tmp_path / "unused-token.json"))

    def no_network(*args, **kwargs):
        pytest.fail("Dashboard reads and retries must not contact a provider")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        app = FastAPI()
        app.include_router(router, prefix="/dashboard")
        app.dependency_overrides[get_db] = lambda: db
        app.state.sync_wakeup = Event()

        async def request(method, path):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test",
            ) as client:
                return await client.request(method, "/dashboard" + path)

        yield SimpleNamespace(
            db=db, wakeup=app.state.sync_wakeup,
            request=lambda method, path: asyncio.run(request(method, path)),
        )
    engine.dispose()


def connection(monkeypatch, **metadata):
    """Substitute the separate client's read-only metadata boundary, never its HTTP."""
    monkeypatch.setitem(sys.modules, "app.clients.threads", SimpleNamespace(
        ThreadsClient=lambda: SimpleNamespace(connection_status=lambda: metadata),
    ))


def saved_job(db, status="failed"):
    row = ThreadsSyncMap(
        mastodon_id=ID, threads_id="900000000000002", permalink="https://www.threads.net/@example/post/synthetic",
        account_id=ACCOUNT, synced_at=WHEN, status=status, error_message="Try later",
        attempt_count=5, next_attempt_at=LATER,
    )
    parts = [
        ThreadsSyncPart(
            mastodon_id=ID, part_index=0, account_id=ACCOUNT, payload_json='{"text":"first"}',
            source_fingerprint="source-a", children_json='["900000000000003"]',
            container_id="900000000000004", threads_id="900000000000002",
            permalink=row.permalink, state="published",
        ),
        ThreadsSyncPart(
            mastodon_id=ID, part_index=1, account_id=ACCOUNT, payload_json='{"text":"second"}',
            source_fingerprint="source-a", children_json="[]",
            container_id="900000000000005", state="ready",
        ),
    ]
    db.add_all([row, *parts])
    db.commit()
    return row, parts


def checkpoints(parts):
    return [{column.name: getattr(part, column.name) for column in ThreadsSyncPart.__table__.columns}
            for part in parts]


def test_default_http_and_direct_calls_keep_bluesky_results(dashboard, monkeypatch):
    # A default request must not initialize optional Threads support.
    monkeypatch.setitem(sys.modules, "app.clients.threads", None)
    dashboard.db.add(SyncMap(mastodon_id=ID, status="success", synced_at=WHEN, bluesky_uri="at://synthetic/root"))
    saved_job(dashboard.db)
    stats = asyncio.run(get_stats(dashboard.db))
    history = asyncio.run(get_history(dashboard.db))
    assert stats["total_synced"] == 1
    assert stats["failed"] == 0
    assert "threads_connection" not in stats
    assert history[0]["status"] == "success"
    assert history[0]["bluesky_uri"] == "at://synthetic/root"
    assert dashboard.request("GET", "/stats").json() == stats
    assert dashboard.request("GET", "/history").json() == history


def test_threads_stats_count_only_threads_and_expose_safe_local_identity(dashboard, monkeypatch):
    connection(monkeypatch, configured=True, account_id=ACCOUNT, username="example", expires_at=1804000000,
               status="ready", error=None, access_token="synthetic-threads-token", app_secret="synthetic-threads-secret")
    dashboard.db.add_all([
        SyncMap(mastodon_id=ID, status="success"),
        *[ThreadsSyncMap(mastodon_id=str(index), status=status) for index, status in enumerate(
            ["success", "failed", "needs_review", "pending", "queued"], 1)],
    ])
    dashboard.db.commit()
    response = dashboard.request("GET", "/stats?destination=threads")
    assert response.status_code == 200
    stats = response.json()
    assert (stats["total_synced"], stats["failed"], stats["pending"], stats["queued"], stats["success_rate"]) == (1, 2, 1, 1, "33.3%")
    assert stats["threads_connection"] == {
        "enabled": True, "configured": True, "account_id": ACCOUNT, "username": "example",
        "expires_at": 1804000000, "status": "ready", "error": None,
    }
    assert "synthetic-threads" not in response.text


def test_threads_history_uses_own_parts_and_retains_utc_schedule(dashboard):
    row, parts = saved_job(dashboard.db)
    dashboard.db.add_all([
        SyncMap(mastodon_id=ID, status="success", synced_at=WHEN),
        SyncPart(mastodon_id=ID, part_index=0, record_key="synthetic-key", repo_did="did:plc:synthetic",
                 record_json="{}", bluesky_uri="at://synthetic/root"),
    ])
    dashboard.db.commit()
    response = dashboard.request("GET", "/history?destination=threads")
    assert response.status_code == 200
    entry, = response.json()
    assert entry["mastodon_id"] == ID
    assert entry["status"] == "failed"
    assert (entry["parts_published"], entry["parts_total"]) == (1, 2)
    assert entry["synced_at"] == "2026-10-30T06:00:00Z"
    assert entry["next_attempt_at"] == "2026-10-30T07:00:00Z"
    assert entry["threads_id"] == row.threads_id
    assert entry["permalink"] == row.permalink
    assert entry["bluesky_uri"] is None
    assert "payload_json" not in entry
    assert "children_json" not in entry


@pytest.mark.parametrize("destination", ["bluesky", "threads"])
def test_history_redacts_stored_credentials(dashboard, destination):
    model = ThreadsSyncMap if destination == "threads" else SyncMap
    dashboard.db.add(model(mastodon_id=ID, status="failed", error_message=(
        "Failed synthetic-threads-token synthetic-threads-secret access_token=renewed-private-token"
    )))
    dashboard.db.commit()
    response = dashboard.request("GET", f"/history?destination={destination}")
    assert "synthetic-threads-token" not in response.text
    assert "synthetic-threads-secret" not in response.text
    assert "renewed-private-token" not in response.text
    assert "REDACTED" in response.json()[0]["error"]


def test_connection_error_redacts_credentials_and_excludes_unrecognized_keys(dashboard, monkeypatch):
    connection(monkeypatch, configured=True, status="invalid", error="Failed synthetic-threads-secret token=private-value",
               access_token="private-value", private_file={"secret": "private-value"})
    response = dashboard.request("GET", "/stats?destination=threads")
    status = response.json()["threads_connection"]
    assert status["status"] == "invalid"
    assert "REDACTED" in status["error"]
    assert "synthetic-threads-secret" not in response.text
    assert "private-value" not in response.text
    assert "private_file" not in status
    assert "access_token" not in status


def test_background_credential_errors_are_visible_without_secrets(dashboard, monkeypatch):
    connection(monkeypatch, configured=True, account_id=ACCOUNT, username="example", status="ready", error=None)
    dashboard.db.add_all([
        SettingsStore(key="threads_credentials_error", value="HTTP 429 token=renewed-private-token"),
        SettingsStore(key="threads_credentials_retry_at", value="2026-10-31T23:00:00"),
    ])
    dashboard.db.commit()
    response = dashboard.request("GET", "/stats?destination=threads")
    metadata = response.json()["threads_connection"]
    assert metadata["status"] == "retry_wait"
    assert metadata["retry_at"] == "2026-10-31T23:00:00Z"
    assert "HTTP 429" in metadata["error"] and "REDACTED" in metadata["error"]
    assert "renewed-private-token" not in response.text


@pytest.mark.parametrize("missing", [True, False])
def test_optional_client_unavailability_is_safe_and_does_not_break_stats(dashboard, monkeypatch, missing):
    def broken_client():
        raise RuntimeError("private-constructor-token synthetic-threads-secret")

    monkeypatch.setitem(sys.modules, "app.clients.threads", None if missing else SimpleNamespace(ThreadsClient=broken_client))
    response = dashboard.request("GET", "/stats?destination=threads")
    assert response.status_code == 200
    assert response.json()["threads_connection"]["status"] == "unavailable"
    assert "private-constructor-token" not in response.text
    assert "synthetic-threads-secret" not in response.text


@pytest.mark.parametrize("status", ["failed", "queued"])
def test_threads_retry_resets_backoff_keeps_every_checkpoint_and_wakes_worker(dashboard, status):
    row, parts = saved_job(dashboard.db, status)
    saved_parts = checkpoints(parts)
    dashboard.db.add(SyncMap(mastodon_id=ID, status="failed", error_message="Bluesky stays failed"))
    dashboard.db.commit()
    response = dashboard.request("POST", f"/retry/{ID}?destination=threads")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    dashboard.db.refresh(row)
    assert (row.status, row.error_message, row.attempt_count, row.next_attempt_at) == ("queued", None, 0, None)
    assert (row.threads_id, row.permalink, row.account_id) == (
        "900000000000002", "https://www.threads.net/@example/post/synthetic", ACCOUNT,
    )
    assert checkpoints(parts) == saved_parts
    assert dashboard.db.get(SyncMap, ID).status == "failed"
    assert dashboard.wakeup.is_set()


@pytest.mark.parametrize("status", ["failed", "queued"])
def test_manual_retry_preserves_future_provider_not_before_time(dashboard, status):
    row, parts = saved_job(dashboard.db, status)
    not_before = datetime.utcnow() + timedelta(seconds=120)
    row.next_attempt_at = not_before
    dashboard.db.commit()
    saved_parts = checkpoints(parts)
    response = dashboard.request("POST", f"/retry/{ID}?destination=threads")
    assert response.status_code == 202
    dashboard.db.refresh(row)
    assert row.status == "queued"
    assert row.error_message is None
    assert row.attempt_count == 0
    assert row.next_attempt_at == not_before
    assert checkpoints(parts) == saved_parts
    assert dashboard.wakeup.is_set()


def test_disabled_threads_reject_retry_and_report_disabled_metadata(dashboard, monkeypatch):
    monkeypatch.setattr(settings, "THREADS_ENABLED", False)
    row, parts = saved_job(dashboard.db)
    response = dashboard.request("POST", f"/retry/{ID}?destination=threads")
    assert response.status_code == 409
    assert row.status == "failed"
    assert row.attempt_count == 5
    assert row.next_attempt_at == LATER
    assert not dashboard.wakeup.is_set()
    stats = dashboard.request("GET", "/stats?destination=threads").json()
    assert stats["threads_connection"]["enabled"] is False


@pytest.mark.parametrize("status", ["success", "needs_review", "pending", "skipped"])
def test_threads_retry_rejects_ineligible_states_without_mutation(dashboard, status):
    row, parts = saved_job(dashboard.db, status)
    response = dashboard.request("POST", f"/retry/{ID}?destination=threads")
    assert response.status_code == 409
    assert row.status == status
    assert row.attempt_count == 5
    assert not dashboard.wakeup.is_set()


@pytest.mark.parametrize("state", ["publishing", "published"])
def test_threads_retry_rejects_parts_with_ambiguous_publication(dashboard, state):
    row, parts = saved_job(dashboard.db)
    parts[1].state = state
    dashboard.db.commit()
    saved_parts = checkpoints(parts)
    response = dashboard.request("POST", f"/retry/{ID}?destination=threads")
    assert response.status_code == 409
    assert row.status == "failed"
    assert checkpoints(parts) == saved_parts
    assert not dashboard.wakeup.is_set()


@pytest.mark.parametrize("method,path", [("GET", "/stats"), ("GET", "/history"), ("POST", f"/retry/{ID}")])
def test_unknown_destination_cannot_fall_through_to_bluesky(dashboard, method, path):
    dashboard.db.add(SyncMap(mastodon_id=ID, status="failed"))
    dashboard.db.commit()
    response = dashboard.request(method, path + "?destination=unknown")
    assert response.status_code == 422
    assert dashboard.db.get(SyncMap, ID).status == "failed"
    assert not dashboard.wakeup.is_set()


@pytest.mark.parametrize("ident,expected", [("not-a-status", 422), ("2", 404)])
def test_threads_retry_keeps_id_validation_and_missing_entry_errors(dashboard, ident, expected):
    assert dashboard.request("POST", f"/retry/{ident}?destination=threads").status_code == expected


def test_default_bluesky_retry_leaves_threads_retry_budget_untouched(dashboard):
    row, parts = saved_job(dashboard.db)
    dashboard.db.add(SyncMap(mastodon_id=ID, status="failed", bluesky_uri="at://synthetic/root", bluesky_cid="synthetic-cid"))
    dashboard.db.commit()
    response = dashboard.request("POST", f"/retry/{ID}")
    assert response.status_code == 202
    assert response.json()["message"] == "Queued for retry; completed thread parts will be reused"
    assert (row.status, row.attempt_count, row.next_attempt_at) == ("failed", 5, LATER)
    bluesky = dashboard.db.get(SyncMap, ID)
    assert (bluesky.status, bluesky.bluesky_uri, bluesky.bluesky_cid) == ("queued", "at://synthetic/root", "synthetic-cid")
