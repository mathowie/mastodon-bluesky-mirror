"""Threads requests run through a real Session and an entirely local adapter."""
import importlib
import json
import traceback
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter


NOW = 1_800_000_000
LIFETIME = 5_184_000
ACCOUNT = {"id": "10001", "username": "mirror_test"}


class ScriptedAdapter(BaseAdapter):
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        assert urlsplit(request.url).netloc == "graph.threads.net"
        assert self.responses, "Unexpected HTTP request"
        reply = self.responses.pop(0)
        if callable(reply):
            reply = reply(request, kwargs)
        if isinstance(reply, Exception):
            raise reply
        status, body, *headers = reply
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers[0] if headers else {})
        response._content = (body if isinstance(body, bytes) else json.dumps(body).encode())
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def session_for(*responses):
    adapter = ScriptedAdapter(*responses)
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session, adapter


def config_for(tmp_path, **changes):
    values = dict(
        THREADS_ENABLED=True,
        THREADS_ACCESS_TOKEN="imported-test-token",
        THREADS_APP_SECRET=None,
        THREADS_USER_ID="10001",
        THREADS_TOKEN_EXPIRES_AT=NOW + LIFETIME,
        THREADS_TOKEN_FILE=str(tmp_path / "private" / "threads-token.json"),
    )
    values.update(changes)
    return SimpleNamespace(**values)


def load_module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Required Threads component is not implemented: {name}")


def make_client(tmp_path, monkeypatch, *responses, **changes):
    module = load_module("app.clients.threads")
    auth = load_module("app.core.threads_auth")
    monkeypatch.setattr(auth.time, "time", lambda: NOW)
    session, adapter = session_for(*responses)
    config = config_for(tmp_path, **changes)
    return module.ThreadsClient(config=config, session=session), adapter, config


def form(request):
    return parse_qs(request.body or "")


@pytest.mark.parametrize("payload,encoded", [
    ({"media_type": "TEXT", "text": "Hello", "reply_to_id": "20001"},
     {"media_type": ["TEXT"], "text": ["Hello"], "reply_to_id": ["20001"]}),
    ({"media_type": "IMAGE", "image_url": "https://images.example/photo.jpg", "alt_text": "A tree", "is_carousel_item": True},
     {"media_type": ["IMAGE"], "image_url": ["https://images.example/photo.jpg"], "alt_text": ["A tree"], "is_carousel_item": ["true"]}),
    ({"media_type": "CAROUSEL", "text": "Photos", "children": ["30001", "30002"]},
     {"media_type": ["CAROUSEL"], "text": ["Photos"], "children": ["30001,30002"]}),
])
def test_create_unpublished_container_with_documented_payload(tmp_path, monkeypatch, payload, encoded):
    client, adapter, _ = make_client(tmp_path, monkeypatch, (200, ACCOUNT), (200, {"id": "40001"}))
    assert client.create_container(payload) == "40001"
    request, options = adapter.calls[-1]
    assert request.method == "POST"
    assert request.url == "https://graph.threads.net/10001/threads"
    assert form(request) == encoded
    assert "auto_publish_text" not in form(request)
    assert request.headers["Authorization"] == "Bearer imported-test-token"
    assert "access_token" not in request.url
    assert options["timeout"] == (5, 20)
    assert payload.get("is_carousel_item") in (None, True)


def test_status_publish_and_permalink_are_separate_single_requests(tmp_path, monkeypatch):
    client, adapter, _ = make_client(
        tmp_path, monkeypatch, (200, ACCOUNT),
        (200, {"id": "40001", "status": "FINISHED"}),
        (200, {"id": "50001"}),
        (200, {"id": "50001", "permalink": "https://www.threads.net/@mirror_test/post/example", "text": "Hello"}),
    )
    assert client.account_id == "10001"
    assert client.container_status("40001") == {"id": "40001", "status": "FINISHED"}
    assert client.publish_container("40001") == "50001"
    assert client.media_details("50001")["permalink"].endswith("/example")
    assert [(r.method, urlsplit(r.url).path) for r, _ in adapter.calls] == [
        ("GET", "/me"), ("GET", "/40001"), ("POST", "/10001/threads_publish"), ("GET", "/50001"),
    ]
    assert parse_qs(urlsplit(adapter.calls[0][0].url).query) == {"fields": ["id,username"]}
    assert parse_qs(urlsplit(adapter.calls[1][0].url).query) == {"fields": ["id,status,error_message"]}
    assert form(adapter.calls[2][0]) == {"creation_id": ["40001"]}
    assert parse_qs(urlsplit(adapter.calls[3][0].url).query) == {"fields": ["id,permalink,text"]}
    assert all(options["timeout"] == (5, 20) for _, options in adapter.calls)


