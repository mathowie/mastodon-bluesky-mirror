"""Regression tests use the real SDKs; only network boundaries are substituted."""
import json
import logging
from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import BaseAdapter
from mastodon import Mastodon, MastodonAPIError
from atproto import Client, models
from atproto_client.models.blob_ref import BlobRef
from atproto_client.models.common import XrpcError
from atproto_client.request import Response
from atproto.exceptions import BadRequestError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.mastodon import MastodonClient
from app.clients.bluesky import BlueskyClient
from app.core.syncer import SyncOrchestrator
from app.core.transformer import ContentTransformer
from app.database import Base, SyncMap, SettingsStore

DID = "did:plc:abcdefghijklmnopqrstuvwx"
CID = "bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku"


def status(**changes):
    result = {
        "id": "100000000000000001", "visibility": "public",
        "created_at": "2026-10-29T12:00:00.000Z",
        "edited_at": None, "in_reply_to_id": None,
        "in_reply_to_account_id": None, "sensitive": False,
        "spoiler_text": "", "content": "<p>Hello from Mastodon!</p>",
        "media_attachments": [], "mentions": [], "tags": [], "emojis": [],
        "reblog": None, "quote": None, "poll": None,
        "uri": "https://mastodon.example/users/test/statuses/100000000000000001",
        "url": "https://mastodon.example/@test/100000000000000001",
        "account": {"id": "42", "username": "test", "acct": "test"},
    }
    result.update(changes)
    return result


class MastodonAdapter(BaseAdapter):
    def __init__(self, unavailable=False):
        self.unavailable = unavailable

    def send(self, request, **kwargs):
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.headers["Content-Type"] = "application/json"
        response.status_code = 503 if self.unavailable else 200
        if self.unavailable:
            body = {"error": "Temporarily unavailable"}
        elif request.path_url == "/api/v1/accounts/verify_credentials":
            body = {"id": "42", "username": "test", "acct": "test"}
        elif request.path_url.startswith("/api/v1/accounts/42/statuses"):
            body = [status()]
        else:
            raise AssertionError("Unexpected Mastodon request: " + request.path_url)
        response._content = json.dumps(body).encode()
        return response

    def close(self):
        pass


def mastodon_client(unavailable=False):
    session = requests.Session()
    session.mount("https://", MastodonAdapter(unavailable))
    wrapper = object.__new__(MastodonClient)
    wrapper.api = Mastodon(
        api_base_url="https://mastodon.example", access_token="not-a-real-token",
        version_check_mode="none", session=session,
    )
    return wrapper


class RecordLog(list):
    def __init__(self):
        super().__init__()
        self.by_key = {}
        self.uris = []
        self.cids = []


def bluesky_client(monkeypatch):
    sdk = Client()
    sdk.me = models.AppBskyActorDefs.ProfileViewDetailed(did=DID, handle="test.bsky.social")
    records = RecordLog()

    def capture(repo, record, rkey=None, **kwargs):
        assert repo == DID
        key = rkey or f"test{len(records)+1}"
        if key in records.by_key:
            raise BadRequestError(Response(False, 400, XrpcError("InvalidRequest", "Record already exists"), {}))
        # Distinct valid CIDs let tests catch wrong root/parent CID references.
        import base64, hashlib
        cid = CID if not records else "b" + base64.b32encode(b"\x01\x55\x12\x20" + hashlib.sha256(key.encode()).digest()).decode().lower().rstrip("=")
        uri = f"at://{DID}/app.bsky.feed.post/{key}"
        records.append(record.model_copy(deep=True))
        records.uris.append(uri)
        records.cids.append(cid)
        records.by_key[key] = {"uri": uri, "cid": cid, "value": record.model_copy(deep=True)}
        return models.AppBskyFeedPost.CreateRecordResponse(uri=uri, cid=cid)

    def read(repo, rkey, **kwargs):
        assert repo == DID
        if rkey not in records.by_key:
            raise BadRequestError(Response(False, 400, XrpcError("RecordNotFound", "Record not found"), {}))
        return models.AppBskyFeedPost.GetRecordResponse(**records.by_key[rkey])

    monkeypatch.setattr(sdk.app.bsky.feed.post, "create", capture)
    monkeypatch.setattr(sdk.app.bsky.feed.post, "get", read)
    wrapper = object.__new__(BlueskyClient)
    wrapper.client = sdk
    return wrapper, records


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(SettingsStore(key="sync_started_at", value="2026-10-01T00:00:00+00:00"))
        session.commit()
        yield session
    engine.dispose()


def syncer(db, bsky, posts):
    runner = object.__new__(SyncOrchestrator)
    runner.db = db
    runner.masto = SimpleNamespace(get_recent_posts=lambda limit=20: posts)
    runner.bsky = bsky
    runner.transformer = ContentTransformer()
    return runner


def test_authenticated_account_timeline_uses_the_current_user():
    posts = mastodon_client().get_recent_posts()
    assert [str(post["id"]) for post in posts] == ["100000000000000001"]


def test_timeline_failure_is_not_disguised_as_an_empty_timeline():
    with pytest.raises(MastodonAPIError):
        mastodon_client(unavailable=True).get_recent_posts()


