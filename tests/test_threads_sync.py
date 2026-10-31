"""Durable Threads publication tested against a stateful substitute service."""
import json
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests
from mastodon import MastodonNetworkError, MastodonRatelimitError, MastodonServerError

from app.config import settings
from app.database import SettingsStore, SyncMap, ThreadsSyncMap, ThreadsSyncPart
from test_sync_regressions import db, status, bluesky_client, syncer

ID = "100000000000000001"
CHILD = "100000000000000002"


class APIError(Exception):
    def __init__(self, message="Provider rejected request", status=400, transient=False, delay=0):
        super().__init__(message)
        self.http_status = status
        self.transient = transient
        self.retry_after = delay


class NetworkError(Exception):
    transient = True
    http_status = None
    retry_after = 0


class ThreadsService:
    """Models separate unpublished containers and published, immutable IDs."""
    def __init__(self):
        self.account_id = "700"
        self.containers = {}
        self.posts = {}
        self.publish_calls = []
        self.default_status = "FINISHED"
        self.create_error = None
        self.publish_error = None
        self.crash_after_publish = False
        self.timeout_after_create = False
        self.credential_checks = 0

    def maintain_credentials(self):
        self.credential_checks += 1

    def create_container(self, payload):
        if self.create_error:
            raise self.create_error
        ident = str(1000 + len(self.containers))
        self.containers[ident] = {"payload": deepcopy(payload), "status": self.default_status}
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise NetworkError("Lost draft response")
        return ident

    def container_status(self, ident):
        return {"id": ident, "status": self.containers[ident]["status"]}

    def publish_container(self, ident):
        self.publish_calls.append(ident)
        if self.publish_error:
            raise self.publish_error
        container = self.containers[ident]
        assert container["status"] == "FINISHED"
        payload = deepcopy(container["payload"])
        if payload.get("reply_to_id"):
            assert payload["reply_to_id"] in self.posts
        published_id = str(2000 + len(self.posts))
        self.posts[published_id] = payload
        container["status"] = "PUBLISHED"
        if self.crash_after_publish:
            self.crash_after_publish = False
            raise SystemExit("Crash after remote commit")
        return published_id

    def media_details(self, ident):
        if ident not in self.posts:
            raise APIError("Post does not exist", status=404)
        return {"id": ident, "text": self.posts[ident].get("text", ""),
                "permalink": "https://www.threads.net/@example/post/test" + ident}


@pytest.fixture
def active_db(db, monkeypatch):
    monkeypatch.setattr(settings, "THREADS_ENABLED", True)
    db.add(SettingsStore(key="threads_started_at", value="2026-10-01T00:00:00+00:00"))
    db.commit()
    return db


def destination(db, service, sources=None):
    from app.core.threads_syncer import ThreadsOrchestrator
    sources = sources or {}
    masto = SimpleNamespace(get_status=lambda ident: sources[str(ident)])
    return ThreadsOrchestrator(db, masto=masto, client=service)


def test_a_full_mastodon_post_fits_one_threads_post(active_db):
    service = ThreadsService()
    text = "a" * 498
    assert destination(active_db, service).process_post(status(content="<p>" + text + "</p>")) == "synced"
    assert [p["text"] for p in service.posts.values()] == [text]
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "success" and row.threads_id == "2000"
    assert active_db.get(SyncMap, ID) is None


def test_continuations_keep_their_own_threads_parent(active_db):
    service = ThreadsService()
    runner = destination(active_db, service)
    assert runner.process_post(status(content="<p>" + "word " * 220 + "</p>")) == "synced"
    posts = list(service.posts.values())
    assert len(posts) == 3 and all(len(p["text"]) <= 500 for p in posts)
    assert "reply_to_id" not in posts[0]
    assert posts[1]["reply_to_id"] == "2000"
    assert posts[2]["reply_to_id"] == "2001"


@pytest.mark.parametrize("visibility", ["private", "direct", "unlisted", None])
def test_nonpublic_posts_never_create_threads_containers(active_db, visibility):
    service = ThreadsService()
    assert destination(active_db, service).process_post(status(visibility=visibility)) == "skipped"
    assert not service.containers and active_db.query(ThreadsSyncMap).count() == 0


