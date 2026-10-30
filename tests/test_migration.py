"""Migration uses SQLite's backup API and preserves the activation boundary."""
from pathlib import Path
import sqlite3
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app import database


def test_existing_database_is_backed_up_before_parts_table_is_added(tmp_path,monkeypatch):
    path=tmp_path/"mirror.db"
    engine=create_engine("sqlite:///"+str(path),connect_args={"check_same_thread":False})
    database.SyncMap.__table__.create(engine)
    database.SettingsStore.__table__.create(engine)
    sessions=sessionmaker(bind=engine)
    with sessions() as db:
        db.add(database.SyncMap(mastodon_id="42",status="success",bluesky_uri="at://original"))
        db.add(database.SettingsStore(key="sync_started_at",value="2026-10-01T00:00:00+00:00"))
        db.commit()
    monkeypatch.setattr(database,"engine",engine)
    monkeypatch.setattr(database,"SessionLocal",sessions)
    database.init_db()
    assert inspect(engine).has_table("sync_parts")
    backups=list((tmp_path/"backups").glob("mirror-before-thread-retries-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT status FROM sync_map WHERE mastodon_id='42'").fetchone()[0] == "success"
        assert backup.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sync_parts'").fetchone() is None
    with sessions() as db:
        assert db.get(database.SettingsStore,"sync_started_at").value == "2026-10-01T00:00:00+00:00"
        assert db.get(database.SyncMap,"42").bluesky_uri == "at://original"
    database.init_db()
    assert len(list((tmp_path/"backups").glob("*.db"))) == 1
    engine.dispose()


@pytest.mark.parametrize("ddl_already_committed", [False, True])
def test_legacy_failed_attempt_is_held_for_review_without_losing_its_mapping(tmp_path,monkeypatch,ddl_already_committed):
    path=tmp_path/"mirror.db"
    engine=create_engine("sqlite:///"+str(path),connect_args={"check_same_thread":False})
    database.SyncMap.__table__.create(engine)
    database.SettingsStore.__table__.create(engine)
    if ddl_already_committed:
        database.SyncPart.__table__.create(engine)
    sessions=sessionmaker(bind=engine)
    with sessions() as db:
        db.add(database.SyncMap(mastodon_id="43",status="failed",error_message="BadRequestError",bluesky_uri="at://keep-me",bluesky_cid="keep-cid"))
        db.add(database.SettingsStore(key="sync_started_at",value="2026-10-01T00:00:00+00:00"))
        db.commit()
    monkeypatch.setattr(database,"engine",engine)
    monkeypatch.setattr(database,"SessionLocal",sessions)
    database.init_db()
    with sessions() as db:
        row=db.get(database.SyncMap,"43")
        assert row.status == "needs_review"
        assert row.bluesky_uri == "at://keep-me"
        assert row.bluesky_cid == "keep-cid"
        assert "record keys" in row.error_message
    engine.dispose()
