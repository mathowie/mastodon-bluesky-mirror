"""Verified Threads credentials, private persistence, and safe HTTP failures."""
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests


BASE_URL = "https://graph.threads.net"
TIMEOUT = (5, 20)
_REFRESH_WINDOW = 7 * 24 * 60 * 60  # Maximum lead; actual lead is 10% of the lifetime if shorter.


class ThreadsAPIError(RuntimeError):
    """A safe description with scheduling metadata, never a provider response."""

    def __init__(self, message="Threads API request failed", *, http_status=None,
                 retry_after=0, transient=False, uncertain=False):
        super().__init__(message)
        self.http_status = http_status
        self.retry_after = retry_after
        self.transient = transient
        self.uncertain = uncertain


class ThreadsTransportError(ThreadsAPIError):
    """The remote outcome is unknown; publication must be reconciled, not resent."""

    def __init__(self):
        super().__init__("Threads response was not confirmed; remote outcome is uncertain",
                         transient=True, uncertain=True)


class ThreadsAuthError(ThreadsAPIError):
    pass


def _retry_after(value):
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, AttributeError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


def request_json(session, method, path, *, token=None, params=None, data=None):
    """One attempt only. Suppress raw request URLs and untrusted error bodies."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = session.request(method, BASE_URL + path, headers=headers,
                                   params=params, data=data, timeout=TIMEOUT,
                                   allow_redirects=False)
    except requests.RequestException:
        raise ThreadsTransportError() from None
    status = response.status_code
    if not 200 <= status < 300:
        raise ThreadsAPIError(
            f"Threads API request failed (HTTP {status})",
            http_status=status, retry_after=_retry_after(response.headers.get("Retry-After")),
            transient=status == 429 or status >= 500,
            uncertain=method == "POST" and status >= 500,
        )
    try:
        body = response.json()
    except (ValueError, requests.RequestException):
        if method == "POST":
            raise ThreadsTransportError() from None
        raise ThreadsAPIError("Threads returned an invalid response", http_status=status) from None
    if not isinstance(body, dict):
        if method == "POST":
            raise ThreadsTransportError()
        raise ThreadsAPIError("Threads returned an invalid response", http_status=status)
    if "error" in body:
        error = body["error"]
        raise ThreadsAPIError("Threads API rejected the request", http_status=status,
                              transient=isinstance(error, dict) and error.get("is_transient") is True)
    return body


def identifier(value):
    """IDs enter URL paths; reject URLs and query strings before any HTTP call."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,64}", value):
        raise ThreadsAPIError("Invalid Threads identifier")
    return value


def _secret(value):
    return (isinstance(value, str) and 0 < len(value) <= 8192
            and all(32 < ord(char) < 127 for char in value))


def _timestamp(value):
    return (type(value) in (int, float) and 0 < value < 253402300800
            and math.isfinite(value))


def _fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class _Credentials:
    access_token: str = field(repr=False)
    account_id: str
    username: str
    expires_at: float
    issued_at: float
    refresh_at: float
    bootstrap_fingerprint: str = field(repr=False)
    version: int = 1