def test_only_posts_since_threads_activation_are_eligible(active_db):
    active_db.get(SettingsStore, "threads_started_at").value = "2026-10-30T00:00:00+00:00"
    active_db.add(SyncMap(mastodon_id=ID, status="success", bluesky_uri="at://kept"))
    active_db.commit()
    service = ThreadsService()
    assert destination(active_db, service).process_post(status()) == "skipped"
    assert not service.containers
    assert active_db.get(SyncMap, ID).bluesky_uri == "at://kept"


def test_a_successful_threads_post_is_not_published_twice(active_db):
    service = ThreadsService()
    runner = destination(active_db, service)
    runner.process_post(status())
    runner.process_post(status())
    assert len(service.posts) == len(service.publish_calls) == 1


def test_an_image_and_its_alt_text_are_in_the_first_post(active_db):
    service = ThreadsService()
    post = status(content="<p>" + "word " * 120 + "</p>", media_attachments=[{
        "type": "image", "url": "https://media.example/photo.jpg", "description": "A bird on a branch",
    }])
    assert destination(active_db, service).process_post(post) == "synced"
    first, second = service.posts.values()
    assert first["media_type"] == "IMAGE"
    assert first["image_url"] == "https://media.example/photo.jpg"
    assert first["alt_text"] == "A bird on a branch"
    assert second["media_type"] == "TEXT" and "image_url" not in second


def test_four_images_form_an_ordered_carousel_with_alt_text(active_db):
    service = ThreadsService()
    media = [{"type": "image", "url": f"https://media.example/{i}.jpg", "description": f"Photo {i}"}
             for i in range(4)]
    assert destination(active_db, service).process_post(status(media_attachments=media)) == "synced"
    carousel = service.posts["2000"]
    assert carousel["media_type"] == "CAROUSEL"
    children = [service.containers[ident]["payload"] for ident in carousel["children"].split(",")]
    assert [c["image_url"] for c in children] == [m["url"] for m in media]
    assert [c["alt_text"] for c in children] == [m["description"] for m in media]
    assert all(c["is_carousel_item"] == "true" for c in children)
    assert len(service.publish_calls) == 1


def test_media_processing_waits_without_losing_the_container(active_db):
    service = ThreadsService()
    service.default_status = "IN_PROGRESS"
    post = status(media_attachments=[{"type": "image", "url": "https://media.example/photo.jpg"}])
    runner = destination(active_db, service)
    assert runner.process_post(post) == "pending"
    part = active_db.get(ThreadsSyncPart, (ID, 0))
    container = part.container_id
    assert not service.posts
    service.containers[container]["status"] = "FINISHED"
    assert runner.process_post(post) == "synced"
    assert len(service.containers) == len(service.posts) == 1


def test_a_failed_image_is_not_published_as_text_only(active_db):
    service = ThreadsService()
    service.default_status = "ERROR"
    post = status(media_attachments=[{"type": "image", "url": "https://media.example/photo.jpg"}])
    assert destination(active_db, service).process_post(post) == "failed"
    assert not service.posts
    assert "media" in active_db.get(ThreadsSyncMap, ID).error_message.lower()


def test_a_self_reply_attaches_to_the_last_part_of_its_source_parent(active_db):
    service = ThreadsService()
    root = status(content="<p>" + "word " * 120 + "</p>")
    reply = status(id=CHILD, content="<p>A follow-up.</p>", in_reply_to_id=ID, in_reply_to_account_id="42")
    runner = destination(active_db, service, {ID: root})
    assert runner.process_post(root) == "synced"
    assert runner.process_post(reply) == "synced"
    assert service.posts["2002"]["reply_to_id"] == "2001"


def test_a_reply_waits_until_its_parent_has_a_threads_mapping(active_db):
    service = ThreadsService()
    root = status()
    reply = status(id=CHILD, in_reply_to_id=ID, in_reply_to_account_id="42")
    runner = destination(active_db, service, {ID: root})
    assert runner.process_post(reply) == "pending" and not service.posts
    assert runner.process_post(root) == "synced"
    assert runner.process_post(reply) == "synced"
    assert service.posts["2001"]["reply_to_id"] == "2000"


def test_a_reply_to_a_nonpublic_parent_is_skipped(active_db):
    service = ThreadsService()
    reply = status(id=CHILD, in_reply_to_id=ID, in_reply_to_account_id="42")
    runner = destination(active_db, service, {ID: status(visibility="private")})
    assert runner.process_post(reply) == "skipped"
    assert not service.posts


