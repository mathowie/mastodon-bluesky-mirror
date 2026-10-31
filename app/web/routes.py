"""Authenticated history, progress and durable manual retry controls."""
from datetime import timezone, datetime
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import describe_error
from ..core.image_runtime import image_status
from ..database import get_db, SettingsStore, SyncMap, SyncPart, ThreadsSyncMap, ThreadsSyncPart

router = APIRouter()
Destination = Literal["bluesky", "threads"]


def _safe_text(message):
    return describe_error(Exception(message)).removeprefix("Exception: ") if message else message


def _utc_timestamp(when):
    if when and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if when else None


def _threads_connection(db):
    # The client reads local metadata here; verification and renewal belong to
    # setup/the worker. Keep optional imports and failures off default routes.
    status = {"enabled": settings.THREADS_ENABLED}
    try:
        from ..clients.threads import ThreadsClient
        metadata = ThreadsClient().connection_status()
        for key in ("configured", "account_id", "username", "expires_at", "status", "error"):
            if key in metadata:
                value = metadata[key]
                status[key] = _safe_text(value) if isinstance(value, str) else value
    except Exception:
        status.update(configured=False, status="unavailable", error="Threads connection status is unavailable")
    error = db.get(SettingsStore, "threads_credentials_error")
    if error and error.value:
        status.update(status="retry_wait", error=_safe_text(error.value))
        retry = db.get(SettingsStore, "threads_credentials_retry_at")
        if retry:
            try:
                status["retry_at"] = _utc_timestamp(datetime.fromisoformat(retry.value))
            except (TypeError, ValueError):
                pass
    return status


@router.get("/stats")
async def get_stats(db: Session = Depends(get_db), destination: Destination = "bluesky"):
    model = ThreadsSyncMap if destination == "threads" else SyncMap
    counts = dict(db.query(model.status, func.count()).group_by(model.status).all())
    success = counts.get("success", 0)
    failed = counts.get("failed", 0) + counts.get("needs_review", 0)
    completed = success + failed
    result = {
        "total_synced": success, "failed": failed,
        "image_processing": image_status(),
        "queued": counts.get("queued", 0), "pending": counts.get("pending", 0),
        "success_rate": f"{success / completed * 100 if completed else 0:.1f}%",
    }
    if destination == "threads":
        result["threads_connection"] = _threads_connection(db)
    return result


@router.get("/history")
async def get_history(db: Session = Depends(get_db), destination: Destination = "bluesky"):
    model, part = (ThreadsSyncMap, ThreadsSyncPart) if destination == "threads" else (SyncMap, SyncPart)
    published_id = part.threads_id if destination == "threads" else part.bluesky_uri
    entries = db.query(model).order_by(model.synced_at.desc()).limit(50).all()
    progress = {
        ident: (total, published)
        for ident, total, published in db.query(
            part.mastodon_id, func.count(), func.count(published_id),
        ).filter(part.mastodon_id.in_([row.mastodon_id for row in entries]))
        .group_by(part.mastodon_id).all()
    }
    result = []
    for row in entries:
        total, published = progress.get(row.mastodon_id, (1, 1) if row.status == "success" else (0, 0))
        entry = {
            "mastodon_id": row.mastodon_id, "status": row.status,
            "synced_at": _utc_timestamp(row.synced_at),
            "error": _safe_text(row.error_message), "parts_total": total, "parts_published": published,
            "bluesky_uri": getattr(row, "bluesky_uri", None),
        }
        if destination == "threads":
            entry.update(destination="threads", threads_id=row.threads_id, permalink=row.permalink,
                         next_attempt_at=_utc_timestamp(row.next_attempt_at))
        result.append(entry)
    return result


@router.post("/retry/{mastodon_id}", status_code=202)
async def retry_post(mastodon_id: str, request: Request, db: Session = Depends(get_db), destination: Destination = "bluesky"):
    if not re.fullmatch(r"[0-9]{1,30}", mastodon_id):
        raise HTTPException(status_code=422, detail="Invalid Mastodon status ID")
    if destination == "threads" and not settings.THREADS_ENABLED:
        raise HTTPException(status_code=409, detail="Threads is disabled; no retry was queued")
    model = ThreadsSyncMap if destination == "threads" else SyncMap
    row = db.get(model, mastodon_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No sync entry exists for this status")
    if row.status == "success":
        raise HTTPException(status_code=409, detail="This status was already mirrored; no retry was queued")
    if row.status == "needs_review":
        if destination == "threads":
            raise HTTPException(status_code=409, detail="Review the uncertain Threads publication before retrying")
        raise HTTPException(status_code=409, detail="This older attempt has no saved record keys; review Bluesky before retrying")
    if row.status == "pending":
        raise HTTPException(status_code=409, detail="This status is already being processed")
    if row.status not in {"failed", "queued"}:
        raise HTTPException(status_code=409, detail="This source status is not eligible for retry")
    if destination == "threads":
        parts = db.query(ThreadsSyncPart).filter_by(mastodon_id=mastodon_id).all()
        if any(part.state == "publishing" or (part.state == "published" and not part.threads_id) for part in parts):
            raise HTTPException(status_code=409, detail="Review the uncertain Threads publication before retrying")
        row.attempt_count = 0
        # A manual retry must respect a provider's saved Retry-After deadline.
        not_before = row.next_attempt_at
        if not_before and not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        if not_before is None or not_before <= datetime.now(timezone.utc):
            row.next_attempt_at = None
    # Keep every URI/CID and planned key. Deleting them destroys duplicate protection.
    # Threads likewise retains all payloads, child/container IDs and post IDs.
    row.status = "queued"
    row.error_message = None
    row.synced_at = datetime.utcnow()
    db.commit()
    wakeup = getattr(request.app.state, "sync_wakeup", None)
    if wakeup is not None:
        wakeup.set()
    result = {"mastodon_id": mastodon_id, "status": "queued", "message": "Queued for retry; completed thread parts will be reused"}
    if destination == "threads":
        result["next_attempt_at"] = _utc_timestamp(row.next_attempt_at)
        if row.next_attempt_at is not None:
            result["message"] = f"Queued for retry after {result['next_attempt_at']}; completed thread parts will be reused"
    return result
