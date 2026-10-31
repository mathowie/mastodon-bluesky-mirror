"""Credential lifecycle uses real private files and substituted HTTP only."""
import hashlib
import json
import stat
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from test_threads_client import ACCOUNT, LIFETIME, NOW, config_for, load_module, make_client, session_for


def read_token(config):
    return json.loads(Path(config.THREADS_TOKEN_FILE).read_text())


def bootstrap(tmp_path, monkeypatch, **changes):
    client, adapter, config = make_client(
        tmp_path, monkeypatch,
        (200, {"access_token": "renewable-test-token", "token_type": "bearer", "expires_in": LIFETIME}),
        (200, ACCOUNT),
        THREADS_ACCESS_TOKEN="bootstrap-test-token", THREADS_APP_SECRET="fake-app-secret",
        THREADS_TOKEN_EXPIRES_AT=None, **changes,
    )
    assert client.account_id == "10001"
    return client, adapter, config


def test_short_token_exchange_verifies_and_persists_private_identity(tmp_path, monkeypatch):
    client, adapter, config = bootstrap(tmp_path, monkeypatch)
    request, options = adapter.calls[0]
    assert request.method == "GET"
    assert urlsplit(request.url).path == "/access_token"
    assert parse_qs(urlsplit(request.url).query) == {
        "grant_type": ["th_exchange_token"], "client_secret": ["fake-app-secret"], "access_token": ["bootstrap-test-token"],
    }
    assert options["timeout"] == (5, 20)
    assert adapter.calls[1][0].headers["Authorization"] == "Bearer renewable-test-token"
    token_path = Path(config.THREADS_TOKEN_FILE)
    saved = read_token(config)
    assert saved["access_token"] == "renewable-test-token"
    assert saved["account_id"] == "10001"
    assert saved["username"] == "mirror_test"
    assert saved["expires_at"] == NOW + LIFETIME
    assert saved["bootstrap_fingerprint"] == hashlib.sha256(b"bootstrap-test-token").hexdigest()
    assert "bootstrap-test-token" not in token_path.read_text()
    assert "fake-app-secret" not in token_path.read_text()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert list(token_path.parent.iterdir()) == [token_path]
    status = client.connection_status()
    assert status["username"] == "mirror_test"
    assert status["account_id"] == "10001"
    assert status["expires_at"] == NOW + LIFETIME
    assert "renewable-test-token" not in json.dumps(status)


def test_explicit_long_lived_import_does_not_exchange(tmp_path, monkeypatch):
    client, adapter, config = make_client(tmp_path, monkeypatch, (200, ACCOUNT))
    assert client.account_id == "10001"
    assert len(adapter.calls) == 1
    assert read_token(config)["expires_at"] == NOW + LIFETIME


def test_restart_uses_saved_refreshable_token_and_original_expiry(tmp_path, monkeypatch):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    contents = path.read_bytes()
    session, adapter = session_for((200, ACCOUNT))
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    assert client.connection_status()["expires_at"] == NOW + LIFETIME
    assert adapter.calls == []
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + 60)
    assert client.account_id == "10001"
    assert adapter.calls[0][0].headers["Authorization"] == "Bearer renewable-test-token"
    assert len(adapter.calls) == 1
    assert path.read_bytes() == contents


def test_private_file_can_supply_credentials_without_environment_token(tmp_path, monkeypatch):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    config.THREADS_ACCESS_TOKEN = None
    config.THREADS_APP_SECRET = None
    session, adapter = session_for((200, ACCOUNT))
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    assert client.account_id == "10001"
    assert len(adapter.calls) == 1


def test_refresh_is_saved_atomically_before_any_container_uses_it(tmp_path, monkeypatch):
    client, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    inode = path.stat().st_ino
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + LIFETIME - 60)

    def create_after_save(request, options):
        saved = read_token(config)
        assert saved["access_token"] == "refreshed-test-token"
        assert saved["expires_at"] == NOW + 2 * LIFETIME - 60
        assert path.stat().st_ino != inode
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert request.headers["Authorization"] == "Bearer refreshed-test-token"
        return 200, {"id": "40001"}

    session, adapter = session_for(
        (200, {"access_token": "refreshed-test-token", "token_type": "bearer", "expires_in": LIFETIME}),
        (200, ACCOUNT), create_after_save,
    )
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    assert client.create_container({"media_type": "TEXT", "text": "Hello"}) == "40001"
    refresh = adapter.calls[0][0]
    assert urlsplit(refresh.url).path == "/refresh_access_token"
    assert parse_qs(urlsplit(refresh.url).query) == {
        "grant_type": ["th_refresh_token"], "access_token": ["renewable-test-token"],
    }
    assert "Authorization" not in refresh.headers
    assert read_token(config)["bootstrap_fingerprint"] == hashlib.sha256(b"bootstrap-test-token").hexdigest()


