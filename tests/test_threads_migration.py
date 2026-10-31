"""Adding a destination preserves existing Bluesky state and activation times."""
import sqlite3

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app import database
from test_sync_regressions import db


def test_threads_tables_are_backed_up_before_an_existing_database_is_extended(tmp_path, monkeypatch):
    path = tmp_path / "mirror.db"
    engine = create_engine("sqlite:///" + str(path), connect_args={"check_same_thread": False})
    for model in [database.SyncMap, database.SyncPart, database.SettingsStore]:
        model.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(database.SyncMap(mastodon_id="42", status="success", bluesky_uri="at://kept"))
        session.add(database.SettingsStore(key="sync_started_at", value="2026-10-01T00:00:00+00:00"))
        session.commit()
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.init_db()
    assert inspect(engine).has_table("threads_sync_map")
    assert inspect(engine).has_table("threads_sync_parts")
    backups = list((tmp_path / "backups").glob("mirror-before-threads-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as saved:
        assert saved.execute("SELECT bluesky_uri FROM sync_map WHERE mastodon_id='42'").fetchone() == ("at://kept",)
        assert saved.execute("SELECT name FROM sqlite_master WHERE name='threads_sync_map'").fetchone() is None
    with sessions() as session:
        assert session.get(database.SyncMap, "42").status == "success"
        assert session.get(database.SettingsStore, "sync_started_at").value == "2026-10-01T00:00:00+00:00"
    database.init_db()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    engine.dispose()


def test_threads_activation_is_separate_and_persists(db):
    assert callable(getattr(database, "get_threads_started_at", None))
    bluesky_start = db.get(database.SettingsStore, "sync_started_at").value
    first = database.get_threads_started_at(db)
    assert database.get_threads_started_at(db) == first
    assert db.get(database.SettingsStore, "sync_started_at").value == bluesky_start


def test_disabled_threads_does_not_seed_an_activation_time(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(database.settings, "THREADS_ENABLED", False)
    database.init_db()
    with sessions() as session:
        assert session.get(database.SettingsStore, "threads_started_at") is None
    engine.dispose()
