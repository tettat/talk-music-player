"""Local-only music library. No provider-side writes or credentials."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class PlayerError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


MID = re.compile(r"[A-Za-z0-9]{10,32}\Z")


def valid_mid(value):
    return isinstance(value, str) and MID.fullmatch(value) is not None


def text(value, limit):
    if not isinstance(value, str):
        raise PlayerError('INVALID_INPUT')
    value = ''.join(c for c in value if ord(c) >= 32).strip()
    if not value or len(value) > limit:
        raise PlayerError('INVALID_INPUT')
    return value


def clean_track(track):
    """Only canonical, bounded public metadata can enter SQLite/the web response."""
    if not valid_mid(track.get('mid')):
        raise PlayerError('INVALID_TRACK')
    song_id, duration = track.get('songId'), track.get('duration')
    if type(song_id) is not int or not 0 < song_id < 2**53:
        raise PlayerError('INVALID_TRACK')
    if type(duration) is not int or not 0 < duration <= 1800:
        raise PlayerError('INVALID_TRACK')
    album_mid = track.get('albumMid', '')
    if album_mid and not valid_mid(album_mid):
        album_mid = ''
    return {'key': track['mid'], 'mid': track['mid'], 'songId': song_id,
            'title': text(track.get('title'), 240),
            'artist': text(track.get('artist') or '—', 400),
            'album': text(track.get('album') or '—', 240),
            'albumMid': album_mid, 'duration': duration,
            'cover': '/api/cover/' + album_mid if album_mid else '/assets/cover-default.svg'}


class Library:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS tracks (
            key TEXT PRIMARY KEY, metadata TEXT NOT NULL, used REAL);
          CREATE TABLE IF NOT EXISTS favorites (
            key TEXT PRIMARY KEY REFERENCES tracks(key), created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS playlists (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist TEXT REFERENCES playlists(id) ON DELETE CASCADE,
            key TEXT REFERENCES tracks(key), position INTEGER NOT NULL,
            PRIMARY KEY(playlist,key));
          CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        ''')
        self.db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)',
                        (json.dumps({'queue': [], 'current': None, 'position': 0,
                                     'volume': .8, 'mode': 'repeat'}),))

    def put(self, tracks, used=False):
        cleaned = [clean_track(t) for t in tracks]
        with self.lock, self.db:
            count = self.db.execute('SELECT count(*) FROM tracks').fetchone()[0]
            if count + len(cleaned) > 10000:
                # Only prune never-saved search metadata, never favorites/list/queue tracks.
                protected = set(self.state()['queue'])
                if self.state()['current']:
                    protected.add(self.state()['current'])
                for table in ('favorites', 'playlist_tracks', 'talk_listens', 'talk_lyrics', 'talk_analysis'):
                    # Insight tables are optional when the library is used on its own.
                    if self.db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
                        protected.update(key for (key,) in self.db.execute('SELECT DISTINCT key FROM '+table))
                rows = self.db.execute('SELECT key FROM tracks WHERE used IS NULL LIMIT 1000').fetchall()
                self.db.executemany('DELETE FROM tracks WHERE key=?',
                                    [(key,) for (key,) in rows if key not in protected])
            for track in cleaned:
                self.db.execute('''INSERT INTO tracks(key,metadata,used) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET metadata=excluded.metadata,
                    used=coalesce(excluded.used,tracks.used)''',
                    (track['key'], json.dumps(track, ensure_ascii=False), time.time() if used else None))
        return cleaned

    def get(self, key):
        if not valid_mid(key):
            raise PlayerError('INVALID_TRACK')
        with self.lock:
            row = self.db.execute('SELECT metadata FROM tracks WHERE key=?', (key,)).fetchone()
        if not row:
            raise PlayerError('TRACK_NOT_FOUND', 404)
        return json.loads(row[0])

    def mark_used(self, key):
        self.get(key)
        with self.lock:
            self.db.execute('UPDATE tracks SET used=? WHERE key=?', (time.time(), key))

    def state(self):
        with self.lock:
            return json.loads(self.db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])

    def save_state(self, data):
        if not isinstance(data, dict) or set(data) - {'queue', 'current', 'position', 'volume', 'mode'}:
            raise PlayerError('INVALID_INPUT')
        with self.lock:
            state = self.state()
            state.update(data)
            queue = state['queue']
            if (not isinstance(queue, list) or len(queue) > 500 or
                    any(not valid_mid(k) for k in queue) or len(set(queue)) != len(queue)):
                raise PlayerError('INVALID_QUEUE')
            for key in queue:
                self.get(key)
            current = state['current']
            if current is not None:
                self.get(current)
            if state['mode'] not in ('repeat', 'single', 'shuffle'):
                raise PlayerError('INVALID_INPUT')
            for key, maximum in [('position', 1800), ('volume', 1)]:
                if type(state[key]) not in (int, float) or not 0 <= state[key] <= maximum:
                    raise PlayerError('INVALID_INPUT')
            self.db.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps(state),))
            return state

    def snapshot(self):
        with self.lock:
            state = self.state()
            all_tracks = [json.loads(r[0]) for r in self.db.execute(
                'SELECT metadata FROM tracks ORDER BY used DESC, rowid DESC')]
            library = [r[0] for r in self.db.execute('SELECT key FROM tracks WHERE used IS NOT NULL ORDER BY used DESC')]
            needed = set(library + state['queue'] + ([state['current']] if state['current'] else []))
            favorites = [r[0] for r in self.db.execute('SELECT key FROM favorites ORDER BY created DESC')]
            playlists = []
            for pid, name in self.db.execute('SELECT id,name FROM playlists ORDER BY created'):
                keys = [r[0] for r in self.db.execute('SELECT key FROM playlist_tracks WHERE playlist=? ORDER BY position', (pid,))]
                needed.update(keys)
                playlists.append({'id': pid, 'name': name, 'keys': keys})
            needed.update(favorites)
            return {'tracks': [t for t in all_tracks if t['key'] in needed], 'library': library,
                    'favorites': favorites, 'playlists': playlists, 'state': state}

    def favorite(self, key, enabled):
        if type(enabled) is not bool:
            raise PlayerError('INVALID_INPUT')
        self.mark_used(key)
        with self.lock:
            if enabled:
                self.db.execute('INSERT OR IGNORE INTO favorites VALUES(?,?)', (key, time.time()))
            else:
                self.db.execute('DELETE FROM favorites WHERE key=?', (key,))

    def create_playlist(self, name):
        name = text(name, 40)
        with self.lock:
            if self.db.execute('SELECT count(*) FROM playlists').fetchone()[0] >= 100:
                raise PlayerError('PLAYLIST_LIMIT')
            pid = uuid.uuid4().hex
            self.db.execute('INSERT INTO playlists VALUES(?,?,?)', (pid, name, time.time()))
        return pid

    def playlist(self, pid, action, value=None):
        if not isinstance(pid, str) or not re.fullmatch('[a-f0-9]{32}', pid):
            raise PlayerError('PLAYLIST_NOT_FOUND', 404)
        with self.lock:
            if not self.db.execute('SELECT 1 FROM playlists WHERE id=?', (pid,)).fetchone():
                raise PlayerError('PLAYLIST_NOT_FOUND', 404)
            if action == 'rename':
                self.db.execute('UPDATE playlists SET name=? WHERE id=?', (text(value, 40), pid))
            elif action == 'delete':
                self.db.execute('DELETE FROM playlists WHERE id=?', (pid,))
            elif action == 'add':
                self.get(value)
                count = self.db.execute('SELECT count(*) FROM playlist_tracks WHERE playlist=?', (pid,)).fetchone()[0]
                if count >= 500:
                    raise PlayerError('PLAYLIST_LIMIT')
                position = self.db.execute('SELECT coalesce(max(position),0)+1 FROM playlist_tracks WHERE playlist=?', (pid,)).fetchone()[0]
                self.db.execute('INSERT OR IGNORE INTO playlist_tracks VALUES(?,?,?)', (pid, value, position))
                self.mark_used(value)
            elif action == 'remove':
                self.get(value)
                self.db.execute('DELETE FROM playlist_tracks WHERE playlist=? AND key=?', (pid, value))
            else:
                raise PlayerError('INVALID_INPUT')

    def close(self):
        with self.lock:
            self.db.close()