@pytest.mark.parametrize("status,transient", [(400, False), (401, False), (429, True), (500, True), (503, True)])
def test_api_errors_are_safe_classified_and_never_retried(tmp_path, monkeypatch, status, transient):
    client, adapter, _ = make_client(tmp_path, monkeypatch, (200, ACCOUNT), (
        status, {"error": {"message": "https://evil.example/?access_token=imported-test-token secret=provider-unknown-secret", "code": 190}},
        {"Retry-After": "37"},
    ))
    module = load_module("app.clients.threads")
    with pytest.raises(module.ThreadsAPIError) as caught:
        client.publish_container("40001")
    error = caught.value
    assert error.http_status == status
    assert error.transient is transient
    assert error.retry_after == 37
    assert len(adapter.calls) == 2
    public = str(error) + repr(error) + str(vars(error)) + "".join(traceback.format_exception(error))
    for unsafe in ("https://", "imported-test-token", "provider-unknown-secret"):
        assert unsafe not in public


@pytest.mark.parametrize("retry_after,want", [
    ("-2", 0), ("garbage", 0), ("NaN", 0), ("inf", 0), ("1.5", 1.5),
    ("Fri, 15 Jan 2027 08:01:00 GMT", 60),
])
def test_retry_after_is_finite_nonnegative_seconds(tmp_path, monkeypatch, retry_after, want):
    client, _, _ = make_client(tmp_path, monkeypatch, (200, ACCOUNT), (429, {"error": {}}, {"Retry-After": retry_after}))
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError) as caught:
        client.publish_container("40001")
    assert caught.value.retry_after == want


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError])
def test_transport_failures_preserve_uncertainty_without_leaking_exception(tmp_path, monkeypatch, failure):
    client, adapter, _ = make_client(tmp_path, monkeypatch, (200, ACCOUNT), failure("https://graph.threads.net/private?access_token=imported-test-token"))
    with pytest.raises(load_module("app.clients.threads").ThreadsTransportError) as caught:
        client.publish_container("40001")
    assert caught.value.uncertain is True
    assert caught.value.http_status is None
    assert caught.value.transient is True
    assert caught.value.retry_after == 0
    assert len(adapter.calls) == 2
    rendered = "".join(traceback.format_exception(caught.value))
    assert "https://" not in rendered
    assert "imported-test-token" not in rendered


@pytest.mark.parametrize("body", [b"not JSON imported-test-token", {}, {"id": None}])
def test_success_without_published_id_is_ambiguous(tmp_path, monkeypatch, body):
    client, adapter, _ = make_client(tmp_path, monkeypatch, (200, ACCOUNT), (200, body))
    with pytest.raises(load_module("app.clients.threads").ThreadsTransportError):
        client.publish_container("40001")
    assert len(adapter.calls) == 2


def test_provider_container_error_text_is_not_exposed(tmp_path, monkeypatch):
    client, _, _ = make_client(tmp_path, monkeypatch, (200, ACCOUNT), (200, {
        "id": "40001", "status": "ERROR", "error_message": "https://private.example/?access_token=unknown-provider-token",
    }))
    result = client.container_status("40001")
    assert result["status"] == "ERROR"
    assert result["error_message"]
    assert "https://" not in json.dumps(result)
    assert "unknown-provider-token" not in json.dumps(result)


def test_redirect_does_not_send_credentials_to_another_host(tmp_path, monkeypatch):
    client, adapter, _ = make_client(tmp_path, monkeypatch, (302, {}, {"Location": "https://evil.example/token"}))
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.account_id
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("bad_id", ["https://evil.example", "../me", "40001?access_token=unsafe"])
def test_invalid_identifiers_are_rejected_before_http(tmp_path, monkeypatch, bad_id):
    client, adapter, _ = make_client(tmp_path, monkeypatch)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.publish_container(bad_id)
    assert adapter.calls == []


def test_auto_publish_payload_is_rejected_before_http(tmp_path, monkeypatch):
    client, adapter, _ = make_client(tmp_path, monkeypatch)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.create_container({"media_type": "TEXT", "text": "Hello", "auto_publish_text": True})
    assert adapter.calls == []


def test_connection_status_before_authorization_is_local_and_read_only(tmp_path, monkeypatch):
    client, adapter, config = make_client(tmp_path, monkeypatch)
    result = client.connection_status()
    assert result["configured"] is True
    assert result["account_id"] is None
    assert set(result) <= {"configured", "account_id", "username", "expires_at", "status", "error"}
    assert adapter.calls == []
    assert not (tmp_path / "private").exists()
    assert config.THREADS_ACCESS_TOKEN not in json.dumps(result)


@pytest.mark.parametrize("flag", ["true", "false"])
def test_carousel_accepts_documented_string_flags(tmp_path, monkeypatch, flag):
    client, adapter, _ = make_client(tmp_path, monkeypatch, (200, ACCOUNT), (200, {"id": "40001"}))
    payload = {"media_type": "IMAGE", "image_url": "https://images.example/photo.jpg", "is_carousel_item": flag}
    assert client.create_container(payload) == "40001"
    assert form(adapter.calls[-1][0])["is_carousel_item"] == [flag]
    assert payload["is_carousel_item"] == flag