@pytest.mark.parametrize("changes", [
    {"THREADS_ACCESS_TOKEN": "changed-env-token"}, {"THREADS_USER_ID": "90009"},
])
def test_changed_bootstrap_or_expected_account_fails_closed_without_network(tmp_path, monkeypatch, changes):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    before = Path(config.THREADS_TOKEN_FILE).read_bytes()
    for key, value in changes.items():
        setattr(config, key, value)
    session, adapter = session_for()
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.publish_container("40001")
    assert adapter.calls == []
    assert Path(config.THREADS_TOKEN_FILE).read_bytes() == before


def test_changed_remote_account_is_never_persisted_or_used(tmp_path, monkeypatch):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    before = Path(config.THREADS_TOKEN_FILE).read_bytes()
    session, adapter = session_for((200, {"id": "90009", "username": "another_test"}))
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.create_container({"media_type": "TEXT", "text": "Never publish"})
    assert len(adapter.calls) == 1
    assert Path(config.THREADS_TOKEN_FILE).read_bytes() == before


@pytest.mark.parametrize("change", [
    {"expires_at": NOW - 1}, {"expires_at": "tomorrow"}, {"expires_at": float("nan")},
    {"account_id": "https://unsafe.example"}, {"username": "token=unknown-secret"},
    {"bootstrap_fingerprint": "bad"}, {"access_token": ""}, {"version": 999},
])
def test_invalid_or_expired_private_metadata_never_falls_back_to_env(tmp_path, monkeypatch, change):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    saved = read_token(config)
    saved.update(change)
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    session, adapter = session_for()
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    status = client.connection_status()
    assert status["error"]
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.account_id
    assert adapter.calls == []
    assert path.read_bytes() == before
    assert "unknown-secret" not in json.dumps(status)


@pytest.mark.parametrize("file_type", ["invalid_json", "public", "symlink"])
def test_untrusted_private_file_is_rejected_locally(tmp_path, monkeypatch, file_type):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    if file_type == "invalid_json":
        path.write_text("not JSON token=private-file-secret")
    elif file_type == "public":
        path.chmod(0o644)
    else:
        target = tmp_path / "actual-token.json"
        path.rename(target)
        path.symlink_to(target)
    session, adapter = session_for()
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError) as caught:
        client.account_id
    assert adapter.calls == []
    assert "private-file-secret" not in str(caught.value)
    assert "private-file-secret" not in json.dumps(client.connection_status())


@pytest.mark.parametrize("changes", [
    {"THREADS_ACCESS_TOKEN": None},
    {"THREADS_TOKEN_EXPIRES_AT": NOW - 1},
    {"THREADS_TOKEN_EXPIRES_AT": None, "THREADS_APP_SECRET": None},
])
def test_unusable_bootstrap_does_not_contact_provider(tmp_path, monkeypatch, changes):
    client, adapter, _ = make_client(tmp_path, monkeypatch, **changes)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.account_id
    assert adapter.calls == []


@pytest.mark.parametrize("exchange", [
    {"access_token": "renewable-test-token", "token_type": "bearer", "expires_in": 0},
    {"access_token": "renewable-test-token", "token_type": "bearer", "expires_in": "invalid"},
    {"access_token": "renewable-test-token", "token_type": "other", "expires_in": LIFETIME},
    {"expires_in": LIFETIME},
])
def test_invalid_exchange_metadata_is_not_saved(tmp_path, monkeypatch, exchange):
    client, adapter, config = make_client(tmp_path, monkeypatch, (200, exchange),
        THREADS_TOKEN_EXPIRES_AT=None, THREADS_APP_SECRET="fake-app-secret")
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.account_id
    assert len(adapter.calls) == 1
    assert not Path(config.THREADS_TOKEN_FILE).exists()


def test_failed_refresh_keeps_original_file_and_blocks_writes(tmp_path, monkeypatch):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    before = path.read_bytes()
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + LIFETIME - 60)
    session, adapter = session_for((401, {"error": {"message": "secret=renewable-test-token"}}))
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.publish_container("40001")
    assert path.read_bytes() == before
    assert len(adapter.calls) == 1