class ThreadsAuth:
    """Authorize on demand, pin identity, and save every renewal before use.

    The bootstrap fingerprint refers to the original environment token, not
    the rotating access token. Restarts therefore do not undo a renewal.
    THREADS_ENABLED is enforced by the worker; setup is allowed while disabled.
    """

    def __init__(self, config=None, session=None):
        if config is None:
            from ..config import settings
            config = settings
        self.config = config
        self.session = session if session is not None else requests.Session()
        self.path = Path(config.THREADS_TOKEN_FILE)
        self._verified = None
        self._lock = threading.RLock()

    def _read_json(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise ThreadsAuthError("Threads private credential file cannot be read safely") from None
        try:
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ThreadsAuthError("Threads credential file must be a private regular file with mode 0600")
                raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ThreadsAuthError("Threads private credential metadata is invalid")
                saved = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            raise ThreadsAuthError("Threads private credential metadata is invalid") from None
        if not isinstance(saved, dict):
            raise ThreadsAuthError("Threads private credential metadata is invalid")
        return saved

    def _read(self, *, check_config=True):
        saved = self._read_json()
        if saved is None:
            return None
        fields = set(_Credentials.__dataclass_fields__)
        if set(saved) != fields:
            raise ThreadsAuthError("Threads private credential metadata is invalid")
        if (type(saved["version"]) is not int or saved["version"] != 1
                or not _secret(saved["access_token"])
                or not isinstance(saved["account_id"], str)
                or not re.fullmatch(r"[0-9]{1,64}", saved["account_id"])
                or not isinstance(saved["username"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.]{1,64}", saved["username"])
                or not all(_timestamp(saved[key]) for key in ("expires_at", "issued_at", "refresh_at"))
                or not saved["issued_at"] < saved["refresh_at"] < saved["expires_at"]
                or saved["issued_at"] > time.time()
                or not isinstance(saved["bootstrap_fingerprint"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", saved["bootstrap_fingerprint"])):
            raise ThreadsAuthError("Threads private credential metadata is invalid")
        record = _Credentials(**saved)
        if not check_config:
            return record
        bootstrap = self.config.THREADS_ACCESS_TOKEN
        if bootstrap is not None and (not _secret(bootstrap) or _fingerprint(bootstrap) != record.bootstrap_fingerprint):
            raise ThreadsAuthError("Threads bootstrap credentials changed; review account setup")
        expected = self.config.THREADS_USER_ID
        if expected is not None and expected != record.account_id:
            raise ThreadsAuthError("Threads configured account differs from the pinned account")
        if self._verified is not None and self._verified.account_id != record.account_id:
            raise ThreadsAuthError("Threads pinned account changed; review account setup")
        return record

    def _save(self, record):
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(asdict(record), stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError):
            raise ThreadsAuthError("Threads credentials could not be saved privately") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _verify(self, token, expected):
        me = request_json(self.session, "GET", "/me", token=token, params={"fields": "id,username"})
        account_id, username = me.get("id"), me.get("username")
        if (not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{1,64}", account_id)
                or not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,64}", username)):
            raise ThreadsAuthError("Threads account verification returned invalid metadata")
        if expected is not None and account_id != expected:
            raise ThreadsAuthError("Threads verified account differs from the pinned account")
        return account_id, username

    @staticmethod
    def _token_response(body):
        token, lifetime = body.get("access_token"), body.get("expires_in")
        token_type = body.get("token_type")
        if (not _secret(token) or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or not _timestamp(lifetime)):
            raise ThreadsAuthError("Threads token response contains invalid metadata")
        return token, lifetime

    def _new_record(self, token, expires_at, issued_at, fingerprint, expected):
        if not _timestamp(expires_at) or expires_at <= issued_at:
            raise ThreadsAuthError("Threads credentials have expired or have invalid expiry metadata")
        account_id, username = self._verify(token, expected)
        if expires_at <= time.time():
            raise ThreadsAuthError("Threads credentials expired during verification; run Threads setup")
        # Refresh in the final 10% of the lifetime, capped at seven days before
        # expiry: six days before expiry for a 60-day token. Persist the schedule.
        refresh_at = expires_at - min(_REFRESH_WINDOW, (expires_at - issued_at) / 10)
        return _Credentials(token, account_id, username, expires_at, issued_at, refresh_at, fingerprint)

    def _bootstrap(self):
        token = self.config.THREADS_ACCESS_TOKEN
        if not _secret(token):
            raise ThreadsAuthError("Threads access token is required; run Threads setup")
        fingerprint = _fingerprint(token)
        now = time.time()
        expiry = self.config.THREADS_TOKEN_EXPIRES_AT
        if expiry is None:
            secret = self.config.THREADS_APP_SECRET
            if not _secret(secret):
                raise ThreadsAuthError("Threads app secret or explicit long-lived token expiry is required")
            response = request_json(self.session, "GET", "/access_token", params={
                "grant_type": "th_exchange_token", "client_secret": secret, "access_token": token,
            })
            token, lifetime = self._token_response(response)
            expiry = now + lifetime
        record = self._new_record(token, expiry, now, fingerprint, self.config.THREADS_USER_ID)
        self._save(record)
        return record

    def _reconnect_account_id(self):
        configured = self.config.THREADS_USER_ID
        if configured is not None:
            identifier(configured)
        try:
            previous = self._read(check_config=False)
        except ThreadsAuthError:
            if configured is None:
                raise ThreadsAuthError("Set THREADS_USER_ID before reconnecting with malformed private metadata") from None
            # Even malformed metadata may still contain a recoverable identity.
            # Do not let a changed configuration override that previous ID.
            try:
                saved = self._read_json()
            except ThreadsAuthError:
                saved = None
            previous_id = saved.get("account_id") if saved is not None else None
            if not isinstance(previous_id, str) or not re.fullmatch(r"[0-9]{1,64}", previous_id):
                previous_id = None
        else:
            previous_id = previous.account_id if previous is not None else None
        pinned = previous_id or configured
        if pinned is None:
            raise ThreadsAuthError("Set THREADS_USER_ID before reconnecting without a recoverable private identity")
        if configured is not None and configured != pinned:
            raise ThreadsAuthError("Threads reconnect cannot change the pinned account")
        if self._verified is not None and self._verified.account_id != pinned:
            raise ThreadsAuthError("Threads reconnect cannot change the pinned account")
        return pinned

    def reconnect(self, token, app_secret):
        """Explicitly replace credentials for the same identity, after verification.

        Fresh prompt values are used once for exchange. Keep the fingerprint
        anchored to the current private environment, when present, so that an
        unchanged expired environment token does not undo recovery on restart.
        """
        with self._lock:
            pinned = self._reconnect_account_id()
            if not _secret(token) or not _secret(app_secret):
                raise ThreadsAuthError("Fresh Threads access token and app secret are required for reconnect")
            anchor = self.config.THREADS_ACCESS_TOKEN
            if anchor is not None and not _secret(anchor):
                raise ThreadsAuthError("Threads environment bootstrap token is invalid")
            now = time.time()
            response = request_json(self.session, "GET", "/access_token", params={
                "grant_type": "th_exchange_token", "client_secret": app_secret, "access_token": token,
            })
            renewed, lifetime = self._token_response(response)
            record = self._new_record(renewed, now + lifetime, now, _fingerprint(anchor or token), pinned)
            if self._reconnect_account_id() != pinned:
                raise ThreadsAuthError("Threads private identity changed during reconnect")
            self._save(record)
            self._verified = record
            return record

    def verify_existing(self):
        """Verify existing credentials for setup, without renewal or file writes."""
        with self._lock:
            record = self._read()
            if record is None or record.expires_at <= time.time():
                raise ThreadsAuthError("Threads credentials are missing or expired")
            _, username = self._verify(record.access_token, record.account_id)
            if record.expires_at <= time.time():
                raise ThreadsAuthError("Threads credentials expired during verification")
            return replace(record, username=username)

    def authorize(self):
        """Return usable verified credentials, or fail before any publication."""
        with self._lock:
            record = self._read()
            if record is None:
                if self._verified is not None:
                    raise ThreadsAuthError("Threads private credential file disappeared; review account setup")
                record = self._bootstrap()
            elif record.expires_at <= time.time():
                raise ThreadsAuthError("Threads credentials have expired; run Threads setup")
            elif record.refresh_at <= time.time():
                now = time.time()
                response = request_json(self.session, "GET", "/refresh_access_token",
                                        params={"grant_type": "th_refresh_token", "access_token": record.access_token})
                token, lifetime = self._token_response(response)
                record = self._new_record(token, now + lifetime, now, record.bootstrap_fingerprint, record.account_id)
                self._save(record)
            elif self._verified is None or record.access_token != self._verified.access_token:
                _, username = self._verify(record.access_token, record.account_id)
                if username != record.username:
                    record = replace(record, username=username)
                    self._save(record)
            if record.expires_at <= time.time():
                raise ThreadsAuthError("Threads credentials have expired; run Threads setup")
            self._verified = record
            return record

    def connection_status(self):
        """Read local metadata only: no verification, refresh, writes, or secrets."""
        status = {"configured": bool(self.config.THREADS_ACCESS_TOKEN), "account_id": None,
                  "username": None, "expires_at": None, "status": "not_configured", "error": None}
        try:
            record = self._read()
        except ThreadsAPIError as exc:
            status.update(configured=True, status="invalid", error=str(exc))
            return status
        if record is not None:
            status.update(configured=True, account_id=record.account_id, username=record.username,
                          expires_at=record.expires_at, status="ready")
            if record.expires_at <= time.time():
                status.update(status="expired", error="Threads credentials have expired; run Threads setup")
        elif status["configured"]:
            status["status"] = "setup_required"
        return status
