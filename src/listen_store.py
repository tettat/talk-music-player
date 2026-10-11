"""Cumulative, idempotent browser playback telemetry in the existing local SQLite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import re
import time

from library import PlayerError

TZ = timezone(timedelta(hours=8))
SESSION = re.compile(r'[a-f0-9]{32}\Z')
REASONS = {'playing', 'pause', 'seek', 'ended', 'skip', 'close'}
FINAL = {'ended', 'skip', 'close'}


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise PlayerError('INVALID_LISTEN')
    return float(value)


def merge_segments(segments, duration):
    if not isinstance(segments, list) or len(segments) > 1000:
        raise PlayerError('INVALID_LISTEN')
    ranges = []
    for item in segments:
        if not isinstance(item, list) or len(item) != 2:
            raise PlayerError('INVALID_LISTEN')
        start, end = number(item[0], 0, duration + 2), number(item[1], 0, duration + 2)
        if end <= start:
            raise PlayerError('INVALID_LISTEN')
        start, end = min(start, duration), min(end, duration)
        if end > start:
            ranges.append([start, end])
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged, sum(end-start for start, end in merged)


class History:
    def __init__(self, library, clock=time.time):
        self.library, self.clock = library, clock
        with library.lock:
            library.db.executescript('''
              CREATE TABLE IF NOT EXISTS talk_listens (
                id TEXT PRIMARY KEY, key TEXT NOT NULL REFERENCES tracks(key),
                started REAL NOT NULL, updated REAL NOT NULL, seq INTEGER NOT NULL,
                seconds REAL NOT NULL, coverage REAL NOT NULL, segments TEXT NOT NULL,
                reason TEXT NOT NULL, qualified INTEGER NOT NULL, completed INTEGER NOT NULL);
              CREATE INDEX IF NOT EXISTS talk_listens_recent ON talk_listens(started DESC,id DESC);
              CREATE TABLE IF NOT EXISTS talk_listen_days (
                id TEXT NOT NULL REFERENCES talk_listens(id), day TEXT NOT NULL,
                seconds REAL NOT NULL, plays INTEGER NOT NULL, completed INTEGER NOT NULL,
                skipped INTEGER NOT NULL, PRIMARY KEY(id,day));
              CREATE INDEX IF NOT EXISTS talk_listen_day ON talk_listen_days(day);
            ''')

    def heartbeat(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'id', 'key', 'seq', 'seconds', 'segments', 'reason'}:
            raise PlayerError('INVALID_LISTEN')
        sid, key, seq, reason = (payload[name] for name in ('id', 'key', 'seq', 'reason'))
        if (not isinstance(sid, str) or not SESSION.fullmatch(sid) or type(seq) is not int or
                not 1 <= seq <= 1_000_000 or not isinstance(reason, str) or reason not in REASONS):
            raise PlayerError('INVALID_LISTEN')
        seconds = number(payload['seconds'], 0, 86400)
        with self.library.lock:
            track = self.library.get(key)
            segments, heard = merge_segments(payload['segments'], track['duration'])
            # No raw position-as-listening-time; still a client observation, not an anti-fraud guarantee.
            if heard > seconds * 4 + .1:
                raise PlayerError('INVALID_LISTEN')
            now = self.clock()
            previous = self.library.db.execute('SELECT key,updated,seq,seconds,reason,qualified,completed,coverage '
                                               'FROM talk_listens WHERE id=?', (sid,)).fetchone()
            if previous:
                old_key, updated, old_seq, old_seconds, old_reason, qualified, completed, old_coverage = previous
                if old_key != key:
                    raise PlayerError('INVALID_LISTEN')
                if seq <= old_seq:
                    return {'ok': True, 'duplicate': True}
                if old_reason in FINAL:
                    raise PlayerError('LISTEN_CLOSED', 409)
                delta = seconds-old_seconds
                if (delta < -.0001 or delta > min(30, max(0, now-updated)+2) + .001 or
                        heard + .01 < old_coverage * track['duration']):
                    raise PlayerError('INVALID_LISTEN')
            else:
                if seconds > 15:
                    raise PlayerError('INVALID_LISTEN')
                delta, qualified, completed = seconds, False, False
            delta = max(0, delta)
            coverage = min(1, heard / track['duration'])
            new_qualified = seconds >= min(30, track['duration']*.5)
            new_completed = coverage >= .9
            day = datetime.fromtimestamp(now, TZ).date().isoformat()
            db = self.library.db
            db.execute('SAVEPOINT talk_listen_write')
            try:
                if previous:
                    db.execute('''UPDATE talk_listens SET updated=?,seq=?,seconds=?,coverage=?,segments=?,
                        reason=?,qualified=?,completed=? WHERE id=?''',
                        (now, seq, seconds, coverage, json.dumps(segments), reason,
                         int(new_qualified), int(new_completed), sid))
                else:
                    db.execute('INSERT INTO talk_listens VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (sid, key, now, now, seq, seconds, coverage, json.dumps(segments), reason,
                         int(new_qualified), int(new_completed)))
                db.execute('''INSERT INTO talk_listen_days VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id,day) DO UPDATE SET seconds=seconds+excluded.seconds,
                    plays=plays+excluded.plays,completed=completed+excluded.completed,
                    skipped=skipped+excluded.skipped''',
                    (sid, day, delta, int(new_qualified and not qualified),
                     int(new_completed and not completed), int(reason == 'skip' and seconds > 0)))
                if seconds > 0:
                    db.execute('UPDATE tracks SET used=coalesce(used,?) WHERE key=?', (now, key))
                db.execute('RELEASE talk_listen_write')
            except Exception:
                db.execute('ROLLBACK TO talk_listen_write'); db.execute('RELEASE talk_listen_write')
                raise
        return {'ok': True, 'seconds': round(seconds, 4), 'coverage': round(coverage, 4),
                'qualified': bool(new_qualified), 'completed': bool(new_completed)}

    def recent(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise PlayerError('INVALID_INPUT')
        with self.library.lock:
            rows = self.library.db.execute('''SELECT id,key,started,updated,seconds,coverage,qualified,completed,reason
                FROM talk_listens WHERE seconds>0 ORDER BY started DESC,id DESC LIMIT ? OFFSET ?''',
                (limit+1, offset)).fetchall()
            items = [{'id': row[0], 'track': self.library.get(row[1]), 'startedAt': row[2], 'updatedAt': row[3],
                      'seconds': round(row[4], 4), 'coverage': round(row[5], 4), 'qualified': bool(row[6]),
                      'completed': bool(row[7]), 'reason': row[8]} for row in rows[:limit]]
        return {'items': items, 'hasMore': len(rows) > limit}

    def summary(self, days=7):
        if type(days) is not int or days not in (0, 7, 30):
            raise PlayerError('INVALID_INPUT')
        today = datetime.fromtimestamp(self.clock(), TZ).date()
        since = (today-timedelta(days=days-1)).isoformat() if days else '0001-01-01'
        with self.library.lock:
            daily = self.library.db.execute('''SELECT day,sum(seconds),sum(plays),sum(completed),sum(skipped)
                FROM talk_listen_days WHERE day>=? GROUP BY day ORDER BY day''', (since,)).fetchall()
            rows = self.library.db.execute('''SELECT l.key,sum(d.seconds),sum(d.plays)
                FROM talk_listen_days d JOIN talk_listens l ON l.id=d.id
                WHERE d.day>=? GROUP BY l.key HAVING sum(d.seconds)>0''', (since,)).fetchall()
            tracks = [{'track': self.library.get(key), 'seconds': round(seconds, 3), 'plays': plays}
                      for key, seconds, plays in rows]
        artists = {}
        for item in tracks:
            name = item['track']['artist']
            record = artists.setdefault(name, {'artist': name, 'seconds': 0, 'plays': 0})
            record['seconds'] += item['seconds']; record['plays'] += item['plays']
        points = {day: {'date': day, 'seconds': round(seconds, 3), 'plays': plays}
                  for day, seconds, plays, _, _ in daily}
        count = days or 30
        chart = [points.get((today-timedelta(days=i)).isoformat(),
                           {'date': (today-timedelta(days=i)).isoformat(), 'seconds': 0, 'plays': 0})
                 for i in reversed(range(count))]
        return {'days': days, 'timezone': 'Asia/Shanghai',
                'totalSeconds': round(sum(row[1] for row in daily), 3), 'playCount': sum(row[2] for row in daily),
                'songCount': len(tracks), 'completedCount': sum(row[3] for row in daily),
                'skippedCount': sum(row[4] for row in daily), 'daily': chart,
                'topTracks': sorted(tracks, key=lambda row: (-row['seconds'], row['track']['key']))[:10],
                'topArtists': sorted(artists.values(), key=lambda row: (-row['seconds'], row['artist']))[:10]}