def test_identity_mismatch_during_refresh_keeps_original_file(tmp_path, monkeypatch):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    before = path.read_bytes()
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + LIFETIME - 60)
    session, adapter = session_for(
        (200, {"access_token": "wrong-account-token", "token_type": "bearer", "expires_in": LIFETIME}),
        (200, {"id": "90009", "username": "another_test"}),
    )
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.publish_container("40001")
    assert len(adapter.calls) == 2
    assert path.read_bytes() == before


def test_save_failure_blocks_container_request_and_has_safe_error(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    parent.write_text("this is a file")
    client, adapter, config = make_client(tmp_path, monkeypatch, (200, ACCOUNT))
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError) as caught:
        client.create_container({"media_type": "TEXT", "text": "Hello"})
    assert len(adapter.calls) <= 1
    assert "imported-test-token" not in str(caught.value)
    assert str(parent) not in str(caught.value)
    assert not Path(config.THREADS_TOKEN_FILE).exists()


def test_cli_prompts_hidden_and_initializes_while_disabled(tmp_path, monkeypatch, capsys):
    module = load_module("app.threads_setup")
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW)
    prompts = []
    answers = iter(["hidden-short-token", "hidden-app-secret"])
    def hidden(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr(module.getpass, "getpass", hidden)
    config = config_for(tmp_path, THREADS_ENABLED=False, THREADS_ACCESS_TOKEN=None,
                        THREADS_APP_SECRET=None, THREADS_TOKEN_EXPIRES_AT=None)
    session, adapter = session_for(
        (200, {"access_token": "hidden-long-token", "token_type": "bearer", "expires_in": LIFETIME}), (200, ACCOUNT),
    )
    assert module.main(config=config, session=session) == 0
    output = capsys.readouterr()
    assert "mirror_test" in output.out
    assert "10001" in output.out
    assert "2027" in output.out
    assert output.err == ""
    assert len(prompts) == 2
    assert all(request.method == "GET" for request, _ in adapter.calls)
    assert "hidden-" not in output.out
    assert config.THREADS_ACCESS_TOKEN is None


def test_cli_uses_existing_private_file_without_prompts(tmp_path, monkeypatch, capsys):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    config.THREADS_ENABLED = False
    config.THREADS_ACCESS_TOKEN = None
    config.THREADS_APP_SECRET = None
    module = load_module("app.threads_setup")
    def unexpected_prompt(_):
        pytest.fail("A saved renewable token must not require another bootstrap")
    monkeypatch.setattr(module.getpass, "getpass", unexpected_prompt)
    session, adapter = session_for((200, ACCOUNT))
    assert module.main(config=config, session=session) == 0
    assert "mirror_test" in capsys.readouterr().out
    assert len(adapter.calls) == 1


def test_cli_provider_failure_is_redacted_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    module = load_module("app.threads_setup")
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW)
    config = config_for(tmp_path)
    session, adapter = session_for(requests.Timeout("https://private.example/?access_token=imported-test-token"))
    assert module.main(config=config, session=session) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err
    assert "https://" not in output.err
    assert "imported-test-token" not in output.err


@pytest.mark.parametrize("metadata", [
    {"token_type": None}, {"token_type": {"echo": "provider-secret"}},
    {"expires_in": 10 ** 400},
])
def test_malformed_token_metadata_fails_with_safe_auth_error(tmp_path, monkeypatch, metadata):
    response = {"access_token": "renewable-test-token", "token_type": "bearer", "expires_in": LIFETIME}
    response.update(metadata)
    client, adapter, config = make_client(tmp_path, monkeypatch, (200, response),
        THREADS_TOKEN_EXPIRES_AT=None, THREADS_APP_SECRET="fake-app-secret")
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError) as caught:
        client.account_id
    assert "provider-secret" not in str(caught.value)
    assert len(adapter.calls) == 1
    assert not Path(config.THREADS_TOKEN_FILE).exists()


def test_credentials_expiring_during_verification_are_never_used_or_saved(tmp_path, monkeypatch):
    auth = load_module("app.core.threads_auth")
    def slow_verification(request, options):
        monkeypatch.setattr(auth.time, "time", lambda: NOW + 2)
        return 200, ACCOUNT
    client, adapter, config = make_client(tmp_path, monkeypatch, slow_verification, (200, {"id": "40001"}),
                                        THREADS_TOKEN_EXPIRES_AT=NOW + 1)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.create_container({"media_type": "TEXT", "text": "Never send expired credentials"})
    assert len(adapter.calls) == 1
    assert not Path(config.THREADS_TOKEN_FILE).exists()


