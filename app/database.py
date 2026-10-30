# app/database.py
from sqlalchemy import create_engine, Column, String, DateTime, Boolean, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import sqlite3
from contextlib import closing
from sqlalchemy import inspect
from .config import settings

Base = declarative_base()

class SyncMap(Base):
    __tablename__ = "sync_map"
    
    mastodon_id = Column(String, primary_key=True) # Mastodon Status ID
    bluesky_uri = Column(String)                    # Bluesky Post URI
    bluesky_cid = Column(String)                    # Bluesky CID
    synced_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="success")       # success, failed
    error_message = Column(Text, nullable=True)

class SyncPart(Base):
    """A frozen, durably keyed part of one Mastodon status."""
    __tablename__ = "sync_parts"
    mastodon_id = Column(String, primary_key=True)
    part_index = Column(Integer, primary_key=True)
    record_key = Column(String, nullable=False, unique=True)
    repo_did = Column(String, nullable=False)
    record_json = Column(Text, nullable=False)
    source_fingerprint = Column(String, nullable=False, default="")
    bluesky_uri = Column(String, nullable=True)
    bluesky_cid = Column(String, nullable=True)


class SettingsStore(Base):
    __tablename__ = "settings"
    
    key = Column(String, primary_key=True)
    value = Column(String)

engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_sync_started_at(db):
    """Persist the activation boundary once; keep it across container restarts."""
    setting = db.get(SettingsStore, "sync_started_at")
    if setting is None:
        setting = SettingsStore(
            key="sync_started_at", value=datetime.now(timezone.utc).isoformat(),
        )
        db.add(setting)
        db.commit()
    return datetime.fromisoformat(setting.value).astimezone(timezone.utc)



def get_self_replies_started_at(db):
    """Enable future self-replies once, without replaying earlier skipped replies."""
    setting = db.get(SettingsStore, "self_replies_started_at")
    if setting is None:
        setting = SettingsStore(key="self_replies_started_at", value=datetime.now(timezone.utc).isoformat())
        db.add(setting)
        db.commit()
    return datetime.fromisoformat(setting.value).astimezone(timezone.utc)


def init_db():
    # Run inside the NAS container, where SQLite locking and backup are local.
    legacy_migration = False
    database_name = engine.url.database
    if database_name and database_name != ":memory:":
        database_path = Path(database_name)
        if database_path.exists() and not inspect(engine).has_table("sync_parts"):
            legacy_migration = True
            backup_folder = database_path.parent / "backups"
            backup_folder.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = backup_folder / f"mirror-before-thread-retries-{stamp}.db"
            with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(backup_path)) as target:
                    source.backup(target)
            logging.getLogger(__name__).info("Created SQLite backup before thread-retry migration")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        get_sync_started_at(db)
        if settings.MIRROR_SELF_REPLIES:
            get_self_replies_started_at(db)
        # Recheck on every startup: a crash may commit the new table before
        # older failures were classified. New preparation/source-read failures
        # are safe to retry because no network post preceded their checkpoints.
        for row in db.query(SyncMap).filter(SyncMap.status.in_(["failed", "queued", "pending"])):
            has_parts = db.query(SyncPart).filter_by(mastodon_id=row.mastodon_id).first() is not None
            safe_preparation = (row.error_message or "").startswith(("Preparation failed:", "Source refresh failed:"))
            if not has_parts and not safe_preparation and (legacy_migration or row.status == "failed"):
                row.status = "needs_review"
                row.error_message = "This older attempt has no saved record keys; review Bluesky before retrying"
        db.commit()
        if engine.url.database and engine.url.database != ":memory:":
            import_recovery_journals(db, Path(engine.url.database).parent / "recovery-attempts")



def _recovery_reply_context(db, journal, ident):
    """Validate a recovered reply against its already-mirrored source parent."""
    from atproto import models
    parent_id = journal.get("source_reply_to_id")
    context = journal.get("reply_context")
    if parent_id is None and context is None:
        return None
    parent_id = str(parent_id)
    if not parent_id.isdigit() or parent_id == ident or not context:
        raise ValueError("Invalid recovery reply context")
    parent = db.get(SyncMap, parent_id)
    parts = db.query(SyncPart).filter_by(mastodon_id=parent_id).order_by(SyncPart.part_index).all()
    if parent is None or parent.status != "success" or not parts:
        raise ValueError("Recovery reply source parent has no complete mapping")
    if any(part.part_index != i or not part.bluesky_uri or not part.bluesky_cid
           or part.repo_did != journal["repo"] for i, part in enumerate(parts)):
        raise ValueError("Recovery reply parent has incomplete checkpoints")
    last = parts[-1]
    if (parent.bluesky_uri, parent.bluesky_cid) != (last.bluesky_uri, last.bluesky_cid):
        raise ValueError("Recovery reply parent checkpoint mismatch")
    first = models.AppBskyFeedPost.Record.model_validate_json(parts[0].record_json)
    root = first.reply.root if first.reply else models.ComAtprotoRepoStrongRef.Main(
        uri=parts[0].bluesky_uri, cid=parts[0].bluesky_cid,
    )
    expected = models.AppBskyFeedPost.ReplyRef(
        root=root, parent=models.ComAtprotoRepoStrongRef.Main(uri=last.bluesky_uri, cid=last.bluesky_cid),
    )
    actual = models.AppBskyFeedPost.ReplyRef.model_validate(context)
    if actual != expected:
        raise ValueError("Recovery reply does not match the saved parent and thread root")
    return actual


