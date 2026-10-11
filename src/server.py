"""Loopback-only player server; no official GUI, services, or remote storage."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))  # One fixed owned directory, including under Python -I.
from library import Library, PlayerError, text, valid_mid
from bridge import Bridge
from providers import ProviderRegistry
from settings import SETTINGS
from listen_store import History
from insights import InsightStore, lyric_document
import analysis_runner

ROOT = SRC.parent
WEB = ROOT / 'web'
PRIVATE = SETTINGS.data_dir


def byte_range(value, size):
    if value is None:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
    if not match or not any(match.groups()):
        raise PlayerError('INVALID_RANGE', 416)
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise PlayerError('INVALID_RANGE', 416)
        return max(0, size - suffix), size - 1, True
    start, end = int(first), int(last) if last else size - 1
    if start >= size or end < start:
        raise PlayerError('INVALID_RANGE', 416)
    return start, min(end, size - 1), True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Player:
    def __init__(self, private=PRIVATE, live=True, web=None):
        self.private = Path(private)
        self.private.mkdir(parents=True, exist_ok=True)
        self.library = Library(self.private / 'library.sqlite3')
        self.history = History(self.library)
        self.insights = InsightStore(self.library)
        self.web = Path(web) if web is not None else WEB
        self.bridge = Bridge(self.private) if live else None
        self.providers = ProviderRegistry([self.bridge] if live else [])
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.network = threading.Lock()
        self.jobs = {}
        self.extra_jobs = {}
        self.analysis_slot = threading.Lock()
        self.media = {}
        self.auth_thread = None
        self.cover_slots = threading.BoundedSemaphore(3)
        self.covers = self.private / 'covers'
        self.covers.mkdir(exist_ok=True)
        self.instance = secrets.token_hex(16)

    def seed(self):
        # A fresh checkout starts empty; never publish one user's prototype library.
        return None

    @contextmanager
    def network_action(self):
        if not self.network.acquire(blocking=False):
            raise PlayerError('BUSY', 409)
        try:
            yield
        finally:
            self.network.release()

    def snapshot(self):
        return {**self.library.snapshot(), 'account': self.bridge.public()}

    def search(self, query, page):
        query = text(query, 120)
        if type(page) is not int or not 1 <= page <= 10:
            raise PlayerError('INVALID_INPUT')
        with self.network_action():
            result = self.bridge.run('search', query=query, page=page)
            result['tracks'] = self.library.put(result['tracks'])
        return result

    def play(self, key):
        track = self.library.get(key)
        if not self.network.acquire(blocking=False):
            raise PlayerError('BUSY', 409)
        with self.lock:
            for old_id, old in list(self.jobs.items()):
                if time.time() - old['created'] > 600:
                    self.jobs.pop(old_id, None)
            if len(self.jobs) >= 16:
                oldest = next(iter(self.jobs))
                self.jobs.pop(oldest)
            jid = secrets.token_hex(16)
            self.jobs[jid] = {'id': jid, 'status': 'loading', 'created': time.time(), 'key': key}

        def task():
            try:
                result = self.bridge.run('source', track=track)
                self.library.put([result['track']], used=True)
                with self.lock:
                    token = secrets.token_urlsafe(32)
                    self.media = {t: row for t, row in self.media.items()
                                  if row['expires'] > time.time() and row['path'].is_file()}
                    if len(self.media) >= 24:
                        self.media.pop(next(iter(self.media)))
                    self.media[token] = {'path': result['path'], 'expires': time.time() + 12 * 3600}
                    self.jobs[jid].update(status='ready', track=result['track'],
                                          mediaUrl='/media/' + token,
                                          source={'format': 'mp3', 'quality': '128',
                                                  'fullDecodeVerified': bool(
                                                      result['media'].get('decoderVerified') and
                                                      result['media'].get('completeMp3FrameCoverage') and
                                                      result['media'].get('nonSilentPcmObserved'))})
            except PlayerError as exc:
                with self.lock:
                    self.jobs[jid].update(status='error', code=exc.code)
            except Exception:
                with self.lock:
                    self.jobs[jid].update(status='error', code='SOURCE_UNAVAILABLE')
            finally:
                self.network.release()

        threading.Thread(target=task, daemon=True, name='talk-source').start()
        return {'job': jid}

    def login(self, provider):
        if provider not in ('qq', 'wx', 'mobile'):
            raise PlayerError('INVALID_INPUT')
        if not self.network.acquire(blocking=False):
            raise PlayerError('BUSY', 409)
        self.bridge.auth_status, self.bridge.qr = 'creating', None

        def task():
            try:
                asyncio.run(asyncio.wait_for(self.bridge.login(provider), timeout=110))
                if self.bridge.auth_status == 'done':
                    # A new account must not inherit old in-memory media grants.
                    with self.lock:
                        self.media.clear()
                        self.jobs.clear()
            except Exception:
                self.bridge.qr = None
                self.bridge.auth_status = 'failed'
            finally:
                self.network.release()

        self.auth_thread = threading.Thread(target=task, daemon=True, name='talk-auth')
        self.auth_thread.start()
        return {'started': True}

    def extra_job(self, kind, key):
        with self.lock:
            for job in self.extra_jobs.values():
                if job['kind'] == kind and job['key'] == key and job['status'] == 'loading':
                    return job, False
            self.extra_jobs = {jid: row for jid, row in self.extra_jobs.items()
                               if row['status'] == 'loading' or time.time()-row['created'] < 600}
            if len(self.extra_jobs) >= 16:
                finished = next((jid for jid, row in self.extra_jobs.items() if row['status'] != 'loading'), None)
                if finished is None:
                    raise PlayerError('BUSY', 409)
                self.extra_jobs.pop(finished)
            job = {'id': secrets.token_hex(16), 'kind': kind, 'key': key,
                   'status': 'loading', 'created': time.time()}
            self.extra_jobs[job['id']] = job
            return job, True

    def lyrics(self, key, refresh=False):
        track = self.library.get(key)
        if type(refresh) is not bool:
            raise PlayerError('INVALID_INPUT')
        cached = self.insights.get('lyrics', key)
        if cached and not refresh and time.time()-cached['updatedAt'] < 7*86400:
            return {'status': 'ready', 'lyrics': cached}, 200
        with self.lock:
            for job in self.extra_jobs.values():
                if job['kind'] == 'lyrics' and job['key'] == key and job['status'] == 'loading':
                    return {'job': job['id'], 'status': 'loading'}, 202
        if not self.network.acquire(blocking=False):
            raise PlayerError('BUSY', 409)
        try:
            job, _ = self.extra_job('lyrics', key)
        except Exception:
            self.network.release(); raise
        def task():
            try:
                result = self.bridge.run('lyrics', track=track)
                parsed = lyric_document(track, result['lyric'], result.get('translation'))
                saved = self.insights.put('lyrics', key, parsed)
                with self.lock:
                    job.update(status='ready', lyrics=saved)
            except PlayerError as error:
                with self.lock: job.update(status='error', code=error.code)
            except Exception:
                with self.lock: job.update(status='error', code='LYRICS_UNAVAILABLE')
            finally:
                self.network.release()
        threading.Thread(target=task, daemon=True, name='talk-lyrics').start()
        return {'job': job['id'], 'status': 'loading'}, 202

    def analyze(self, key, refresh=False):
        track = self.library.get(key)
        if type(refresh) is not bool:
            raise PlayerError('INVALID_INPUT')
        cached = self.insights.get('analysis', key)
        if cached and not refresh:
            return {'status': 'ready', 'analysis': cached}, 200
        with self.lock:
            for job in self.extra_jobs.values():
                if job['kind'] == 'analysis' and job['key'] == key and job['status'] == 'loading':
                    return {'job': job['id'], 'status': 'loading'}, 202
        if not analysis_runner.ready():
            raise PlayerError('ANALYSIS_UNAVAILABLE', 503)
        if not self.analysis_slot.acquire(blocking=False):
            raise PlayerError('ANALYSIS_BUSY', 409)
        if not self.network.acquire(blocking=False):
            self.analysis_slot.release()
            raise PlayerError('BUSY', 409)
        try:
            job, _ = self.extra_job('analysis', key)
        except Exception:
            self.network.release(); self.analysis_slot.release(); raise
        def task():
            network_held = True
            try:
                with analysis_runner.work_directory(self.private) as workspace:
                    authorized = self.bridge.run('source', track=track)
                    digest = authorized['media']['sha256']
                    copied = analysis_runner.copy_authorized(authorized['path'], workspace, digest)
                    # Inference cannot block normal playback/search/lyric authorization.
                    self.network.release(); network_held = False
                    result = analysis_runner.analyze(copied, workspace, track, digest)
                    saved = self.insights.put('analysis', key, result)
                with self.lock: job.update(status='ready', analysis=saved)
            except PlayerError as error:
                with self.lock: job.update(status='error', code=error.code)
            except Exception:
                with self.lock: job.update(status='error', code='ANALYSIS_FAILED')
            finally:
                if network_held: self.network.release()
                self.analysis_slot.release()
        threading.Thread(target=task, daemon=True, name='talk-analysis').start()
        return {'job': job['id'], 'status': 'loading'}, 202

    def cover(self, mid):
        if not valid_mid(mid):
            raise PlayerError('NOT_FOUND', 404)
        target = self.covers / (mid + '.jpg')
        if target.is_file() and target.stat().st_size <= 1024**2:
            return target.read_bytes(), 'image/jpeg'
        if not self.cover_slots.acquire(blocking=False):
            return (WEB / 'assets/cover-default.svg').read_bytes(), 'image/svg+xml'
        try:
            opener = urllib.request.build_opener(NoRedirect())
            request = urllib.request.Request(
                'https://y.gtimg.cn/music/photo_new/T002R300x300M000' + mid + '.jpg',
                headers={'User-Agent': 'talk-local/1.0'})
            with opener.open(request, timeout=8) as response:
                if response.status != 200:
                    raise ValueError()
                data = response.read(1024**2 + 1)
                if len(data) > 1024**2 or not data.startswith(b'\xff\xd8\xff'):
                    raise ValueError()
            # Bounded cover cache, exclusively public album art; no provider session headers.
            temp = target.with_suffix('.' + secrets.token_hex(4) + '.tmp')
            try:
                temp.write_bytes(data)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            old = sorted(self.covers.glob('*.jpg'), key=lambda p: p.stat().st_mtime, reverse=True)
            for item in old[400:]:
                item.unlink(missing_ok=True)
            return data, 'image/jpeg'
        except Exception:
            return (WEB / 'assets/cover-default.svg').read_bytes(), 'image/svg+xml'
        finally:
            self.cover_slots.release()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, player):
        self.player = player
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(60)
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never log request strings or exception messages (may contain private state).


class Handler(BaseHTTPRequestHandler):
    server_version = 'talk'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    @property
    def app(self):
        return self.server.player

    def log_message(self, *args):
        pass

    def check_request(self, mutation=False):
        expected = '127.0.0.1:' + str(self.server.server_port)
        if self.headers.get_all('Host') != [expected]:
            raise PlayerError('HOST_REJECTED', 403)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + expected:
            raise PlayerError('ORIGIN_REJECTED', 403)
        if self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin'):
            raise PlayerError('ORIGIN_REJECTED', 403)
        if mutation and not secrets.compare_digest(self.headers.get('X-Talk-CSRF', ''), self.app.csrf):
            raise PlayerError('CSRF_REQUIRED', 403)

    def headers_out(self, code, kind, length, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "img-src 'self'; media-src 'self'; connect-src 'self'; "
                         "font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def payload(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.headers_out(status, 'application/json; charset=utf-8', len(body))
        if self.command != 'HEAD':
            self.wfile.write(body)

    def body(self):
        if self.headers.get('Transfer-Encoding'):
            raise PlayerError('INVALID_INPUT')
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise PlayerError('JSON_REQUIRED', 415)
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not lengths[0].isdecimal() or not 0 < int(lengths[0]) <= 128 * 1024:
            raise PlayerError('BODY_LIMIT', 413)
        try:
            data = json.loads(self.rfile.read(int(lengths[0])),
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise PlayerError('INVALID_INPUT') from None
        if not isinstance(data, dict):
            raise PlayerError('INVALID_INPUT')
        return data

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.check_request()
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/health':
                return self.payload({'ok': True, 'instance': self.app.instance})
            if path == '/api/library':
                return self.payload(self.app.snapshot())
            if path == '/api/features':
                return self.payload({'listening': True, 'lyrics': True, 'analysis': analysis_runner.ready()})
            if path == '/api/providers':
                return self.payload({'providers': self.app.providers.describe()})
            if path == '/api/stats':
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try: days = int(query.get('days', ['7'])[0])
                except (ValueError, TypeError): raise PlayerError('INVALID_INPUT') from None
                return self.payload(self.app.history.summary(days))
            if path == '/api/history':
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try: offset = int(query.get('offset', ['0'])[0])
                except (ValueError, TypeError): raise PlayerError('INVALID_INPUT') from None
                return self.payload(self.app.history.recent(offset=offset))
            match = re.fullmatch(r'/api/(lyrics|analysis)/([A-Za-z0-9]{10,32})(/export)?', path)
            if match:
                kind, key, export = match.groups()
                cached = self.app.insights.get(kind, key)
                if not export:
                    if not cached:
                        with self.app.lock:
                            pending = next((row for row in self.app.extra_jobs.values()
                                            if row['key'] == key and row['kind'] == kind and row['status'] == 'loading'), None)
                        if pending:
                            return self.payload({'status': 'loading', 'job': pending['id'], kind: None})
                    return self.payload({'status': 'ready' if cached else 'missing', kind: cached})
                if kind != 'lyrics' or not cached or not cached['available']:
                    raise PlayerError('LYRICS_NOT_FOUND', 404)
                data = cached['lrc'].encode('utf-8')
                extension = 'txt' if cached['format'] == 'text' else 'lrc'
                self.headers_out(200, 'text/plain; charset=utf-8', len(data),
                                 {'Content-Disposition': f'attachment; filename="talk-{key}.{extension}"'})
                if self.command != 'HEAD': self.wfile.write(data)
                return
            if path == '/api/account':
                return self.payload(self.app.bridge.public())
            if path == '/api/auth/qr':
                data = self.app.bridge.qr
                if not data:
                    raise PlayerError('QR_NOT_READY', 404)
                self.headers_out(200, 'image/png', len(data))
                if self.command != 'HEAD':
                    self.wfile.write(data)
                return
            if re.fullmatch(r'/api/jobs/[a-f0-9]{32}', path):
                with self.app.lock:
                    jid = path.rsplit('/', 1)[1]
                    row = self.app.jobs.get(jid) or self.app.extra_jobs.get(jid)
                    if row is None:
                        raise PlayerError('NOT_FOUND', 404)
                    return self.payload(dict(row))
            if path.startswith('/api/cover/'):
                data, kind = self.app.cover(path.removeprefix('/api/cover/'))
                self.headers_out(200, kind, len(data))
                if self.command != 'HEAD':
                    self.wfile.write(data)
                return
            if re.fullmatch(r'/media/[A-Za-z0-9_-]{43}', path):
                return self.stream(path.rsplit('/', 1)[1])
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/listening.mjs': ('listening.mjs', 'text/javascript; charset=utf-8'),
                     '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
                     '/assets/brand.svg': ('assets/brand.svg', 'image/svg+xml'),
                     '/assets/cover-default.svg': ('assets/cover-default.svg', 'image/svg+xml')}
            if path not in files:
                raise PlayerError('NOT_FOUND', 404)
            name, kind = files[path]
            data = (self.app.web / name).read_bytes()
            if name == 'index.html':
                data = data.replace(b'__CSRF__', self.app.csrf.encode('ascii'))
            self.headers_out(200, kind, len(data))
            if self.command != 'HEAD':
                self.wfile.write(data)
        except PlayerError as exc:
            self.payload({'code': exc.code}, exc.status)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            self.payload({'code': 'INTERNAL_ERROR'}, 500)

    def stream(self, token):
        with self.app.lock:
            row = self.app.media.get(token)
            if not row or row['expires'] <= time.time():
                raise PlayerError('MEDIA_EXPIRED', 410)
            path = row['path']
        if not path.is_file():
            raise PlayerError('MEDIA_EXPIRED', 410)
        size = path.stat().st_size
        try:
            start, end, partial = byte_range(self.headers.get('Range'), size)
        except PlayerError:
            self.headers_out(416, 'audio/mpeg', 0, {'Content-Range': 'bytes */' + str(size)})
            return
        headers = {'Accept-Ranges': 'bytes'}
        if partial:
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        with path.open('rb') as handle:
            self.headers_out(206 if partial else 200, 'audio/mpeg', end - start + 1, headers)
            if self.command == 'HEAD':
                return
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                block = handle.read(min(65536, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def do_POST(self):
        try:
            self.check_request(mutation=True)
            data = self.body()
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/search':
                return self.payload(self.app.search(data.get('query'), data.get('page', 1)))
            if path == '/api/play':
                return self.payload(self.app.play(data.get('key')), 202)
            if path == '/api/listen':
                return self.payload(self.app.history.heartbeat(data))
            if path == '/api/lyrics':
                result, status = self.app.lyrics(data.get('key'), data.get('refresh', False))
                return self.payload(result, status)
            if path == '/api/analyze':
                result, status = self.app.analyze(data.get('key'), data.get('refresh', False))
                return self.payload(result, status)
            if path == '/api/favorite':
                self.app.library.favorite(data.get('key'), data.get('enabled'))
            elif path == '/api/playlists':
                pid = self.app.library.create_playlist(data.get('name'))
                return self.payload({'id': pid, **self.app.snapshot()}, 201)
            elif path == '/api/playlist':
                self.app.library.playlist(data.get('id'), data.get('action'), data.get('value'))
            elif path == '/api/state':
                return self.payload(self.app.library.save_state(data))
            elif path == '/api/account/verify':
                with self.app.network_action():
                    return self.payload(self.app.bridge.run('status'))
            elif path == '/api/auth/start':
                return self.payload(self.app.login(data.get('provider')), 202)
            elif path == '/api/auth/cancel':
                self.app.bridge.auth_cancel = True
                self.app.bridge.qr = None
                return self.payload({'ok': True})
            elif path == '/api/close':
                if self.app.network.locked() or self.app.analysis_slot.locked():
                    raise PlayerError('BUSY', 409)
                self.payload({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                raise PlayerError('NOT_FOUND', 404)
            self.payload(self.app.snapshot())
        except PlayerError as exc:
            self.close_connection = True
            self.payload({'code': exc.code}, exc.status)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.payload({'code': 'INVALID_INPUT'}, 400)

    def unsupported(self):
        self.close_connection = True
        try:
            self.check_request()
            self.payload({'code': 'METHOD_NOT_ALLOWED'}, 405)
        except PlayerError as exc:
            self.payload({'code': exc.code}, exc.status)

    do_OPTIONS = do_PUT = do_DELETE = do_PATCH = do_TRACE = unsupported


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=SETTINGS.port)
    parser.add_argument('--ttl', type=int, default=SETTINGS.ttl_seconds)
    parser.add_argument('--desktop', action='store_true', help='Owned stdin lifetime; random loopback port')
    parser.add_argument('--qa', action='store_true', help='Separate owned QA library, never the user library')
    parser.add_argument('--preview', action='store_true', help='QA-only staged web assets')
    args = parser.parse_args()
    if (not (1024 <= args.port <= 65535 or args.desktop and args.port == 0) or
            not 60 <= args.ttl <= 172800):
        return 2
    if args.preview and not args.qa:
        return 2
    if args.desktop:
        from process_lifetime import own_process_tree
        own_process_tree()
    app = Player(PRIVATE / 'qa' if args.qa else PRIVATE, web=ROOT / 'web-next' if args.preview else WEB)
    app.seed()
    server = Server(('127.0.0.1', args.port), app)
    state = {'pid': os.getpid(), 'port': server.server_port,
             'url': f'http://127.0.0.1:{server.server_port}/', 'instance': app.instance,
             'automaticOfficialClientLaunch': False}
    timer = None
    if args.desktop:
        def watch_owner():
            # Electron owns this pipe. EOF, including an abrupt UI crash, stops us.
            while True:
                line = sys.stdin.buffer.readline(128)
                if not line or line.strip() == b'shutdown':
                    server.shutdown()
                    return
        threading.Thread(target=watch_owner, daemon=True, name='talk-desktop-owner').start()
        print(json.dumps({'event': 'ready', **state}), flush=True)
    else:
        ready = ROOT / ('artifacts/qa-player-ready.json' if args.qa else 'artifacts/player-ready.json')
        ready.parent.mkdir(exist_ok=True)
        ready.write_text(json.dumps(state), encoding='utf-8')
        timer = threading.Timer(args.ttl, server.shutdown)
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        if timer:
            timer.cancel()
        app.bridge.auth_cancel = True
        server.server_close()
        # In-flight daemon SDK workers must not race a deliberately closed SQLite connection.
        if not app.network.locked() and not app.analysis_slot.locked():
            app.library.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PlayerError as error:
        print(json.dumps({'code': error.code}), flush=True)
        raise SystemExit(1)
    except Exception:
        print('{"code":"PLAYER_START_FAILED"}', flush=True)
        raise SystemExit(1)
