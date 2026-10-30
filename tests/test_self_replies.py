"""Self-replies keep their source parent and original Bluesky thread root."""
import json
from types import SimpleNamespace

import pytest
from atproto import models

from app import database
from app.config import settings
from app.core import syncer as sync_module
from app.database import SyncMap, SyncPart, SettingsStore
from test_sync_regressions import db, status, bluesky_client, syncer, DID, CID

ROOT = '100000000000000001'
CHILD = '100000000000000002'
GRANDCHILD = '100000000000000003'


@pytest.fixture(autouse=True)
def reply_boundary(db):
    db.add(SettingsStore(key='self_replies_started_at', value='2026-10-01T00:00:00+00:00'))
    db.commit()


def reply(ident=CHILD, parent=ROOT, **changes):
    return status(id=ident, in_reply_to_id=parent, in_reply_to_account_id='42',
                  content=changes.pop('content', '<p>A follow-up.</p>'), **changes)


def runner(db, bsky, posts, parents=None):
    result = syncer(db, bsky, posts)
    lookup = {str(p['id']): p for p in (parents or posts)}
    result.masto.get_status = lambda ident: lookup[str(ident)]
    return result


def test_public_self_reply_attaches_to_the_mirrored_parent(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    posts = [reply(), status()]
    work = runner(db, bsky, posts)
    work.run_sync_cycle()
    assert len(records) == 2
    assert records[1].reply.parent.uri == records.uris[0]
    assert records[1].reply.parent.cid == records.cids[0]
    assert records[1].reply.root.uri == records.uris[0]
    work.run_sync_cycle()
    assert len(records) == 2
    assert db.get(SyncMap, CHILD).status == 'success'


def test_nested_self_reply_keeps_the_original_root(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    posts = [reply(GRANDCHILD, CHILD), reply(), status()]
    runner(db, bsky, posts).run_sync_cycle()
    assert len(records) == 3
    assert records[2].reply.parent.uri == records.uris[1]
    assert records[2].reply.parent.cid == records.cids[1]
    assert records[2].reply.root.uri == records.uris[0]
    assert records[2].reply.root.cid == records.cids[0]


def test_split_parent_and_split_reply_keep_the_entire_thread_in_order(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    parent = status(content='<p>' + 'parent words ' * 35 + '</p>')
    child = reply(content='<p>' + 'follow up ' * 40 + '</p>')
    runner(db, bsky, [child, parent]).run_sync_cycle()
    assert len(records) == 4
    assert records[2].reply.parent.uri == records.uris[1]
    assert records[2].reply.parent.cid == records.cids[1]
    assert records[2].reply.root.uri == records.uris[0]
    assert records[3].reply.parent.uri == records.uris[2]
    assert records[3].reply.root.uri == records.uris[0]


def test_self_reply_waits_for_parent_success_then_recovers_outside_recent_poll(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    parent, child = status(), reply()
    work = runner(db, bsky, [child], [parent, child])
    db.add(SyncMap(mastodon_id=ROOT, status='failed', error_message='Preparation failed: temporary'))
    db.commit()
    work.run_sync_cycle()
    assert len(records) == 0
    row = db.get(SyncMap, CHILD)
    assert row is not None and row.status == 'pending'
    assert 'parent' in row.error_message.lower()
    db.get(SyncMap, ROOT).status = 'queued'
    db.commit()
    work.masto.get_recent_posts = lambda limit=20: []
    work.run_sync_cycle()
    assert len(records) == 2
    assert records[1].reply.parent.uri == records.uris[0]


def test_old_self_replies_are_not_backfilled_when_feature_is_enabled(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    parent, child = status(), reply()
    work = runner(db, bsky, [parent])
    work.run_sync_cycle()
    db.get(SettingsStore, 'self_replies_started_at').value = '2026-10-30T00:00:00+00:00'
    db.commit()
    work = runner(db, bsky, [child], [parent])
    work.run_sync_cycle()
    assert len(records) == 1
    assert db.get(SyncMap, CHILD) is None


def test_self_replies_can_be_disabled_without_changing_other_preferences(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    values = settings.model_dump()
    values.update(MIRROR_SELF_REPLIES=False, MIRROR_REPLIES=False)
    monkeypatch.setattr(sync_module, 'settings', SimpleNamespace(**values))
    runner(db, bsky, [status(), reply()]).run_sync_cycle()
    assert len(records) == 1


def test_a_parent_that_became_private_is_not_used_for_new_public_replies(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    runner(db, bsky, [status()]).run_sync_cycle()
    child = reply()
    runner(db, bsky, [child], [status(visibility='private')]).run_sync_cycle()
    assert len(records) == 1
    row = db.get(SyncMap, CHILD)
    assert row is not None and row.status == 'skipped'
    assert db.query(SyncPart).filter_by(mastodon_id=CHILD).count() == 0


def test_deleted_bluesky_parent_does_not_create_an_orphan_reply(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    parent = status()
    runner(db, bsky, [parent]).run_sync_cycle()
    records.by_key.clear()
    runner(db, bsky, [reply()], [parent]).run_sync_cycle()
    assert len(records) == 1
    row = db.get(SyncMap, CHILD)
    assert row is not None and row.status == 'failed'
    assert 'missing' in row.error_message.lower()


def test_recovery_adopts_a_reply_only_when_its_saved_parent_matches(db, monkeypatch, tmp_path):
    bsky, records = bluesky_client(monkeypatch)
    runner(db, bsky, [status()]).run_sync_cycle()
    ref = {'uri': records.uris[0], 'cid': records.cids[0]}
    context = {'root': ref, 'parent': ref}
    key = '2222222222225'
    uri = f'at://{DID}/app.bsky.feed.post/{key}'
    record = {'$type': 'app.bsky.feed.post', 'text': 'Recovered reply',
              'createdAt': '2026-10-29T12:01:00Z', 'reply': context}
    journal = {'mastodon_id': CHILD, 'repo': DID, 'source_images': [],
               'source_reply_to_id': ROOT, 'reply_context': context,
               'parts': [{'record_key': key, 'text': 'Recovered reply', 'state': 'published',
                          'uri': uri, 'cid': CID, 'record': record}]}
    path = tmp_path / (CHILD + '.json')
    path.write_text(json.dumps(journal))
    assert database.import_recovery_journals(db, tmp_path) == 1
    assert db.get(SyncMap, CHILD).status == 'success'
    assert db.get(SyncMap, CHILD).bluesky_uri == uri
    assert database.import_recovery_journals(db, tmp_path) == 0
    saved = db.query(SyncPart).filter_by(mastodon_id=CHILD).one()
    assert json.loads(saved.record_json)['reply']['root']['uri'] == records.uris[0]
    assert db.get(SyncMap, ROOT).bluesky_uri == records.uris[0]


def test_recovery_rejects_a_reply_with_unmapped_source_parent(db, tmp_path):
    key = '2222222222225'
    ref = {'uri': f'at://{DID}/app.bsky.feed.post/2222222222224', 'cid': CID}
    context = {'root': ref, 'parent': ref}
    record = {'$type': 'app.bsky.feed.post', 'text': 'Reply', 'createdAt': '2026-10-29T12:01:00Z', 'reply': context}
    journal = {'mastodon_id': CHILD, 'repo': DID, 'source_images': [], 'source_reply_to_id': ROOT,
               'reply_context': context, 'parts': [{'record_key': key, 'text': 'Reply', 'state': 'published',
               'uri': f'at://{DID}/app.bsky.feed.post/{key}', 'cid': CID, 'record': record}]}
    (tmp_path / (CHILD + '.json')).write_text(json.dumps(journal))
    assert database.import_recovery_journals(db, tmp_path) == 0
    assert db.get(SyncMap, CHILD) is None


def test_self_reply_activation_boundary_is_persisted(db):
    function = getattr(database, 'get_self_replies_started_at', None)
    assert callable(function), 'Self-replies need their own durable activation boundary'
    expected = db.get(SettingsStore, 'self_replies_started_at').value
    first = function(db)
    second = function(db)
    assert first == second
    assert db.get(SettingsStore, 'self_replies_started_at').value == expected


def test_partial_self_reply_retry_keeps_root_and_does_not_repeat_published_parts(db, monkeypatch):
    from test_retry_features import rejection
    bsky, records = bluesky_client(monkeypatch)
    parent = status()
    child = reply(content='<p>' + 'follow up ' * 40 + '</p>')
    work = runner(db, bsky, [parent, child])
    create = bsky.client.app.bsky.feed.post.create
    fail = [True]
    def create_until_final_part(repo, record, **kwargs):
        if len(records) == 2 and fail[0]:
            raise rejection()
        return create(repo, record, **kwargs)
    monkeypatch.setattr(bsky.client.app.bsky.feed.post, 'create', create_until_final_part)
    work.run_sync_cycle()
    assert len(records) == 2
    assert db.get(SyncMap, CHILD).status == 'failed'
    db.get(SyncMap, CHILD).status = 'queued'
    db.commit()
    fail[0] = False
    work.run_sync_cycle()
    assert len(records) == 3
    assert records[2].reply.parent.uri == records.uris[1]
    assert records[2].reply.root.uri == records.uris[0]
    assert records[2].reply.root.cid == records.cids[0]
    assert db.get(SyncMap, CHILD).status == 'success'


def test_an_edited_parent_cid_prevents_publishing_into_a_changed_context(db, monkeypatch):
    bsky, records = bluesky_client(monkeypatch)
    parent = status()
    runner(db, bsky, [parent]).run_sync_cycle()
    other_uri, other_cid = bsky.post_text('Different record')
    parent_key = records.uris[0].rsplit('/', 1)[-1]
    records.by_key[parent_key]['cid'] = other_cid
    runner(db, bsky, [reply()], [parent]).run_sync_cycle()
    assert len(records) == 2
    row = db.get(SyncMap, CHILD)
    assert row is not None and row.status == 'failed'
    assert 'changed' in row.error_message.lower()


def test_self_reply_boundary_survives_a_real_database_restart(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine('sqlite:///' + str(tmp_path / 'mirror.db'), connect_args={'check_same_thread': False})
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'SessionLocal', sessions)
    database.init_db()
    with sessions() as session:
        baseline = session.get(SettingsStore, 'self_replies_started_at')
        assert baseline is not None
        saved = baseline.value
    database.init_db()
    with sessions() as session:
        assert session.get(SettingsStore, 'self_replies_started_at').value == saved
    engine.dispose()
