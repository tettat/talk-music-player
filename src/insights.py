"""Private local lyric/analysis cache. No credentials or remote audio inference."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from library import PlayerError


class InsightStore:
    def __init__(self, library, clock=time.time):
        self.library, self.clock = library, clock
        with library.lock:
            library.db.executescript('''
              CREATE TABLE IF NOT EXISTS talk_lyrics (
                key TEXT PRIMARY KEY REFERENCES tracks(key), updated REAL NOT NULL,
                data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS talk_analysis (
                key TEXT PRIMARY KEY REFERENCES tracks(key), updated REAL NOT NULL,
                data TEXT NOT NULL);
            ''')

    def get(self, kind, key):
        key = self.library.get(key)['key']
        table = {'lyrics': 'talk_lyrics', 'analysis': 'talk_analysis'}.get(kind)
        if table is None:
            raise PlayerError('INVALID_INPUT')
        with self.library.lock:
            row = self.library.db.execute('SELECT data FROM ' + table + ' WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, value):
        track = self.library.get(key)
        key = track['key']
        table = {'lyrics': 'talk_lyrics', 'analysis': 'talk_analysis'}.get(kind)
        if table is None or not isinstance(value, dict):
            raise PlayerError('INVALID_INPUT')
        value = {**value, 'key': key, 'source': track['source'], 'updatedAt': self.clock()}
        try:
            data = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise PlayerError('INVALID_INPUT') from None
        if len(data.encode('utf-8')) > 2 * 1024**2:
            raise PlayerError('RESULT_LIMIT', 422)
        with self.library.lock:
            self.library.db.execute('INSERT INTO ' + table + '''(key,updated,data) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET updated=excluded.updated,data=excluded.data''',
                (key, value['updatedAt'], data))
        return value


def lyric_document(track, original, translation=None):
    """The SDK already handles its lyric transport encoding; this is plain lyric text."""
    from lyric_parser import normalize_lyrics
    parsed = normalize_lyrics(original, track['duration'])
    translated = normalize_lyrics(translation or '', track['duration'])
    digest = hashlib.sha256((parsed['lrc'] + '\0' + translated['lrc']).encode('utf-8')).hexdigest()
    return {**parsed, 'translation': translated, 'source': track.get('source', 'qq'),
            'songId': track['songId'], 'sha256': digest,
            'available': bool(parsed['lines'])}


HEX = re.compile(r'[a-f0-9]{64}\Z')


def finite(value, lower, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise PlayerError('ANALYSIS_INVALID', 502)
    return value


def note_name(midi):
    midi = round(midi)
    return ('C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B')[midi % 12] + str(midi // 12 - 1)


def pitch_document(raw, *, expected_sha, start, seconds):
    """Narrow public result schema: do not forward arbitrary child output to the browser."""
    if not isinstance(raw, dict) or raw.get('schemaVersion') != 1:
        raise PlayerError('ANALYSIS_INVALID', 502)
    model, audio, notes = raw.get('model', {}), raw.get('audio', {}), raw.get('notes')
    if (not isinstance(model, dict) or not isinstance(audio, dict) or
            model.get('id') not in ('spotify/basic-pitch', 'basic-pitch') or
            not isinstance(model.get('sha256'), str) or not HEX.fullmatch(model['sha256']) or
            audio.get('sha256') != expected_sha or not isinstance(notes, list) or len(notes) > 2000):
        raise PlayerError('ANALYSIS_INVALID', 502)
    if abs(finite(audio.get('start'), 0, 1800) - start) > .05:
        raise PlayerError('ANALYSIS_INVALID', 502)
    clip_seconds = finite(audio.get('seconds'), .1, 60)
    if abs(clip_seconds - seconds) > .1 or audio.get('sampleRate') != 22050:
        raise PlayerError('ANALYSIS_INVALID', 502)
    clean = []
    for note in notes:
        if not isinstance(note, dict):
            raise PlayerError('ANALYSIS_INVALID', 502)
        begin = finite(note.get('start'), start - .05, start + clip_seconds + .1)
        end = finite(note.get('end'), begin, start + clip_seconds + .15)
        if end <= begin:
            raise PlayerError('ANALYSIS_INVALID', 502)
        clean.append({'start': round(begin, 3), 'end': round(end, 3),
                      'midi': finite(note.get('midi'), 0, 127),
                      'confidence': round(finite(note.get('confidence'), 0, 1), 4)})
    clean.sort(key=lambda n: (n['start'], n['midi']))
    values = [n['midi'] for n in clean]
    metrics = [{'id': 'noteCount', 'label': '音符候选', 'value': len(clean), 'unit': ''},
               {'id': 'density', 'label': '音符密度', 'value': round(len(clean) / clip_seconds, 1), 'unit': '/秒'}]
    if values:
        metrics.append({'id': 'range', 'label': '候选音域',
                        'value': note_name(min(values)) + ' – ' + note_name(max(values)), 'unit': ''})
    summary = raw.get('summary', {})
    if isinstance(summary, dict):
        for field, label, lower, upper, unit in [
                ('bpmEstimate', '估计速度', 0, 400, 'BPM'),
                ('spectralCentroidHz', '频谱重心', 0, 11025, 'Hz'),
                ('rmsDb', '片段能量', -240, 24, 'dBFS')]:
            if field in summary:
                value = finite(summary[field], lower, upper)
                metrics.append({'id': field, 'label': label, 'value': round(value, 1), 'unit': unit})
    return {'kind': 'pitch', 'estimated': True,
            'model': {'id': 'spotify/basic-pitch', 'version': str(model.get('version', ''))[:64],
                      'sha256': model['sha256'], 'license': 'Apache-2.0'},
            'audio': {'sha256': expected_sha, 'start': start, 'seconds': clip_seconds, 'sampleRate': 22050},
            'metrics': metrics, 'notes': clean, 'tags': []}