def test_text_post_is_accepted_by_the_real_bluesky_sdk(monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    uri, cid = bsky.post_text("Hello Bluesky")
    assert records[0].text == "Hello Bluesky"
    assert records[0].reply is None
    assert uri.endswith("/test1") and cid == CID


def test_public_mastodon_status_gets_published_and_mapped(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(db, bsky, [status()])
    runner.run_sync_cycle()
    assert len(records) == 1
    row = db.get(SyncMap, "100000000000000001")
    assert row.status == "success"
    assert row.bluesky_uri == records.uris[0]


@pytest.mark.parametrize("visibility", ["private", "direct", "unlisted", None])
def test_nonpublic_statuses_never_leave_mastodon(db, monkeypatch, visibility):
    bsky, records = bluesky_client(monkeypatch)
    syncer(db, bsky, [status(visibility=visibility)]).run_sync_cycle()
    assert records == []
    assert db.query(SyncMap).count() == 0


def test_successful_status_is_not_republished_on_the_next_poll(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(db, bsky, [status()])
    runner.run_sync_cycle()
    runner.run_sync_cycle()
    assert len(records) == 1
    assert db.query(SyncMap).count() == 1


def test_thread_keeps_one_root_and_chains_each_parent(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(db, bsky, [status(content="<p>" + "a " * 400 + "</p>")])
    runner.run_sync_cycle()
    assert len(records) == 3
    assert records[0].reply is None
    assert records[1].reply.root.uri == records.uris[0]
    assert records[1].reply.parent.uri == records.uris[0]
    assert records[2].reply.root.uri == records.uris[0]
    assert records[2].reply.root.cid == records.cids[0]
    assert records[2].reply.parent.uri == records.uris[1]
    assert records[2].reply.parent.cid == records.cids[1]


def test_image_blob_and_alt_text_use_real_sdk_models(monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    blob = BlobRef.model_validate({
        "$type": "blob", "ref": {"$link": CID}, "mimeType": "image/jpeg", "size": 3,
    })
    bsky.post_text("An image", images=[(blob, "A cat sitting by a window")])
    assert records[0].embed.images[0].alt == "A cat sitting by a window"
    assert records[0].embed.images[0].image.mime_type == "image/jpeg"


def test_mastodon_images_are_read_from_media_attachments():
    post = status(media_attachments=[{
        "id": "7", "type": "image", "url": "https://cdn.example/cat.jpg",
        "description": "A cat", "preview_url": "https://cdn.example/preview.jpg",
    }])
    assert ContentTransformer.process_media(post) == [("https://cdn.example/cat.jpg", "A cat")]


def test_html_paragraphs_breaks_and_entities_remain_readable():
    text = ContentTransformer.clean_text("<p>Fish &amp; chips<br>Next line</p><p>New paragraph</p>")
    assert text == "Fish & chips\nNext line\n\nNew paragraph"


def test_sync_errors_do_not_expose_credentials(db, monkeypatch, caplog):
    bsky, _ = bluesky_client(monkeypatch)
    runner = syncer(db, bsky, [])
    def unavailable(limit=20):
        raise RuntimeError("Request failed with test-mastodon-secret")
    runner.masto.get_recent_posts = unavailable
    with caplog.at_level(logging.INFO):
        runner.run_sync_cycle()
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert "test-mastodon-secret" not in caplog.text


def test_boosts_are_not_mirrored_by_default(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    syncer(db, bsky, [status(reblog=status(id="99"))]).run_sync_cycle()
    assert records == []


def test_replies_are_not_mirrored_by_default(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    syncer(db, bsky, [status(in_reply_to_id="99")]).run_sync_cycle()
    assert records == []


def test_links_and_hashtags_have_clickable_utf8_facets(monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    bsky.post_text("Café https://example.com #Mirror")
    facets = records[0].facets
    assert facets is not None
    assert [(f.index.byte_start, f.index.byte_end) for f in facets] == [(6, 25), (26, 33)]
    assert facets[0].features[0].uri == "https://example.com"
    assert facets[1].features[0].tag == "Mirror"


def test_descriptive_links_keep_their_destination():
    text = ContentTransformer.clean_text('<p>Read <a href="https://example.com/story">this story</a>.</p>')
    assert text == "Read this story (https://example.com/story)."


def test_posts_older_than_activation_are_never_backfilled(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    syncer(db, bsky, [status(created_at="2020-01-01T00:00:00Z")]).run_sync_cycle()
    assert records == []
    assert db.query(SyncMap).count() == 0


def test_first_activation_persists_a_start_time_before_fetching(db, monkeypatch):
    from datetime import datetime, timezone, timedelta
    db.delete(db.get(SettingsStore, "sync_started_at"))
    db.commit()
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(db, bsky, [status(created_at="2020-01-01T00:00:00Z")])
    runner.run_sync_cycle()
    assert records == []
    baseline = db.get(SettingsStore, "sync_started_at")
    assert baseline is not None
    started = datetime.fromisoformat(baseline.value)
    runner.masto.get_recent_posts = lambda limit=20: [status(
        id="100000000000000002", created_at=started + timedelta(seconds=1),
    )]
    runner.run_sync_cycle()
    assert len(records) == 1
    assert db.get(SettingsStore, "sync_started_at").value == started.isoformat()


def test_an_image_upload_returns_a_blob_not_a_response_envelope(monkeypatch):
    from PIL import Image
    from io import BytesIO
    bsky, _ = bluesky_client(monkeypatch)
    output = BytesIO()
    Image.new("RGB", (2, 2), (20, 30, 40)).save(output, "JPEG")
    data = output.getvalue()
    blob = BlobRef.model_validate({
        "$type": "blob", "ref": {"$link": CID}, "mimeType": "image/jpeg", "size": len(data),
    })
    monkeypatch.setattr(bsky.client, "upload_blob", lambda data: SimpleNamespace(blob=blob))
    uploaded = bsky.upload_image(data, "A cat")
    assert uploaded.mime_type == "image/jpeg"
    assert uploaded.size == len(data)
