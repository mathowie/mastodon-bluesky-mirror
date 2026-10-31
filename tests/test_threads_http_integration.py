"""Run the durable destination through its real HTTP client and token store."""
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from app.clients.threads import ThreadsClient
from app.core.threads_syncer import ThreadsOrchestrator
from app.database import SyncMap, ThreadsSyncMap, ThreadsSyncPart
from test_sync_regressions import status, bluesky_client, syncer
from test_threads_sync import active_db, db, ID, ThreadsService


class GraphAdapter(BaseAdapter):
    def __init__(self, service):
        self.service = service
        self.profile_reads = 0
        self.refreshes = 0
        self.token = "synthetic-long-token"

    def send(self, request, **kwargs):
        assert request.url.startswith("https://graph.threads.net/")
        assert kwargs["timeout"] == (5, 20)
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        if path != "/refresh_access_token":
            assert request.headers.get("Authorization") == "Bearer " + self.token
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        data = {key: values[0] for key, values in parse_qs(body or "").items()}
        if request.method == "GET" and path == "/refresh_access_token":
            assert query["access_token"] == [self.token]
            self.refreshes += 1
            self.token = "synthetic-refreshed-token"
            payload = {"access_token": self.token, "token_type": "bearer", "expires_in": 5184000}
        elif request.method == "GET" and path == "/me":
            self.profile_reads += 1
            payload = {"id": "700", "username": "example"}
        elif request.method == "POST" and path == "/700/threads":
            assert "auto_publish_text" not in data
            payload = {"id": self.service.create_container(data)}
        elif request.method == "POST" and path == "/700/threads_publish":
            payload = {"id": self.service.publish_container(data["creation_id"])}
        elif request.method == "GET" and path[1:] in self.service.containers:
            payload = self.service.container_status(path[1:])
        elif request.method == "GET" and path[1:] in self.service.posts:
            payload = self.service.media_details(path[1:])
        else:
            raise AssertionError("Unexpected request method/path: " + request.method + " " + path)
        response = requests.Response()
        response.request, response.status_code = request, 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        return response

    def close(self):
        pass


def client(tmp_path, service):
    config = SimpleNamespace(
        THREADS_ENABLED=True, THREADS_ACCESS_TOKEN="synthetic-long-token", THREADS_APP_SECRET=None,
        THREADS_USER_ID="700", THREADS_TOKEN_EXPIRES_AT=int(time.time()) + 60 * 86400,
        THREADS_TOKEN_FILE=str(tmp_path / "private-token.json"),
    )
    adapter = GraphAdapter(service)
    session = requests.Session()
    session.mount("https://", adapter)
    return ThreadsClient(config=config, session=session), adapter


def test_real_http_client_publishes_one_photo_with_caption_and_alt_text(active_db, tmp_path):
    service = ThreadsService()
    http, adapter = client(tmp_path, service)
    runner = ThreadsOrchestrator(active_db, SimpleNamespace(), client=http)
    source = status(content="<p>Café &amp; a photo.</p>", media_attachments=[{
        "type": "image", "url": "https://media.example/photo.jpg", "description": "A café window",
    }])
    assert runner.process_post(source) == "synced"
    assert service.posts["2000"] == {
        "media_type": "IMAGE", "text": "Café & a photo.", "image_url": "https://media.example/photo.jpg",
        "alt_text": "A café window",
    }
    assert active_db.get(ThreadsSyncMap, ID).status == "success"
    assert active_db.get(ThreadsSyncPart, (ID, 0)).account_id == "700"
    assert runner.process_post(source) == "existing"
    assert adapter.profile_reads == 1 and len(service.publish_calls) == 1


def test_real_http_client_builds_carousel_children_before_the_parent(active_db, tmp_path):
    service = ThreadsService()
    http, _ = client(tmp_path, service)
    runner = ThreadsOrchestrator(active_db, SimpleNamespace(), client=http)
    source = status(media_attachments=[
        {"type": "image", "url": "https://media.example/one.jpg", "description": "First photo"},
        {"type": "image", "url": "https://media.example/two.jpg", "description": "Second photo"},
    ])
    assert runner.process_post(source) == "synced"
    post = service.posts["2000"]
    assert post["media_type"] == "CAROUSEL"
    assert post["children"] == "1000,1001"
    assert service.containers["1000"]["payload"]["alt_text"] == "First photo"
    assert service.containers["1001"]["payload"]["alt_text"] == "Second photo"


def test_real_client_restart_never_repeats_an_uncertain_remote_commit(active_db, tmp_path):
    service = ThreadsService()
    http, _ = client(tmp_path, service)
    service.crash_after_publish = True
    runner = ThreadsOrchestrator(active_db, SimpleNamespace(), client=http)
    with pytest.raises(SystemExit):
        runner.process_post(status())
    active_db.rollback()
    replacement, _ = client(tmp_path, service)
    restarted = ThreadsOrchestrator(active_db, SimpleNamespace(), client=replacement)
    assert restarted.process_post(status()) == "failed"
    row = active_db.get(ThreadsSyncMap, ID)
    assert row.status == "needs_review" and "PUBLISHED" in row.error_message
    assert len(service.posts) == len(service.publish_calls) == 1


@pytest.mark.parametrize("empty_timeline", [True, False])
def test_quiet_polling_renews_credentials_without_publishing(active_db, tmp_path, monkeypatch, empty_timeline):
    service = ThreadsService()
    http, adapter = client(tmp_path, service)
    assert http.account_id == "700"
    saved = json.loads((tmp_path / "private-token.json").read_text())
    monkeypatch.setattr("app.core.threads_auth.time.time", lambda: saved["refresh_at"] + 1)
    bsky, records = bluesky_client(monkeypatch)
    active_db.add_all([SyncMap(mastodon_id=ID, status="success", bluesky_uri="at://kept"),
                       ThreadsSyncMap(mastodon_id=ID, status="success", account_id="700", threads_id="2000")])
    active_db.commit()
    runner = syncer(active_db, bsky, [] if empty_timeline else [status()])
    runner.threads = ThreadsOrchestrator(active_db, SimpleNamespace(), client=http)
    runner.run_sync_cycle()
    renewed = json.loads((tmp_path / "private-token.json").read_text())
    assert adapter.refreshes == 1
    assert renewed["access_token"] == "synthetic-refreshed-token"
    assert renewed["expires_at"] > saved["expires_at"]
    assert not service.posts and not records
