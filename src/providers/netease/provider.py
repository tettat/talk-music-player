"""Search and play only ordinary, full-length free NetEase standard MP3 sources."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

from library import ART, PlayerError, clean_track
from providers.qq.member_source import inspect_mp3, decoded_metadata
from . import transport

MAX_BYTES = 24 * 1024**2


def media_url(value):
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError('NETEASE_SOURCE_HOST_REJECTED')
    u = urllib.parse.urlsplit(value)
    host = (u.hostname or '').lower()
    if (u.scheme not in ('http', 'https') or not host.endswith('.music.126.net')
            or not re.fullmatch(r'[a-z0-9.-]+', host) or u.username is not None or u.password is not None
            or u.port not in (None, 443 if u.scheme == 'https' else 80) or u.fragment or not u.path.startswith('/')):
        raise ValueError('NETEASE_SOURCE_HOST_REJECTED')
    return urllib.parse.urlunsplit(('https', host, u.path, u.query, ''))


def metadata(row, privilege=None):
    privilege = privilege or {}
    album = row.get('al') or row.get('album') or {}
    artists = row.get('ar') or row.get('artists') or []
    duration = row.get('dt', row.get('duration'))
    if type(duration) is not int:
        raise PlayerError('INVALID_TRACK')
    fee = row.get('fee', privilege.get('fee', 0))
    availability = 'unavailable' if privilege.get('st', 0) < 0 else 'requires_login' if fee not in (0, 8) else 'free'
    art = ''
    url = album.get('picUrl')
    if isinstance(url, str):
        u = urllib.parse.urlsplit(url)
        if (u.scheme in ('http', 'https') and (u.hostname or '').endswith('.music.126.net')
                and ART.fullmatch(u.path)):
            art = u.path  # Only an opaque public artwork path; never signed audio URLs.
    return clean_track({'source': 'netease', 'songId': row.get('id'), 'title': row.get('name'),
                        'artist': ' / '.join(a.get('name', '') for a in artists),
                        'album': album.get('name'), 'duration': max(1, round(duration / 1000)),
                        'albumArt': art, 'availability': availability})


def source_record(result, track):
    rows = [row for row in result.get('data', []) if row.get('id') == track['songId']]
    if result.get('code') != 200 or len(rows) != 1:
        raise PlayerError('NETEASE_SOURCE_UNAVAILABLE', 422)
    row = rows[0]
    if (row.get('code') != 200 or row.get('freeTrialInfo') is not None or row.get('fee') not in (0, 8)
            or row.get('type') != 'mp3' or type(row.get('br')) is not int or not 0 < row['br'] <= 128000
            or type(row.get('size')) is not int or not 0 < row['size'] <= MAX_BYTES
            or type(row.get('time')) is not int or abs(row['time']/1000 - track['duration']) > 2):
        raise PlayerError('NETEASE_SOURCE_UNAVAILABLE', 422)
    privilege = row.get('freeTrialPrivilege') or {}
    if privilege.get('cannotListenReason') not in (None, 0) or privilege.get('listenType') not in (None, 0):
        raise PlayerError('NETEASE_SOURCE_UNAVAILABLE', 422)
    try:
        url = media_url(row.get('url'))
    except ValueError:
        raise PlayerError('NETEASE_SOURCE_UNAVAILABLE', 422) from None
    return {**row, 'url': url}  # Private, transient only.


def download(row):
    req = urllib.request.Request(media_url(row['url']), headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://music.163.com/', 'Accept-Encoding': 'identity'})
    parts, count, started = [], 0, time.monotonic()
    with urllib.request.build_opener(transport.NoRedirect()).open(req, timeout=12) as response:
        declared = response.headers.get('Content-Length')
        if (response.status != 200 or declared is not None and (not declared.isdecimal() or int(declared) != row['size'])
                or response.headers.get('Content-Encoding', 'identity') != 'identity'):
            raise ValueError('NETEASE_SOURCE_RESPONSE_REJECTED')
        while True:
            chunk = getattr(response, 'read1', response.read)(65536)
            if not chunk:
                break
            count += len(chunk)
            if count > min(MAX_BYTES, row['size']) or time.monotonic() - started > 45:
                raise ValueError('NETEASE_SOURCE_LIMIT')
            parts.append(chunk)
    body = b''.join(parts)
    if len(body) != row['size']:
        raise ValueError('NETEASE_SOURCE_INCOMPLETE')
    md5 = row.get('md5')
    if md5 and (not isinstance(md5, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', md5)
                or hashlib.md5(body).hexdigest() != md5.lower()):
        raise ValueError('NETEASE_SOURCE_INTEGRITY')
    return body


class NetEase:
    id = 'netease'
    name = '网易云音乐'
    capabilities = ['search', 'play', 'lyrics', 'free-only']

    def __init__(self, private):
        self.cache = Path(private) / 'netease' / 'audio'
        self.cache.mkdir(exist_ok=True, parents=True)

    def public(self):
        return {'session': 'free', 'loginAvailable': False, 'memberPlaybackVerified': False}

    def search(self, query, page):
        result = transport.request('/api/cloudsearch/pc', {'s': query, 'type': 1, 'limit': 30,
                                                          'offset': (page-1)*30, 'total': True})
        if result.get('code') != 200:
            raise PlayerError('NETEASE_UNAVAILABLE', 502)
        rows = result.get('result', {}).get('songs', [])
        tracks, seen = [], set()
        for row in rows[:30]:
            try:
                track = metadata(row, row.get('privilege'))
            except (PlayerError, ValueError, TypeError, AttributeError):
                continue
            if track['key'] not in seen:
                tracks.append(track); seen.add(track['key'])
        total = result.get('result', {}).get('songCount', 0)
        return {'tracks': tracks, 'hasMore': type(total) is int and total > page*30 and page < 10}

    def detail(self, selected):
        result = transport.request('/api/v3/song/detail', {'c': json.dumps([{'id': selected['songId']}])})
        songs = result.get('songs', [])
        if result.get('code') != 200 or len(songs) != 1:
            raise PlayerError('TRACK_IDENTITY_MISMATCH', 502)
        actual = metadata(songs[0], next(iter(result.get('privileges') or [{}])))
        if any(actual[field] != selected[field] for field in ('key', 'songId', 'title', 'artist', 'duration')):
            raise PlayerError('TRACK_IDENTITY_MISMATCH', 502)
        return actual

    def source(self, selected):
        actual = self.detail(selected)
        if actual['availability'] == 'requires_login':
            raise PlayerError('NETEASE_FREE_ONLY', 422)
        if actual['availability'] == 'unavailable':
            raise PlayerError('NETEASE_SOURCE_UNAVAILABLE', 422)
        # Always recheck first-party availability even if there is a validated cache.
        row = source_record(transport.request('/api/song/enhance/player/url',
                                             {'ids': json.dumps([actual['songId']]), 'br': 128000}), actual)
        path = self.cache / (str(actual['songId']) + '.mp3')
        manifest = path.with_suffix('.json')
        media = None
        if path.is_file() and path.stat().st_size == row['size'] and manifest.is_file() and manifest.stat().st_size <= 8192:
            try:
                saved = json.loads(manifest.read_text(encoding='utf-8'))
                body = path.read_bytes()
                if (saved['sha256'] == hashlib.sha256(body).hexdigest() and saved.get('decoder') == 'miniaudio-1.71'
                        and (not row.get('md5') or hashlib.md5(body).hexdigest() == row['md5'].lower())
                        and saved.get('decoderVerified') and saved.get('nonSilentPcmObserved')
                        and saved.get('completeMp3FrameCoverage')
                        and abs(saved.get('decodedDurationSeconds', 0)-actual['duration']) <= 2):
                    inspect_mp3(body, actual['duration'])
                    media = saved
            except Exception:
                media = None
        if media is None:
            body = download(row)
            media = inspect_mp3(body, actual['duration'])
            if importlib.metadata.version('miniaudio') != '1.71':
                raise PlayerError('NETEASE_SOURCE_UNAVAILABLE', 422)
            import miniaudio
            decoded = miniaudio.decode(body, sample_rate=22050, nchannels=1)
            media.update(decoded_metadata(decoded, actual['duration']))
            media.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body),
                         provider='netease', signedUrlExported=False, cacheStoredPrivately=True)
            tmp = path.with_suffix('.part')
            try:
                tmp.write_bytes(body); os.replace(tmp, path)
                tmp = manifest.with_suffix('.tmp')
                tmp.write_text(json.dumps(media), encoding='utf-8'); os.replace(tmp, manifest)
            finally:
                tmp.unlink(missing_ok=True)
        path.touch()
        total = 0
        for index, item in enumerate(sorted(self.cache.glob('*.mp3'), key=lambda p: p.stat().st_mtime, reverse=True)):
            total += item.stat().st_size
            if item != path and (index >= 12 or total > 150*1024**2):
                item.unlink(missing_ok=True); item.with_suffix('.json').unlink(missing_ok=True)
        return {'track': actual, 'path': path, 'media': media}

    def run(self, operation, **args):
        try:
            if operation == 'search':
                return self.search(args['query'], args.get('page', 1))
            if operation == 'source':
                return self.source(args['track'])
            if operation == 'lyrics':
                track = self.detail(args['track'])
                result = transport.request('/api/song/lyric', {'id': track['songId'], 'lv': -1, 'tv': -1, 'rv': -1, 'kv': -1})
                if result.get('code') != 200:
                    raise PlayerError('LYRICS_UNAVAILABLE', 502)
                lyric, translation = (result.get('lrc') or {}).get('lyric', ''), (result.get('tlyric') or {}).get('lyric', '')
                if any(not isinstance(s, str) or len(s.encode('utf-8')) > 256*1024 for s in (lyric, translation)):
                    raise PlayerError('LYRICS_INVALID', 502)
                return {'lyric': lyric, 'translation': translation, 'songId': track['songId']}
            if operation == 'status':
                return self.public()
            raise PlayerError('CAPABILITY_UNAVAILABLE', 422)
        except PlayerError:
            raise
        except TimeoutError:
            raise PlayerError('NETWORK_TIMEOUT', 504) from None
        except Exception:
            raise PlayerError('NETEASE_UNAVAILABLE', 502) from None