def test_future_issued_metadata_fails_closed_before_verification(tmp_path, monkeypatch):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    saved = read_token(config)
    saved.update(issued_at=NOW + 10, refresh_at=NOW + 100, expires_at=NOW + 1000)
    path.write_text(json.dumps(saved))
    session, adapter = session_for((200, ACCOUNT))
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError):
        client.account_id
    assert adapter.calls == []


@pytest.mark.parametrize("transport_failure", [False, True])
def test_refresh_query_token_is_redacted_from_provider_and_transport_errors(tmp_path, monkeypatch, transport_failure):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    before = Path(config.THREADS_TOKEN_FILE).read_bytes()
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + LIFETIME - 60)
    def echo_request(request, options):
        if transport_failure:
            raise requests.Timeout(f"Lost response for {request.url}")
        return 400, {"error": {"message": f"Rejected {request.url}"}}
    session, adapter = session_for(echo_request)
    client = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    with pytest.raises(load_module("app.clients.threads").ThreadsAPIError) as caught:
        client.account_id
    rendered = repr(caught.value) + str(vars(caught.value)) + "".join(traceback.format_exception(caught.value))
    assert "https://" not in rendered
    assert "renewable-test-token" not in rendered
    assert Path(config.THREADS_TOKEN_FILE).read_bytes() == before
    assert len(adapter.calls) == 1


def fresh_prompts(monkeypatch):
    module = load_module("app.threads_setup")
    prompts = []
    answers = iter(["fresh-short-test-token", "fresh-test-app-secret"])
    def hidden(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr(module.getpass, "getpass", hidden)
    return module, prompts


@pytest.mark.parametrize("bootstrap_token", ["bootstrap-test-token", "changed-env-test-token", None])
def test_reconnect_verifies_fresh_credentials_before_atomic_replacement(tmp_path, monkeypatch, capsys, bootstrap_token):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    before, inode = path.read_bytes(), path.stat().st_ino
    config.THREADS_ENABLED = False
    config.THREADS_USER_ID = None  # The old private identity must supply the pin.
    config.THREADS_ACCESS_TOKEN = bootstrap_token
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + LIFETIME + 1)
    module, prompts = fresh_prompts(monkeypatch)

    def exchange(request, options):
        assert path.read_bytes() == before
        assert urlsplit(request.url).path == "/access_token"
        assert parse_qs(urlsplit(request.url).query) == {
            "grant_type": ["th_exchange_token"], "access_token": ["fresh-short-test-token"],
            "client_secret": ["fresh-test-app-secret"],
        }
        return 200, {"access_token": "fresh-long-test-token", "token_type": "bearer", "expires_in": LIFETIME}

    def verify(request, options):
        assert path.read_bytes() == before
        assert urlsplit(request.url).path == "/me"
        assert request.headers["Authorization"] == "Bearer fresh-long-test-token"
        return 200, ACCOUNT

    session, adapter = session_for(exchange, verify)
    assert module.main(config=config, session=session, reconnect=True) == 0
    assert len(prompts) == 2
    assert len(adapter.calls) == 2
    assert all(request.method == "GET" for request, _ in adapter.calls)
    saved = read_token(config)
    assert saved["account_id"] == "10001"
    assert saved["access_token"] == "fresh-long-test-token"
    assert saved["expires_at"] == NOW + 2 * LIFETIME + 1
    assert path.stat().st_ino != inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]
    assert "fresh-short-test-token" not in path.read_text()
    assert "fresh-test-app-secret" not in path.read_text()
    output = capsys.readouterr()
    assert "mirror_test" in output.out
    assert "10001" in output.out
    assert "fresh-" not in output.out + output.err
    assert output.err == ""
    assert config.THREADS_ACCESS_TOKEN == bootstrap_token
    # A private prompt must not require editing the existing private env file.
    session, _ = session_for((200, ACCOUNT))
    restarted = load_module("app.clients.threads").ThreadsClient(config=config, session=session)
    assert restarted.account_id == "10001"


def test_reconnect_cannot_switch_account_and_keeps_old_credentials(tmp_path, monkeypatch, capsys):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    config.THREADS_USER_ID = None
    before = Path(config.THREADS_TOKEN_FILE).read_bytes()
    module, _ = fresh_prompts(monkeypatch)
    session, adapter = session_for(
        (200, {"access_token": "wrong-account-test-token", "token_type": "bearer", "expires_in": LIFETIME}),
        (200, {"id": "90009", "username": "another_test"}),
    )
    assert module.main(config=config, session=session, reconnect=True) == 1
    assert Path(config.THREADS_TOKEN_FILE).read_bytes() == before
    assert len(adapter.calls) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err
    assert "fresh-" not in output.err
    assert "wrong-account-test-token" not in output.err


