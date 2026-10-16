"""Backed-up, atomic source-qualified identity upgrade. No live DB during QA."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import sqlite3
import uuid

from library import PlayerError, canonical_key

REFERENCES = ('favorites', 'playlist_tracks', 'talk_listens', 'talk_lyrics', 'talk_analysis')


def columns(db, table):
    return {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}


def migrate(db, path, clean):
    existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = db.execute('SELECT key,metadata FROM tracks').fetchall()
    legacy = any(key != canonical_key(key) for key, _ in rows)
    if db.execute('PRAGMA user_version').fetchone()[0] >= 2 and not legacy and 'source' in columns(db, 'tracks'):
        return None
    # Validate every mapping before changing anything. Never merge collisions or
    # delete one recording because its key resembles another provider's key.
    mapped = [(old, canonical_key(old), clean(json.loads(metadata))) for old, metadata in rows]
    if len({new for _, new, _ in mapped}) != len(mapped) or any(new != t['key'] for _, new, t in mapped):
        raise PlayerError('LIBRARY_MIGRATION_CONFLICT', 500)
    backup = None
    if rows:
        folder = path.resolve().parent / 'backups'
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = folder / ('library-before-sources-' + stamp + '-' + uuid.uuid4().hex[:8] + '.sqlite3')
        target = sqlite3.connect(backup)
        try:
            db.backup(target)  # Consistent online SQLite snapshot, including WAL.
        finally:
            target.close()
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('PRAGMA defer_foreign_keys=ON')
        if 'source' not in columns(db, 'tracks'):
            db.execute("ALTER TABLE tracks ADD COLUMN source TEXT NOT NULL DEFAULT 'qq'")
        if 'talk_listens' in existing and 'source' not in columns(db, 'talk_listens'):
            db.execute("ALTER TABLE talk_listens ADD COLUMN source TEXT NOT NULL DEFAULT 'qq'")
        for old, new, track in mapped:
            db.execute('UPDATE tracks SET key=?,metadata=?,source=? WHERE key=?',
                       (new, json.dumps(track, ensure_ascii=False), track['source'], old))
            if old != new:
                for table in REFERENCES:
                    if table in existing:
                        db.execute('UPDATE ' + table + ' SET key=? WHERE key=?', (new, old))
            for table in ('talk_lyrics', 'talk_analysis'):
                if table in existing:
                    row = db.execute('SELECT data FROM ' + table + ' WHERE key=?', (new,)).fetchone()
                    if row:
                        payload = json.loads(row[0])
                        payload.update(key=new, source=track['source'])
                        db.execute('UPDATE ' + table + ' SET data=? WHERE key=?',
                                   (json.dumps(payload, ensure_ascii=False), new))
            if 'talk_listens' in existing:
                db.execute('UPDATE talk_listens SET source=? WHERE key=?', (track['source'], new))
        row = db.execute('SELECT data FROM settings WHERE id=1').fetchone()
        state = json.loads(row[0])
        state['queue'] = [canonical_key(key) for key in state['queue']]
        if state['current'] is not None:
            state['current'] = canonical_key(state['current'])
        db.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps(state),))
        db.execute('CREATE INDEX IF NOT EXISTS tracks_source ON tracks(source)')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise PlayerError('LIBRARY_MIGRATION_REFERENCES', 500)
        db.execute('PRAGMA user_version=2')
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        raise
    return {'version': 2, 'trackCount': len(rows), 'backup': backup.name if backup else None}
