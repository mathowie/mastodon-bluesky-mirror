"""Durable, independent Threads container creation and publication."""
import ipaddress
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests
from mastodon import MastodonNetworkError, MastodonRatelimitError, MastodonServerError

from ..config import settings
from ..database import SettingsStore, ThreadsSyncMap, ThreadsSyncPart, get_threads_started_at
from .errors import describe_error
from .syncer import source_fingerprint
from .transformer import ContentTransformer

logger = logging.getLogger(__name__)
MAX_TRANSIENT_ATTEMPTS = 3


class ThreadsReviewRequired(ValueError):
    """The saved plan or outcome cannot safely be changed automatically."""


class ExpiredDraft(RuntimeError):
    transient = True
    retry_after = 0


class ThreadsOrchestrator:
    def __init__(self, db, masto, client=None):
        self.db = db
        self.masto = masto
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from ..clients.threads import ThreadsClient
            self._client = ThreadsClient()
        return self._client

    def _credentials_deadline(self):
        row = self.db.get(SettingsStore, "threads_credentials_retry_at")
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row.value)
        except (TypeError, ValueError):
            return datetime.max  # Invalid saved retry metadata fails closed.

    def _defer_credentials(self, exc):
        counter = self.db.get(SettingsStore, "threads_credentials_attempts")
        attempts = min(6, int(counter.value) + 1) if counter and counter.value.isdigit() else 1
        delay = max(min(3600, 60 * 2 ** attempts), float(getattr(exc, "retry_after", 0) or 0))
        try:
            deadline = datetime.utcnow() + timedelta(seconds=delay)
        except (OverflowError, ValueError):
            deadline = datetime.max
        previous = self._credentials_deadline()
        if previous:
            deadline = max(deadline, previous)
        for key, value in {
            "threads_credentials_attempts": str(attempts),
            "threads_credentials_retry_at": deadline.isoformat(),
            "threads_credentials_error": describe_error(exc),
        }.items():
            row = self.db.get(SettingsStore, key)
            if row is None:
                self.db.add(SettingsStore(key=key, value=value))
            else:
                row.value = value
        self.db.commit()

    def maintain_credentials(self):
        deadline = self._credentials_deadline()
        if deadline and deadline > datetime.utcnow():
            return
        try:
            self.client.maintain_credentials()
        except Exception as exc:
            self._defer_credentials(exc)
            logger.error("Threads credential maintenance failed: %s", describe_error(exc))
        else:
            for row in self.db.query(SettingsStore).filter(SettingsStore.key.in_([
                    "threads_credentials_attempts", "threads_credentials_retry_at", "threads_credentials_error"])):
                self.db.delete(row)
            self.db.commit()

    def _skip(self, ident, reason):
        row = self.db.get(ThreadsSyncMap, ident)
        if row and row.status in {"pending", "queued"}:
            row.status, row.error_message = "skipped", reason
            self.db.commit()
        logger.info("Skipping Threads source %s: %s", ident, reason)
        return "skipped"

    def _wait(self, row, reason):
        row.status, row.error_message = "pending", reason
        row.synced_at = datetime.utcnow()
        self.db.commit()
        return "pending"

    @staticmethod
    def _images(post):
        images = []
        for media in post.get("media_attachments", []):
            if media.get("type") != "image":
                continue
            url = media.get("url")
            if not isinstance(url, str):
                raise ValueError("Image attachment has no public URL")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Threads images require a public HTTPS URL")
            if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith(".local"):
                raise ValueError("Threads cannot fetch images from a private host")
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None
            if address and not address.is_global:
                raise ValueError("Threads cannot fetch images from a private address")
            images.append({"url": url, "alt": media.get("description") or ""})
        if len(images) > 4:
            raise ValueError("This mirror supports at most four images per source post")
        return images

    def _reply_parent(self, post, account_id):
        parent_id = str(post["in_reply_to_id"]) if post.get("in_reply_to_id") else None
        if not parent_id:
            return None, False
        source = self.masto.get_status(parent_id)
        if source.get("visibility") != "public":
            return None, True
        if (str(source.get("id")) != parent_id or str((source.get("account") or {}).get("id"))
                != str(post.get("in_reply_to_account_id"))):
            raise ThreadsReviewRequired("Source reply parent identity changed")
        mapped = self.db.get(ThreadsSyncMap, parent_id)
        if mapped is None or mapped.status != "success" or not mapped.threads_id:
            return None, False
        if mapped.account_id != account_id:
            raise ThreadsReviewRequired("The Threads reply parent belongs to another account")
        current = self.client.media_details(mapped.threads_id)
        if str(current.get("id")) != mapped.threads_id:
            raise ThreadsReviewRequired("The Threads reply parent is missing or changed")
        return mapped.threads_id, False

    def _ready(self, container_id):
        result = self.client.container_status(container_id)
        if str(result.get("id")) != container_id:
            raise ThreadsReviewRequired("Threads returned an unexpected media container")
        state = result.get("status")
        if state == "FINISHED":
            return True
        if state == "IN_PROGRESS":
            return False
        if state == "PUBLISHED":
            raise ThreadsReviewRequired("The media was published without a saved post ID; review Threads before retrying")
        if state == "EXPIRED":
            raise ExpiredDraft("An unpublished Threads media container expired")
        if state == "ERROR":
            raise ValueError("Threads media processing failed; verify the attachment's public URL and supported format")
        raise ValueError("Threads returned an unknown media processing state")

    def _container(self, part):
        if part.container_id:
            return self._ready(part.container_id)
        payload = json.loads(part.payload_json)
        images = payload.pop("images", [])
        if len(images) == 1:
            payload.update(media_type="IMAGE", image_url=images[0]["url"], alt_text=images[0]["alt"])
        elif images:
            children = json.loads(part.children_json)
            if len(children) > len(images):
                raise ThreadsReviewRequired("Saved carousel children do not match the source")
            for image in images[len(children):]:
                children.append(self.client.create_container({
                    "media_type": "IMAGE", "image_url": image["url"],
                    "alt_text": image["alt"], "is_carousel_item": "true",
                }))
                part.children_json = json.dumps(children)
                self.db.commit()
            # Inspect every child before creating its parent carousel.
            ready = [self._ready(child) for child in children]
            if not all(ready):
                return False
            payload.update(media_type="CAROUSEL", children=",".join(children))
        else:
            payload["media_type"] = "TEXT"
        part.container_id = self.client.create_container(payload)
        part.state = "ready"
        self.db.commit()
        return self._ready(part.container_id)

    def _publish(self, part):
        # The intent must survive a crash both before and after the POST.
        part.state = "publishing"
        self.db.commit()
        try:
            published_id = self.client.publish_container(part.container_id)
        except Exception as exc:
            if getattr(exc, "http_status", None) == 429:
                # An explicit rate-limit rejection can be retried, using the
                # same container only, after the provider's delay.
                part.state = "ready"
                self.db.commit()
                raise
            raise ThreadsReviewRequired(
                "Publication may have succeeded; review Threads before retrying this saved container"
            ) from None
        if not published_id or not str(published_id).isdigit():
            raise ThreadsReviewRequired("Publication returned no valid post ID; review Threads before retrying")
        part.threads_id, part.state = str(published_id), "published"
        self.db.commit()
        # A missing permalink must not turn an acknowledged post into a retry.
        try:
            details = self.client.media_details(part.threads_id)
            link = details.get("permalink")
            if isinstance(link, str) and link.startswith(("https://www.threads.net/", "https://www.threads.com/")):
                part.permalink = link
                self.db.commit()
        except Exception:
            logger.warning("Threads post ID saved; its permalink could not be retrieved")

    def record_failure(self, ident, exc):
        self.db.rollback()
        row = self.db.get(ThreadsSyncMap, ident)
        row.synced_at = datetime.utcnow()
        row.error_message = describe_error(exc)
        if isinstance(exc, ThreadsReviewRequired):
            row.status = "needs_review"
        else:
            transient = getattr(exc, "transient", False) or isinstance(
                exc, (requests.RequestException, MastodonNetworkError, MastodonRatelimitError, MastodonServerError)
            )
            row.attempt_count += 1
            if transient:
                delay = max(15 * 2 ** row.attempt_count, float(getattr(exc, "retry_after", 0) or 0))
                try:
                    retry_at = datetime.utcnow() + timedelta(seconds=delay)
                    row.next_attempt_at = max(row.next_attempt_at or retry_at, retry_at)
                    row.status = "queued" if row.attempt_count < MAX_TRANSIENT_ATTEMPTS else "failed"
                except (OverflowError, ValueError):
                    row.status = "failed"
                    row.error_message += "; provider retry delay requires manual review"
            else:
                row.status = "failed"
        self.db.commit()
        logger.error("Threads source %s: %s", ident, row.error_message)
        return "pending" if row.status == "queued" else "failed"

    def process_post(self, post):
        ident = str(post.get("id", ""))
        if not ident.isdigit() or len(ident) > 30:
            return "skipped"
        if post.get("visibility") != "public":
            return self._skip(ident, "Source is not public")
        created = post.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                created = None
        if not isinstance(created, datetime):
            return self._skip(ident, "Source has no valid creation time")
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < get_threads_started_at(self.db):
            return self._skip(ident, "Source predates Threads activation")
        if post.get("reblog"):
            return self._skip(ident, "Boost publishing is not supported")
        if post.get("spoiler_text") and not settings.MIRROR_CONTENT_WARNINGS:
            return self._skip(ident, "Content-warning publishing is disabled")
        source_parent = post.get("in_reply_to_id")
        own_account = str((post.get("account") or {}).get("id") or "")
        self_reply = bool(source_parent and own_account and str(post.get("in_reply_to_account_id")) == own_account)
        if source_parent and not (settings.MIRROR_REPLIES or (self_reply and settings.MIRROR_SELF_REPLIES)):
            return self._skip(ident, "Reply publishing is disabled for this source")
        row = self.db.get(ThreadsSyncMap, ident)
        if row and row.status in {"success", "skipped"}:
            return "existing"
        if row and row.status in {"failed", "needs_review"}:
            return "failed"
        if row and row.next_attempt_at and row.next_attempt_at > datetime.utcnow():
            return "pending"
        if row is None:
            row = ThreadsSyncMap(mastodon_id=ident, status="pending")
            self.db.add(row)
        row.status, row.next_attempt_at = "pending", None
        self.db.commit()
        authorized = False
        try:
            deadline = self._credentials_deadline()
            if deadline and deadline > datetime.utcnow():
                row.status, row.next_attempt_at = "queued", deadline
                row.error_message = "Waiting for Threads authorization retry"
                self.db.commit()
                return "pending"
            account_id = str(self.client.account_id)
            authorized = True
            if row.account_id and row.account_id != account_id:
                raise ThreadsReviewRequired("The saved job belongs to a different Threads account")
            row.account_id = account_id
            parent_id, private_parent = self._reply_parent(post, account_id)
            if private_parent:
                return self._skip(ident, "Source reply parent is not public")
            if source_parent and not parent_id:
                return self._wait(row, "Waiting for the source parent to finish mirroring to Threads")
            parts = self.db.query(ThreadsSyncPart).filter_by(mastodon_id=ident).order_by(ThreadsSyncPart.part_index).all()
            fingerprint = source_fingerprint(post)
            if not parts:
                if row.threads_id:
                    raise ThreadsReviewRequired("A published Threads post has no saved part plan")
                text = ContentTransformer.clean_text(post.get("content", ""))
                if post.get("spoiler_text"):
                    text = f"CW: {post['spoiler_text']}\n\n{text}".strip()
                images = self._images(post)
                if not text and not images:
                    return self._skip(ident, "Source contains no supported text or images")
                for index, chunk in enumerate(ContentTransformer.split_into_threads(text, max_length=500)):
                    payload = {"text": chunk, "images": images if index == 0 else []}
                    if index == 0 and parent_id:
                        payload["reply_to_id"] = parent_id
                    part = ThreadsSyncPart(mastodon_id=ident, part_index=index, account_id=account_id,
                                           payload_json=json.dumps(payload), source_fingerprint=fingerprint)
                    parts.append(part)
                    self.db.add(part)
                self.db.commit()
            elif any(part.source_fingerprint != fingerprint or part.account_id != account_id for part in parts):
                raise ThreadsReviewRequired("Source or Threads account changed after planning")
            for index, part in enumerate(parts):
                if part.part_index != index:
                    raise ThreadsReviewRequired("The saved Threads plan has missing or unordered parts")
                payload = json.loads(part.payload_json)
                if part.threads_id:
                    remote = self.client.media_details(part.threads_id)
                    if str(remote.get("id")) != part.threads_id or (remote.get("text") or "") != payload["text"]:
                        raise ThreadsReviewRequired("An already-published Threads part is missing or changed")
                    parent_id = part.threads_id
                    continue
                if part.state in {"publishing", "published"}:
                    # A post ID lost between the remote commit and SQLite commit
                    # cannot be reconstructed by guessing or comparing text.
                    try:
                        state = self.client.container_status(part.container_id).get("status")
                    except Exception:
                        state = "unknown"
                    if state not in {"FINISHED", "IN_PROGRESS", "PUBLISHED", "ERROR", "EXPIRED"}:
                        state = "unknown"
                    raise ThreadsReviewRequired(
                        f"Publication was interrupted (container status {state}); review Threads before retrying"
                    )
                if payload.get("reply_to_id") != parent_id:
                    if part.container_id or index == 0:
                        raise ThreadsReviewRequired("Saved Threads reply context changed")
                    payload["reply_to_id"] = parent_id
                    part.payload_json = json.dumps(payload)
                    self.db.commit()
                try:
                    ready = self._container(part)
                except ExpiredDraft:
                    part.container_id, part.children_json, part.state = None, "[]", "planned"
                    self.db.commit()
                    raise
                if not ready:
                    return self._wait(row, "Waiting for Threads media processing")
                self._publish(part)
                row.threads_id, row.permalink = part.threads_id, part.permalink
                self.db.commit()
                parent_id = part.threads_id
            # Rebuild the aggregate checkpoint even if a previous process died
            # just after committing the final part's post ID.
            row.threads_id, row.permalink = parts[-1].threads_id, parts[-1].permalink
            row.status, row.error_message = "success", None
            row.synced_at, row.next_attempt_at = datetime.utcnow(), None
            self.db.commit()
            logger.info("Mirrored Mastodon source %s to Threads (%d part(s))", ident, len(parts))
            return "synced"
        except Exception as exc:
            if not authorized:
                self.db.rollback()
                self._defer_credentials(exc)
            return self.record_failure(ident, exc)