def test_a_crash_after_publish_never_creates_a_duplicate(active_db):
    service = ThreadsService()
    service.crash_after_publish = True
    runner = destination(active_db, service)
    with pytest.raises(SystemExit):
        runner.process_post(status())
    active_db.rollback()
    assert len(service.posts) == 1
    assert runner.process_post(status()) == "failed"
    assert active_db.get(ThreadsSyncMap, ID).status == "needs_review"
    assert len(service.publish_calls) == 1


def test_an_uncertain_publish_response_is_held_for_review(active_db):
    service = ThreadsService()
    service.publish_error = NetworkError("Response lost")
    runner = destination(active_db, service)
    assert runner.process_post(status()) == "failed"
    assert active_db.get(ThreadsSyncMap, ID).status == "needs_review"
    assert runner.process_post(status()) == "failed"
    assert len(service.publish_calls) == 1


def test_rate_limited_publication_reuses_its_saved_container(active_db):
    service = ThreadsService()
    service.publish_error = APIError("Rate limited", status=429, transient=True, delay=120)
    runner = destination(active_db, service)
    assert runner.process_post(status()) == "pending"
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "queued" and (row.next_attempt_at - datetime.utcnow()).total_seconds() > 110
    assert runner.process_post(status()) == "pending"
    assert len(service.publish_calls) == 1
    row.next_attempt_at = None
    active_db.commit()
    service.publish_error = None
    assert runner.process_post(status()) == "synced"
    assert len(service.containers) == 1 and len(service.posts) == 1
    assert service.publish_calls[0] == service.publish_calls[1]


def test_transient_retries_have_a_durable_bound(active_db):
    service = ThreadsService()
    service.create_error = NetworkError("Service unavailable")
    runner = destination(active_db, service)
    for _ in range(3):
        runner.process_post(status())
        row = active_db.get(ThreadsSyncMap, ID)
        row.next_attempt_at = None
        active_db.commit()
    assert row.status == "failed" and row.attempt_count == 3
    assert not service.posts


def test_a_lost_draft_response_can_be_retried_without_duplicate_publication(active_db):
    service = ThreadsService()
    service.timeout_after_create = True
    runner = destination(active_db, service)
    assert runner.process_post(status()) == "pending"
    row = active_db.get(ThreadsSyncMap, ID)
    row.next_attempt_at = None
    active_db.commit()
    assert runner.process_post(status()) == "synced"
    assert len(service.containers) == 2 and len(service.posts) == 1


def test_a_changed_source_cannot_replace_a_saved_plan(active_db):
    service = ThreadsService()
    service.default_status = "IN_PROGRESS"
    runner = destination(active_db, service)
    runner.process_post(status())
    assert runner.process_post(status(content="<p>Edited text</p>")) == "failed"
    assert active_db.get(ThreadsSyncMap, ID).status == "needs_review"
    assert len(service.containers) == 1 and not service.posts


def test_changing_threads_accounts_never_moves_a_saved_job(active_db):
    service = ThreadsService()
    service.default_status = "IN_PROGRESS"
    runner = destination(active_db, service)
    runner.process_post(status())
    service.account_id = "701"
    assert runner.process_post(status()) == "failed"
    assert active_db.get(ThreadsSyncMap, ID).status == "needs_review"
    assert len(service.containers) == 1 and not service.posts