def import_recovery_journals(db, folder):
    """Adopt verified parts, but never hide missing source media."""
    from atproto import models
    from .core.media import validate_image_inventory, MediaMismatchError
    imported = 0
    for path in Path(folder).glob("*.json"):
        if not path.stem.isdigit() or path.stat().st_size > 4_000_000:
            continue
        try:
            journal = json.loads(path.read_text())
            ident = str(journal["mastodon_id"])
            if ident != path.stem:
                raise ValueError("Recovery journal status ID mismatch")
            parts = journal["parts"]
            if not parts or not all(p.get("state") == "published" for p in parts):
                continue
            existing = db.get(SyncMap, ident)
            # Do not replace a mapping that now refers to a different thread.
            if existing and existing.bluesky_uri and existing.bluesky_uri != parts[-1].get("uri"):
                continue
            saved = db.query(SyncPart).filter_by(mastodon_id=ident).order_by(SyncPart.part_index).all()
            validated, records = [], []
            context = _recovery_reply_context(db, journal, ident)
            root_ref = (context.root.uri, context.root.cid) if context else None
            parent_ref = (context.parent.uri, context.parent.cid) if context else None
            for index, part in enumerate(parts):
                uri = f"at://{journal['repo']}/app.bsky.feed.post/{part['record_key']}"
                if part["uri"] != uri or not part.get("cid"):
                    raise ValueError("Recovery journal record reference mismatch")
                record = models.AppBskyFeedPost.Record.model_validate(part["record"])
                if record.text != part["text"]:
                    raise ValueError("Recovery journal text mismatch")
                if index == 0 and record.reply != context:
                    raise ValueError("Recovery first post has an unexpected reply context")
                if index and (record.reply is None or record.reply.root.uri != root_ref[0]
                        or record.reply.root.cid != root_ref[1] or record.reply.parent.uri != parent_ref[0]
                        or record.reply.parent.cid != parent_ref[1]):
                    raise ValueError("Recovery journal thread relationship mismatch")
                ref = (part["uri"], part["cid"])
                if root_ref is None:
                    root_ref = ref
                parent_ref = ref
                records.append(record)
                validated.append(SyncPart(
                    mastodon_id=ident, part_index=index, record_key=part["record_key"],
                    repo_did=journal["repo"], record_json=record.model_dump_json(by_alias=True, exclude_none=True),
                    source_fingerprint=journal.get("source_fingerprint", ""),
                    bluesky_uri=part["uri"], bluesky_cid=part["cid"],
                ))
            if saved and (len(saved) != len(validated) or any(
                    (old.record_key, old.bluesky_uri, old.bluesky_cid, json.loads(old.record_json)) !=
                    (new.record_key, new.bluesky_uri, new.bluesky_cid, json.loads(new.record_json))
                    for old, new in zip(saved, validated))):
                raise ValueError("Recovery journal conflicts with existing part checkpoints")
            inventory = journal.get("source_images")
            expected = [item.get("alt") or "" for item in inventory] if isinstance(inventory, list) and all(isinstance(item, dict) for item in inventory) else None
            problem = None
            try:
                validate_image_inventory(records, expected)
            except MediaMismatchError as exc:
                problem = str(exc)
            target_status = "needs_review" if problem else "success"
            if existing and saved and existing.status == target_status and existing.error_message == problem:
                continue
            if existing is None:
                existing = SyncMap(mastodon_id=ident)
                db.add(existing)
            if not saved:
                db.add_all(validated)
            existing.status = target_status
            existing.error_message = problem
            existing.bluesky_uri, existing.bluesky_cid = parent_ref
            existing.synced_at = datetime.utcnow()
            db.commit()
            if problem:
                logging.getLogger(__name__).warning("Recovered Mastodon status %s requires media review: %s", ident, problem)
            else:
                imported += 1
                logging.getLogger(__name__).info("Adopted recovered Bluesky thread for Mastodon status %s", ident)
        except Exception as exc:
            db.rollback()
            logging.getLogger(__name__).error("Recovery journal import failed (%s)", type(exc).__name__)
    return imported


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
