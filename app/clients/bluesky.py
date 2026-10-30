"""Supported SDK payloads and idempotent, bounded thread-part publication."""
import secrets
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from atproto import Client, models
from atproto.exceptions import RequestErrorBase, NetworkError, InvokeTimeoutError
from atproto_client.utils.facet_detection import detect_facets

from ..config import settings
from ..core.media import prepare_image

_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
_clock_id = secrets.randbits(10)
_last_micros = 0
_key_lock = threading.Lock()


def new_record_key():
    """Generate a monotonically increasing AT Protocol TID; persist before use."""
    global _last_micros
    with _key_lock:
        _last_micros = max(time.time_ns() // 1000, _last_micros + 1)
        value = (_last_micros << 10) | _clock_id
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(60, -1, -5))


class PublishedRecordMissing(RuntimeError):
    pass


class RecordConflict(RuntimeError):
    pass


class BlueskyClient:
    def __init__(self):
        self.client = Client()
        password = settings.BLUESKY_APP_PASSWORD or settings.BLUESKY_PASSWORD
        if not password:
            raise ValueError("Set BLUESKY_APP_PASSWORD before starting the mirror")
        self.client.login(settings.BLUESKY_HANDLE, password)

    @property
    def repo_did(self):
        if self.client.me is None:
            raise ValueError("Bluesky login is required")
        return self.client.me.did

    def upload_image_attachment(self, image_bytes, alt_text=None):
        prepared = prepare_image(image_bytes)
        blob = self.client.upload_blob(prepared.data).blob
        return models.AppBskyEmbedImages.Image(
            image=blob, alt=alt_text or "",
            aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(width=prepared.width, height=prepared.height),
        )

    def upload_image(self, image_bytes, alt_text=None):
        return self.upload_image_attachment(image_bytes, alt_text).image

    @staticmethod
    def build_record(text, images=None, parent_uri=None, parent_cid=None,
                     root_uri=None, root_cid=None, created_at=None):
        if images and len(images) > 4:
            raise ValueError("Bluesky supports at most four images per post")
        embed = None
        if images:
            embed = models.AppBskyEmbedImages.Main(images=[
                image if isinstance(image, models.AppBskyEmbedImages.Image)
                else models.AppBskyEmbedImages.Image(image=image[0], alt=image[1] or "")
                for image in images
            ])
        reply = None
        if parent_uri or parent_cid:
            if not (parent_uri and parent_cid):
                raise ValueError("A reply requires both the parent URI and CID")
            reply = models.AppBskyFeedPost.ReplyRef(
                parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
                root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri or parent_uri, cid=root_cid or parent_cid),
            )
        return models.AppBskyFeedPost.Record(
            text=text, embed=embed, reply=reply, facets=detect_facets(text), langs=["en"],
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
        )

    def post_text(self, text, images=None, parent_uri=None, parent_cid=None, root_uri=None, root_cid=None):
        record = self.build_record(text, images, parent_uri, parent_cid, root_uri, root_cid)
        response = self.client.send_post(text=text, embed=record.embed, reply_to=record.reply, facets=record.facets)
        return response.uri, response.cid

    def _read_record(self, repo_did, record_key):
        try:
            return self.client.app.bsky.feed.post.get(repo_did, record_key)
        except RequestErrorBase as exc:
            content = getattr(getattr(exc, "response", None), "content", None)
            code = content.get("error") if isinstance(content, dict) else getattr(content, "error", None)
            if code == "RecordNotFound":
                return None
            raise

    def get_reply_context(self, parent_uri, parent_cid):
        """Resolve a mapped parent's current reference and original thread root."""
        prefix = f"at://{self.repo_did}/app.bsky.feed.post/"

        def read_ref(uri, cid):
            if not uri or not uri.startswith(prefix) or not uri[len(prefix):] or "/" in uri[len(prefix):]:
                raise RecordConflict("The reply parent belongs to an unexpected repository")
            existing = self._read_record(self.repo_did, uri[len(prefix):])
            if existing is None:
                raise PublishedRecordMissing("The reply parent or thread root is missing on Bluesky")
            if existing.uri != uri or existing.cid != cid:
                raise RecordConflict("The reply parent or thread root changed; manual review is required")
            return models.AppBskyFeedPost.Record.model_validate(existing.value)

        parent = models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid)
        record = read_ref(parent_uri, parent_cid)
        root = record.reply.root if record.reply else parent
        if root != parent:
            root_record = read_ref(root.uri, root.cid)
            if root_record.reply is not None:
                raise RecordConflict("The saved thread root is not an original post")
        return models.AppBskyFeedPost.ReplyRef(root=root, parent=parent)

    @staticmethod
    def _retryable(exc):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            return status == 429 or status >= 500
        return isinstance(exc, (NetworkError, InvokeTimeoutError))

    @staticmethod
    def _retry_delay(exc, attempt):
        delay = 0.5 * (2 ** attempt)
        response = getattr(exc, "response", None)
        headers = {str(k).lower(): v for k, v in (getattr(response, "headers", None) or {}).items()}
        if "retry-after" in headers:
            try:
                requested = float(headers["retry-after"])
            except (TypeError, ValueError):
                try:
                    requested = parsedate_to_datetime(headers["retry-after"]).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    requested = 0
            delay = max(delay, requested)
        elif "ratelimit-reset" in headers or "x-ratelimit-reset" in headers:
            try:
                reset = headers.get("ratelimit-reset", headers.get("x-ratelimit-reset"))
                delay = max(delay, float(reset) - time.time())
            except (TypeError, ValueError):
                pass
        # A longer server delay is surfaced as a failure for a later manual retry;
        # never send early just to keep an inline retry short.
        return delay if delay <= 30 else None

    @staticmethod
    def _verify_record(existing, record, repo_did, record_key, known_uri=None, known_cid=None):
        expected_uri = f"at://{repo_did}/app.bsky.feed.post/{record_key}"
        if existing.uri != expected_uri or not existing.cid:
            raise RecordConflict("Bluesky returned an unexpected record reference")
        actual = models.AppBskyFeedPost.Record.model_validate(existing.value).model_dump(mode="json", by_alias=True, exclude_none=True)
        expected = record.model_dump(mode="json", by_alias=True, exclude_none=True)
        if actual != expected or (known_uri and existing.uri != known_uri) or (known_cid and existing.cid != known_cid):
            raise RecordConflict("The published post differs from the saved thread; manual review is required")
        return existing.uri, existing.cid

    def publish_record(self, record, record_key, repo_did, known_uri=None, known_cid=None):
        """Read/reconcile a saved key before each write, including every retry."""
        if repo_did != self.repo_did:
            raise RecordConflict("This thread was planned for a different Bluesky account")
        record = models.AppBskyFeedPost.Record.model_validate(record)
        for attempt in range(3):
            try:
                existing = self._read_record(repo_did, record_key)
                if existing is not None:
                    return self._verify_record(existing, record, repo_did, record_key, known_uri, known_cid)
                if known_uri or known_cid:
                    raise PublishedRecordMissing("An already-published thread part is missing; it will not be recreated automatically")
                try:
                    created = self.client.app.bsky.feed.post.create(repo_did, record, rkey=record_key)
                except Exception:
                    # A rejected/uncertain response can follow a successful write.
                    # Reconcile the same key before considering another attempt.
                    existing = self._read_record(repo_did, record_key)
                    if existing is not None:
                        return self._verify_record(existing, record, repo_did, record_key)
                    raise
                expected_uri = f"at://{repo_did}/app.bsky.feed.post/{record_key}"
                if created.uri != expected_uri or not created.cid:
                    raise RecordConflict("Bluesky returned an unexpected created record reference")
                return created.uri, created.cid
            except Exception as exc:
                if attempt == 2 or not self._retryable(exc):
                    raise
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    raise
                time.sleep(delay)
        raise RuntimeError("Publication attempts exhausted")