def test_bluesky_success_does_not_suppress_the_threads_destination(active_db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(active_db, bsky, [status()])
    service = ThreadsService()
    runner.threads = destination(active_db, service)
    active_db.add(SyncMap(mastodon_id=ID, status="success", bluesky_uri="at://already-published"))
    active_db.commit()
    runner.run_sync_cycle()
    assert not records and len(service.posts) == 1
    assert active_db.get(SyncMap, ID).bluesky_uri == "at://already-published"


def test_a_bluesky_failure_does_not_block_threads(active_db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    monkeypatch.setattr(bsky, "build_record", lambda *a, **kw: (_ for _ in ()).throw(ValueError("Unavailable")))
    runner = syncer(active_db, bsky, [status()])
    service = ThreadsService()
    runner.threads = destination(active_db, service)
    runner.run_sync_cycle()
    assert active_db.get(SyncMap, ID).status == "failed"
    assert active_db.get(ThreadsSyncMap, ID).status == "success" and len(service.posts) == 1


def test_a_threads_failure_does_not_change_bluesky_success(active_db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(active_db, bsky, [status()])
    service = ThreadsService()
    service.create_error = APIError("Invalid media")
    runner.threads = destination(active_db, service)
    runner.run_sync_cycle()
    assert len(records) == 1 and active_db.get(SyncMap, ID).status == "success"
    assert active_db.get(ThreadsSyncMap, ID).status == "failed"


def test_bluesky_login_failure_still_allows_threads(active_db, monkeypatch):
    from app.core import syncer as module
    def login_fails():
        raise ValueError("Bluesky login unavailable")
    monkeypatch.setattr(module, "BlueskyClient", login_fails)
    masto = SimpleNamespace(get_recent_posts=lambda limit=20: [status()])
    runner = module.SyncOrchestrator(active_db, masto=masto)
    service = ThreadsService()
    runner.threads = destination(active_db, service)
    runner.run_sync_cycle()
    assert active_db.get(ThreadsSyncMap, ID).status == "success"
    assert active_db.get(SyncMap, ID).status == "failed"


def test_long_server_retry_after_is_never_shortened(active_db):
    service = ThreadsService()
    service.create_error = APIError("Rate limit", status=429, transient=True, delay=172800)
    assert destination(active_db, service).process_post(status()) == "pending"
    row = active_db.get(ThreadsSyncMap, ID)
    assert (row.next_attempt_at - datetime.utcnow()).total_seconds() > 172790


def test_an_expired_unpublished_draft_can_be_recreated_safely(active_db):
    service = ThreadsService()
    service.default_status = "EXPIRED"
    runner = destination(active_db, service)
    assert runner.process_post(status()) == "pending"
    row = active_db.get(ThreadsSyncMap, ID)
    row.next_attempt_at = None
    active_db.commit()
    service.default_status = "FINISHED"
    assert runner.process_post(status()) == "synced"
    assert len(service.containers) == 2 and len(service.posts) == 1


def test_threads_retry_can_refresh_a_source_outside_the_recent_twenty(active_db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    service = ThreadsService()
    runner = syncer(active_db, bsky, [])
    runner.masto.get_status = lambda ident: status(id=ident)
    runner.threads = destination(active_db, service)
    active_db.add(ThreadsSyncMap(mastodon_id=ID, status="queued"))
    active_db.commit()
    runner.run_sync_cycle()
    assert len(service.posts) == 1 and active_db.get(ThreadsSyncMap, ID).status == "success"


def test_refresh_of_a_now_private_post_cannot_publish_to_threads(active_db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    service = ThreadsService()
    runner = syncer(active_db, bsky, [])
    runner.masto.get_status = lambda ident: status(id=ident, visibility="private")
    runner.threads = destination(active_db, service)
    active_db.add_all([ThreadsSyncMap(mastodon_id=ID, status="queued"),
                       SyncMap(mastodon_id=ID, status="success", bluesky_uri="at://kept")])
    active_db.commit()
    runner.run_sync_cycle()
    assert not service.posts and not records
    assert active_db.get(ThreadsSyncMap, ID).status == "skipped"
    assert active_db.get(SyncMap, ID).bluesky_uri == "at://kept"


@pytest.mark.parametrize("error_class", [requests.ConnectTimeout, MastodonNetworkError, MastodonRatelimitError, MastodonServerError])
def test_source_network_failure_is_retried_without_touching_bluesky_success(active_db, monkeypatch, error_class):
    bsky, records = bluesky_client(monkeypatch)
    service = ThreadsService()
    runner = syncer(active_db, bsky, [])
    def unavailable(ident):
        raise error_class("Source unavailable")
    runner.masto.get_status = unavailable
    runner.threads = destination(active_db, service)
    active_db.add_all([ThreadsSyncMap(mastodon_id=ID, status="queued"),
                       SyncMap(mastodon_id=ID, status="success", bluesky_uri="at://kept")])
    active_db.commit()
    runner.run_sync_cycle()
    assert active_db.get(ThreadsSyncMap, ID).status == "queued"
    assert active_db.get(ThreadsSyncMap, ID).attempt_count == 1
    assert active_db.get(SyncMap, ID).status == "success"
    assert not service.posts and not records


def test_source_outage_does_not_shorten_an_existing_provider_wait(active_db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    service = ThreadsService()
    runner = syncer(active_db, bsky, [])
    def unavailable(ident):
        raise requests.ConnectTimeout("Source unavailable")
    runner.masto.get_status = unavailable
    runner.threads = destination(active_db, service)
    deadline = datetime.utcnow() + timedelta(hours=1)
    active_db.add(ThreadsSyncMap(mastodon_id=ID, status="queued", next_attempt_at=deadline))
    active_db.commit()
    runner.run_sync_cycle()
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "queued" and row.next_attempt_at >= deadline


@pytest.mark.parametrize("caption", ["A photo caption", ""])
def test_restart_after_a_saved_part_repairs_aggregate_mapping(active_db, monkeypatch, caption):
    service = ThreadsService()
    original_details = service.media_details
    def sparse_details(ident):
        result = original_details(ident)
        if not result.get("text"):
            result.pop("text", None)
        return result
    service.media_details = sparse_details
    source = status(content=caption, media_attachments=[{"type": "image", "url": "https://media.example/photo.jpg"}])
    runner = destination(active_db, service)
    original_publish = runner._publish
    def crash_after_saved_part(part):
        original_publish(part)
        raise SystemExit("Part saved; aggregate mapping not saved yet")
    monkeypatch.setattr(runner, "_publish", crash_after_saved_part)
    with pytest.raises(SystemExit):
        runner.process_post(source)
    active_db.rollback()
    assert active_db.get(ThreadsSyncPart, (ID, 0)).threads_id == "2000"
    restarted = destination(active_db, service)
    assert restarted.process_post(source) == "synced"
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.threads_id == "2000" and row.permalink.endswith("2000")
    assert len(service.posts) == len(service.publish_calls) == 1


def test_exhausted_retries_still_preserve_the_last_server_deadline(active_db):
    service = ThreadsService()
    service.publish_error = APIError("Rate limit", status=429, transient=True, delay=3600)
    active_db.add(ThreadsSyncMap(mastodon_id=ID, status="queued", attempt_count=2))
    active_db.commit()
    runner = destination(active_db, service)
    assert runner.process_post(status()) == "failed"
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "failed" and row.attempt_count == 3
    assert row.next_attempt_at and (row.next_attempt_at - datetime.utcnow()).total_seconds() > 3590
    row.status = "queued"  # Manual retry preserves the future deadline.
    active_db.commit()
    assert runner.process_post(status()) == "pending"
    assert len(service.publish_calls) == 1


def test_future_jobs_do_not_refresh_the_source_or_spend_retry_budget(active_db, monkeypatch):
    bsky, _ = bluesky_client(monkeypatch)
    runner = syncer(active_db, bsky, [])
    source_reads = []
    def unavailable(ident):
        source_reads.append(ident)
        raise requests.ConnectTimeout("Source unavailable")
    runner.masto.get_status = unavailable
    runner.threads = destination(active_db, ThreadsService())
    active_db.add(ThreadsSyncMap(mastodon_id=ID, status="queued", attempt_count=1,
                                 next_attempt_at=datetime.utcnow() + timedelta(days=1)))
    active_db.commit()
    runner.run_sync_cycle()
    runner.run_sync_cycle()
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "queued" and row.attempt_count == 1
    assert not source_reads


def test_source_failure_for_bluesky_cannot_spend_a_future_threads_budget(active_db, monkeypatch):
    bsky, _ = bluesky_client(monkeypatch)
    runner = syncer(active_db, bsky, [])
    def unavailable(ident):
        raise requests.ConnectTimeout("Source unavailable")
    runner.masto.get_status = unavailable
    runner.threads = destination(active_db, ThreadsService())
    active_db.add_all([SyncMap(mastodon_id=ID, status="queued"),
        ThreadsSyncMap(mastodon_id=ID, status="queued", attempt_count=1,
                       next_attempt_at=datetime.utcnow() + timedelta(days=1))])
    active_db.commit()
    runner.run_sync_cycle()
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "queued" and row.attempt_count == 1
    assert active_db.get(SyncMap, ID).status == "failed"


def test_credential_maintenance_respects_backoff_without_blocking_bluesky(active_db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(active_db, bsky, [])
    service = ThreadsService()
    checks = []
    def limited():
        checks.append(1)
        raise APIError("Authorization rate limit", status=429, transient=True, delay=3600)
    service.maintain_credentials = limited
    runner.threads = destination(active_db, service)
    runner.run_sync_cycle()
    runner.run_sync_cycle()
    assert len(checks) == 1
    runner.masto.get_recent_posts = lambda limit=20: [status()]
    runner.run_sync_cycle()
    assert len(records) == 1 and not service.posts
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "queued" and (row.next_attempt_at - datetime.utcnow()).total_seconds() > 3590