@pytest.mark.parametrize("malformed", ["json", "expiry"])
def test_reconnect_requires_explicit_account_for_malformed_metadata(tmp_path, monkeypatch, capsys, malformed):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    config.THREADS_USER_ID = None
    path = Path(config.THREADS_TOKEN_FILE)
    if malformed == "json":
        path.write_text("invalid JSON token=old-private-test-secret")
    else:
        saved = read_token(config)
        saved["expires_at"] = "invalid expiry"
        path.write_text(json.dumps(saved))
    before = path.read_bytes()
    module, _ = fresh_prompts(monkeypatch)
    session, adapter = session_for()
    assert module.main(config=config, session=session, reconnect=True) == 1
    assert path.read_bytes() == before
    assert adapter.calls == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "THREADS_USER_ID" in output.err
    assert "old-private-test-secret" not in output.err


def test_reconnect_can_replace_malformed_metadata_with_explicit_account(tmp_path, monkeypatch, capsys):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    path.write_text("invalid JSON token=old-private-test-secret")
    before = path.read_bytes()
    module, _ = fresh_prompts(monkeypatch)
    def verify(request, options):
        assert path.read_bytes() == before
        return 200, ACCOUNT
    session, adapter = session_for(
        (200, {"access_token": "fresh-long-test-token", "token_type": "bearer", "expires_in": LIFETIME}), verify,
    )
    assert module.main(config=config, session=session, reconnect=True) == 0
    assert read_token(config)["account_id"] == "10001"
    assert read_token(config)["access_token"] == "fresh-long-test-token"
    assert len(adapter.calls) == 2
    assert "old-private-test-secret" not in capsys.readouterr().out


@pytest.mark.parametrize("malformed", [False, True])
def test_reconnect_rejects_configured_account_change_when_previous_id_is_recoverable(tmp_path, monkeypatch, capsys, malformed):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    if malformed:
        saved = read_token(config)
        saved["expires_at"] = "invalid expiry"
        path.write_text(json.dumps(saved))
    before = path.read_bytes()
    config.THREADS_USER_ID = "90009"
    module, _ = fresh_prompts(monkeypatch)
    session, adapter = session_for()
    assert module.main(config=config, session=session, reconnect=True) == 1
    assert adapter.calls == []
    assert path.read_bytes() == before
    assert capsys.readouterr().out == ""


def test_reconnect_exchange_failure_preserves_previous_file_and_redacts_secrets(tmp_path, monkeypatch, capsys):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    before = Path(config.THREADS_TOKEN_FILE).read_bytes()
    module, _ = fresh_prompts(monkeypatch)
    def rejected(request, options):
        return 400, {"error": {"message": f"Rejected {request.url}"}}
    session, adapter = session_for(rejected)
    assert module.main(config=config, session=session, reconnect=True) == 1
    assert Path(config.THREADS_TOKEN_FILE).read_bytes() == before
    assert len(adapter.calls) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "https://" not in output.err
    assert "fresh-" not in output.err


@pytest.mark.parametrize("username", ["mirror_test", "renamed_test"])
def test_default_setup_verifies_existing_valid_file_without_refresh_or_writes(tmp_path, monkeypatch, capsys, username):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    before, metadata = path.read_bytes(), path.stat()
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + LIFETIME - 60)
    module = load_module("app.threads_setup")
    def no_prompt(_):
        pytest.fail("Default setup must not request replacement credentials")
    monkeypatch.setattr(module.getpass, "getpass", no_prompt)
    session, adapter = session_for((200, {"id": "10001", "username": username}))
    assert module.main(config=config, session=session) == 0
    assert path.read_bytes() == before
    assert path.stat().st_ino == metadata.st_ino
    assert path.stat().st_mtime_ns == metadata.st_mtime_ns
    assert [urlsplit(request.url).path for request, _ in adapter.calls] == ["/me"]
    assert username in capsys.readouterr().out


def test_default_setup_on_expired_file_explains_explicit_reconnect_flag(tmp_path, monkeypatch, capsys):
    _, _, config = bootstrap(tmp_path, monkeypatch)
    path = Path(config.THREADS_TOKEN_FILE)
    before = path.read_bytes()
    monkeypatch.setattr(load_module("app.core.threads_auth").time, "time", lambda: NOW + LIFETIME + 1)
    module = load_module("app.threads_setup")
    session, adapter = session_for()
    assert module.main(config=config, session=session) == 1
    assert path.read_bytes() == before
    assert adapter.calls == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "python -m app.threads_setup --reconnect" in output.err
