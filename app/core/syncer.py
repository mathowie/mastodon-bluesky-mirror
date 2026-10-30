"""Poll, filter, checkpoint and resume Mastodon-to-Bluesky thread jobs."""
import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone

import requests
from atproto import models
from sqlalchemy.orm import Session

from .errors import describe_error
from .media import MAX_SOURCE_BYTES, validate_image_inventory
from .transformer import ContentTransformer
from ..clients.mastodon import MastodonClient
from ..clients.bluesky import BlueskyClient, new_record_key
from ..database import SyncMap, SyncPart, get_sync_started_at, get_self_replies_started_at
from ..config import settings

logger = logging.getLogger(__name__)


def source_fingerprint(post):
    source = {key: post.get(key) for key in ("content", "spoiler_text", "media_attachments")}
    if post.get("in_reply_to_id"):
        source.update({key: str(post.get(key)) if post.get(key) is not None else None
                       for key in ("in_reply_to_id", "in_reply_to_account_id")})
    return hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()


class SyncOrchestrator:
    def __init__(self, db: Session, masto=None, bsky=None):
        self.db = db
        self.masto = masto if masto is not None else MastodonClient()
        self.bsky = bsky if bsky is not None else BlueskyClient()
        self.transformer = ContentTransformer()

    def run_sync_cycle(self):
        logger.info("Starting sync cycle")
        get_sync_started_at(self.db)
        counts = Counter()
        jobs = [row.mastodon_id for row in self.db.query(SyncMap).filter(SyncMap.status.in_(["queued", "pending"])).all()]
        try:
            posts = self.masto.get_recent_posts(limit=20)
        except Exception as exc:
            logger.error("Mastodon poll failed: %s", describe_error(exc))
            posts = []
        by_id = {str(post["id"]): post for post in posts}
        for ident in jobs:
            if ident in by_id:
                continue
            try:
                by_id[ident] = self.masto.get_status(ident)
            except Exception as exc:
                row = self.db.get(SyncMap, ident)
                row.status = "failed"
                row.error_message = "Source refresh failed: " + describe_error(exc)
                self.db.commit()
                counts["failed"] += 1
        for post in sorted(by_id.values(), key=lambda item: int(item["id"])):
            try:
                counts[self.process_post(post)] += 1
            except Exception as exc:
                self.db.rollback()
                counts["failed"] += 1
                logger.error("Status processing failed: %s", describe_error(exc))
        logger.info("Sync complete: %d synced, %d already mirrored, %d skipped, %d waiting, %d failed",
                    counts["synced"], counts["existing"], counts["skipped"], counts["pending"], counts["failed"])

    def _skip(self, ident, reason):
        logger.info("Skipping Mastodon status %s: %s", ident, reason)
        row = self.db.get(SyncMap, ident)
        if row and row.status in {"queued", "pending"}:
            row.status = "skipped"
            row.error_message = reason
            self.db.commit()
        return "skipped"

    def _images(self, post):
        uploaded = []
        for url, alt in self.transformer.process_media(post):
            with requests.get(url, timeout=(5, 20), stream=True) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > MAX_SOURCE_BYTES:
                        raise ValueError("Image exceeds the download size limit")
            uploaded.append(self.bsky.upload_image_attachment(bytes(data), alt_text=alt))
        return uploaded

    def process_post(self, post):
        ident = str(post["id"])
        # Recheck the live source, even when resuming a stored job.
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
        if created < get_sync_started_at(self.db):
            return self._skip(ident, "Source predates first activation")
        if post.get("reblog"):
            return self._skip(ident, "Boost publishing is not supported yet")
        parent_id = str(post["in_reply_to_id"]) if post.get("in_reply_to_id") else None
        account_id = str((post.get("account") or {}).get("id") or "")
        is_self_reply = bool(parent_id and account_id and str(post.get("in_reply_to_account_id")) == account_id)
        if parent_id and not (settings.MIRROR_REPLIES or (is_self_reply and settings.MIRROR_SELF_REPLIES)):
            return self._skip(ident, "Reply publishing is disabled for this source")
        if post.get("spoiler_text") and not settings.MIRROR_CONTENT_WARNINGS:
            return self._skip(ident, "Content-warning publishing is disabled")

        row = self.db.get(SyncMap, ident)
        if row and row.status in {"failed", "needs_review"}:
            return "failed"
        if row and row.status in {"success", "skipped"}:
            return "existing"
        if is_self_reply and not settings.MIRROR_REPLIES and created < get_self_replies_started_at(self.db):
            return self._skip(ident, "Self-reply predates activation of self-reply mirroring")
        if row is None:
            row = SyncMap(mastodon_id=ident, status="pending")
            self.db.add(row)
        else:
            row.status = "pending"
        self.db.commit()

        part_index = None
        total = 0
        try:
            reply_context = None
            if parent_id:
                source_parent = self.masto.get_status(parent_id)
                if source_parent.get("visibility") != "public":
                    return self._skip(ident, "Reply parent is not public")
                parent_account = str((source_parent.get("account") or {}).get("id") or "")
                if str(source_parent.get("id")) != parent_id or parent_account != str(post.get("in_reply_to_account_id")):
                    raise ValueError("Source reply parent identity does not match")
                parent = self.db.get(SyncMap, parent_id)
                if parent is None or parent.status != "success" or not parent.bluesky_uri or not parent.bluesky_cid:
                    row.status = "pending"
                    row.error_message = "Waiting for the source parent to finish mirroring"
                    self.db.commit()
                    return "pending"
                reply_context = self.bsky.get_reply_context(parent.bluesky_uri, parent.bluesky_cid)
            parts = self.db.query(SyncPart).filter_by(mastodon_id=ident).order_by(SyncPart.part_index).all()
            fingerprint = source_fingerprint(post)
            if not parts:
                if row.bluesky_uri or row.bluesky_cid:
                    raise ValueError("Legacy partial thread has no saved part keys; manual review is required")
                text = self.transformer.clean_text(post.get("content", ""))
                if post.get("spoiler_text"):
                    text = f"CW: {post['spoiler_text']}\n\n{text}".strip()
                images = self._images(post)
                if not text and not images:
                    return self._skip(ident, "Source contains no supported text or images")
                texts = self.transformer.split_into_threads(text)
                for index, text in enumerate(texts):
                    reply_args = {}
                    if index == 0 and reply_context is not None:
                        reply_args = dict(parent_uri=reply_context.parent.uri, parent_cid=reply_context.parent.cid,
                                          root_uri=reply_context.root.uri, root_cid=reply_context.root.cid)
                    record = self.bsky.build_record(text, images=images if index == 0 else [], **reply_args)
                    part = SyncPart(
                        mastodon_id=ident, part_index=index, record_key=new_record_key(),
                        repo_did=self.bsky.repo_did, source_fingerprint=fingerprint,
                        record_json=record.model_dump_json(by_alias=True, exclude_none=True),
                    )
                    self.db.add(part)
                    parts.append(part)
                validate_image_inventory(
                    [json.loads(part.record_json) for part in parts],
                    [alt for url, alt in self.transformer.process_media(post)],
                )
                # All record keys and exact payloads must be durable before writes.
                self.db.commit()
            elif any(part.source_fingerprint and part.source_fingerprint != fingerprint for part in parts):
                raise ValueError("Source content changed after thread planning; manual review is required")
            total = len(parts)
            first_record = models.AppBskyFeedPost.Record.model_validate_json(parts[0].record_json)
            if parent_id and first_record.reply != reply_context:
                raise ValueError("The saved reply context differs from its source parent; manual review is required")
            root_ref = first_record.reply.root if first_record.reply else None
            parent_ref = first_record.reply.parent if first_record.reply else None
            for part in parts:
                part_index = part.part_index
                record = models.AppBskyFeedPost.Record.model_validate_json(part.record_json)
                if part.part_index and not part.bluesky_uri:
                    record.reply = models.AppBskyFeedPost.ReplyRef(root=root_ref, parent=parent_ref)
                    part.record_json = record.model_dump_json(by_alias=True, exclude_none=True)
                    self.db.commit()
                uri, cid = self.bsky.publish_record(
                    record=record, record_key=part.record_key, repo_did=part.repo_did,
                    known_uri=part.bluesky_uri, known_cid=part.bluesky_cid,
                )
                part.bluesky_uri, part.bluesky_cid = uri, cid
                row.bluesky_uri, row.bluesky_cid = uri, cid
                self.db.commit()
                ref = models.ComAtprotoRepoStrongRef.Main(uri=uri, cid=cid)
                if root_ref is None:
                    root_ref = ref
                parent_ref = ref
            row.status = "success"
            row.error_message = None
            row.synced_at = datetime.utcnow()
            self.db.commit()
            logger.info("Successfully mirrored Mastodon status %s (%d part(s))", ident, total)
            return "synced"
        except Exception as exc:
            self.db.rollback()
            row = self.db.get(SyncMap, ident)
            row.status = "failed"
            prefix = f"Part {part_index + 1}/{total}: " if part_index is not None else "Preparation failed: "
            row.error_message = prefix + describe_error(exc)
            row.synced_at = datetime.utcnow()
            self.db.commit()
            logger.error("Failed to mirror Mastodon status %s: %s", ident, row.error_message)
            return "failed"
