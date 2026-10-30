"""Authenticated history, progress and durable manual retry controls."""
from datetime import timezone, datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.image_runtime import image_status
from ..database import get_db, SyncMap, SyncPart

router = APIRouter()


@router.get("/stats")
async def get_stats(db: Session = Depends(get_db)):
    counts = dict(db.query(SyncMap.status, func.count()).group_by(SyncMap.status).all())
    success = counts.get("success", 0)
    failed = counts.get("failed", 0) + counts.get("needs_review", 0)
    completed = success + failed
    return {
        "total_synced": success, "failed": failed,
        "image_processing": image_status(),
        "queued": counts.get("queued", 0), "pending": counts.get("pending", 0),
        "success_rate": f"{success / completed * 100 if completed else 0:.1f}%",
    }


@router.get("/history")
async def get_history(db: Session = Depends(get_db)):
    entries = db.query(SyncMap).order_by(SyncMap.synced_at.desc()).limit(50).all()
    progress = {
        ident: (total, published)
        for ident, total, published in db.query(
            SyncPart.mastodon_id, func.count(), func.count(SyncPart.bluesky_uri),
        ).filter(SyncPart.mastodon_id.in_([row.mastodon_id for row in entries]))
        .group_by(SyncPart.mastodon_id).all()
    }
    result = []
    for row in entries:
        total, published = progress.get(row.mastodon_id, (1, 1) if row.status == "success" else (0, 0))
        when = row.synced_at
        if when and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        result.append({
            "mastodon_id": row.mastodon_id, "status": row.status,
            "synced_at": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if when else None,
            "error": row.error_message, "parts_total": total, "parts_published": published,
            "bluesky_uri": row.bluesky_uri,
        })
    return result


@router.post("/retry/{mastodon_id}", status_code=202)
async def retry_post(mastodon_id: str, request: Request, db: Session = Depends(get_db)):
    if not re.fullmatch(r"[0-9]{1,30}", mastodon_id):
        raise HTTPException(status_code=422, detail="Invalid Mastodon status ID")
    row = db.get(SyncMap, mastodon_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No sync entry exists for this status")
    if row.status == "success":
        raise HTTPException(status_code=409, detail="This status was already mirrored; no retry was queued")
    if row.status == "needs_review":
        raise HTTPException(status_code=409, detail="This older attempt has no saved record keys; review Bluesky before retrying")
    if row.status == "pending":
        raise HTTPException(status_code=409, detail="This status is already being processed")
    if row.status not in {"failed", "queued"}:
        raise HTTPException(status_code=409, detail="This source status is not eligible for retry")
    # Keep every URI/CID and planned key. Deleting them destroys duplicate protection.
    row.status = "queued"
    row.error_message = None
    row.synced_at = datetime.utcnow()
    db.commit()
    wakeup = getattr(request.app.state, "sync_wakeup", None)
    if wakeup is not None:
        wakeup.set()
    return {"mastodon_id": mastodon_id, "status": "queued", "message": "Queued for retry; completed thread parts will be reused"}
