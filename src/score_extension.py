"""Optional, same-origin score capability. No model imports on player startup.

The host grants a track identity, never a browser-supplied filesystem path.
Recognition has its own queue/data/job lifetime and never changes playback state.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capabilities.score_lab.common import (EXPORTS, MEDIA, MAX_UPLOAD, MAX_CLIP,
    atomic_json, child_file, load_config, public_sample, read_json, sample_dir, sha256)
from capabilities.score_lab.lifetime import DataLock, OwnedJob
from capabilities.score_lab.server import Lab, Handler as LabHandler, runtime_ready, ABCJS_SHA256
from library import PlayerError

PREFIX = '/extensions/score-lab/'
WEB = ROOT / 'capabilities/score_lab/web'
STATIC = {'': 'index.html', 'index.html': 'index.html', 'app.mjs': 'app.mjs',
          'timeline.mjs': 'timeline.mjs', 'editor.mjs': 'editor.mjs',
          'print.html': 'print.html', 'print.mjs': 'print.mjs',
          'print-layout.mjs': 'print-layout.mjs', 'print.css': 'print.css',
          'style.css': 'style.css', 'embedded.css': 'embedded.css'}


def clip_options(data):
    offset, seconds = data.get('offset', 0), data.get('seconds', 30)
    if seconds == 'full':
        seconds = None
        offset = 0
    if (type(offset) not in (int, float) or not math.isfinite(offset) or not 0 <= offset <= 86400 or
        seconds is not None and (type(seconds) not in (int, float) or
                                 not math.isfinite(seconds) or not 1 <= seconds <= MAX_CLIP)):
        raise PlayerError('INVALID_INPUT')
    return float(offset), float(seconds) if seconds is not None else None


class ManagedLab(Lab):
    """Reuse the standalone worker contract; add host grants, cancel and retry."""
    def __init__(self, config, player):
        self.player = player
        self.preparations = {}
        self.cancelled = set()
        self.active = None
        self.generations = {}
        self.pending_tasks = set()
        self.submit_lock = threading.RLock()
        super().__init__(config)

    def recover(self):
        super().recover()
        for info in self.samples():
            directory = sample_dir(self.config, info['id'])
            path = directory / 'edit-status.json'
            if path.is_file() and read_json(path, 32768).get('state') in ('queued', 'running'):
                atomic_json(path, {'state': 'failed', 'phase': 'interrupted', 'message': '编辑已中断，请重新保存。'})

    def submit_edit(self, sid, draft):
        from capabilities.score_lab.editing import validate_draft, read_current, inspect_abc, EditError, EditConflict
        with self.submit_lock:
            if self.closed.is_set() or self.jobs.full():
                raise PlayerError('SCORE_BUSY', 429)
            pending = any(task[0] == sid for task in self.pending_tasks)
            if pending or self.active == sid:
                raise PlayerError('SCORE_BUSY', 409)
            directory = sample_dir(self.config, sid)
            if public_sample(directory)['status']['state'] != 'completed':
                raise PlayerError('SCORE_NOT_READY', 409)
            try:
                draft = validate_draft(draft)
                current = read_current(self.config, sid)
                if draft['baseRevision'] != current['revision'] or draft['sourceFingerprint'] != current['sourceFingerprint']:
                    raise PlayerError('SCORE_EDIT_CONFLICT', 409)
                if inspect_abc(draft['abc'])['skeleton'] != inspect_abc(current['abc'])['skeleton']:
                    raise PlayerError('SCORE_EDIT_INVALID', 422)
            except EditConflict:
                raise PlayerError('SCORE_EDIT_CONFLICT', 409) from None
            except EditError:
                raise PlayerError('SCORE_EDIT_INVALID', 422) from None
            revision = secrets.token_hex(12)
            file = child_file(directory, 'edits', 'drafts', revision + '.json')
            atomic_json(file, draft)
            atomic_json(directory / 'edit-status.json', {'state': 'queued', 'phase': 'editing', 'revisionId': revision})
            generation = self.generations.get(sid, 0) + 1
            self.generations[sid] = generation
            self.cancelled.discard(sid)
            self.pending_tasks.add((sid, generation))
            self.jobs.put_nowait({'id': sid, 'kind': 'edit', 'draft': f'edits/drafts/{revision}.json',
                                 'revision': revision, 'generation': generation})
            return {'id': sid, 'revisionId': revision}

    def create(self, meta, request=None, prepare=None, input_file=None):
        with self.submit_lock:
            if self.closed.is_set() or self.jobs.full():
                raise PlayerError('SCORE_BUSY', 429)
            sid = secrets.token_hex(12)
            directory = sample_dir(self.config, sid)
            directory.mkdir(parents=True, exist_ok=False)
            atomic_json(directory / 'sample.json', {
                **meta, 'createdAt': datetime.now(timezone.utc).isoformat()})
            atomic_json(directory / 'request.json', request or {'trackKey': meta['trackKey'],
                        'offset': meta['offset'], 'seconds': meta['duration']})
            if input_file:
                Path(input_file).replace(child_file(directory, request['input']))
            if prepare:
                self.preparations[sid] = prepare
            atomic_json(directory / 'status.json', {'state': 'queued', 'phase': 'queued'})
            self.generations[sid] = 1
            self.pending_tasks.add((sid, 1))
            self.jobs.put_nowait({'id': sid, 'kind': 'recognize', 'generation': 1})
            return sid

    def retry(self, sid):
        with self.submit_lock:
            directory = sample_dir(self.config, sid)
            info = public_sample(directory)
            if info['status']['state'] not in ('failed', 'cancelled'):
                raise PlayerError('SCORE_NOT_RETRYABLE', 409)
            if self.jobs.full():
                raise PlayerError('SCORE_BUSY', 429)
            if self.active == sid:
                raise PlayerError('SCORE_BUSY', 409)
            request = read_json(directory / 'request.json', 32768)
            if not request.get('input') or not child_file(directory, request['input']).is_file():
                meta = read_json(directory / 'sample.json', 32768)
                key = meta.get('trackKey')
                if not key:
                    raise PlayerError('SCORE_INPUT_MISSING', 409)
                self.player.library.get(key)
                self.preparations[sid] = lambda target: self.prepare_track(key, target)
            self.cancelled.discard(sid)
            atomic_json(directory / 'status.json', {'state': 'queued', 'phase': 'queued'})
            generation = self.generations.get(sid, 0) + 1
            self.generations[sid] = generation
            self.pending_tasks.add((sid, generation))
            self.jobs.put_nowait({'id': sid, 'kind': 'recognize', 'generation': generation})

    def prepare_track(self, key, directory):
        track = self.player.library.get(key)
        with self.player.network_action(track['source']):
            result = self.player.provider(track['source']).run('source', track=track)
            if result['track']['key'] != key:
                raise PlayerError('TRACK_IDENTITY_MISMATCH', 502)
            source = Path(result['path'])
            if not source.is_file() or not 0 < source.stat().st_size <= MAX_UPLOAD:
                raise PlayerError('SCORE_AUDIO_LIMIT', 413)
            # Open while the provider grant is held. Do not leak a cache pathname.
            with source.open('rb') as incoming, (directory / 'input.mp3').open('wb') as output:
                shutil.copyfileobj(incoming, output, 128 * 1024)
        request = read_json(directory / 'request.json', 32768)
        request['input'] = 'input.mp3'
        atomic_json(directory / 'request.json', request)

    def cancel(self, sid):
        directory = sample_dir(self.config, sid)
        with self.submit_lock:
            state = public_sample(directory)['status']['state']
            if state not in ('queued', 'running'):
                return
            self.cancelled.add(sid)
            with self.process_lock:
                if self.active == sid and self.owned_job:
                    self.owned_job.close()
            atomic_json(directory / 'status.json', {'state': 'cancelled', 'phase': 'cancelled'})

    def dispatch(self):
        while not self.closed.is_set():
            try:
                task = self.jobs.get(timeout=.25)
            except queue.Empty:
                continue
            editing = task['kind'] == 'edit'
            sid = task['id']
            directory = sample_dir(self.config, sid)
            status_file = directory / ('edit-status.json' if editing else 'status.json')
            try:
                if (sid in self.cancelled or self.closed.is_set() or
                    task['generation'] != self.generations.get(sid)):
                    continue
                self.active = sid
                atomic_json(status_file, {'state': 'running', 'phase': 'editing' if editing else 'preparing'})
                prepare = self.preparations.pop(sid, None)
                if prepare:
                    prepare(directory)
                if sid in self.cancelled or self.closed.is_set():
                    continue
                with OwnedJob() as job, (directory / ('editor.log' if editing else 'worker.log')).open('wb') as log:
                    command = [str(self.config['python']), '-I', '-B',
                        str(ROOT / 'capabilities/score_lab/entry.py'), 'editing' if editing else 'worker',
                        '--config', str(self.config['configPath']), '--sample', sid]
                    if editing:
                        command.extend(['--draft', str(directory / task['draft']), '--revision', task['revision'], '--_run'])
                    if job.name:
                        command.extend(['--job-name', job.name])
                    with self.process_lock:
                        if sid in self.cancelled or self.closed.is_set():
                            continue
                        self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        self.owned_job = job
                        job.process = self.process
                        process = self.process
                    try:
                        code = process.wait(timeout=min(600, self.config['jobTimeoutSeconds']) if editing else self.config['jobTimeoutSeconds'])
                    except subprocess.TimeoutExpired:
                        job.close()
                        process.kill()
                        process.wait(timeout=10)
                        raise RuntimeError('Timeout')
                    if code != 0:
                        raise RuntimeError('Worker failed')
                    if editing:
                        from capabilities.score_lab.editing import read_current
                        if read_current(self.config, sid)['revisionId'] != task['revision']:
                            raise RuntimeError('Incomplete edit commit')
                        atomic_json(status_file, {'state': 'completed', 'phase': 'ready', 'revisionId': task['revision']})
                    elif read_json(status_file).get('state') != 'completed':
                        raise RuntimeError('Incomplete result')
            except Exception as error:
                if sid not in self.cancelled:
                    atomic_json(status_file, {'state': 'failed', 'phase': 'interrupted' if self.closed.is_set() else 'error',
                        'message': '编辑未保存，请检查音符或重新打开乐谱。' if editing else
                                   '识谱未完成，请重试。',
                        'code': error.code if isinstance(error, PlayerError) else 'SCORE_FAILED'})
            finally:
                if sid in self.cancelled:
                    atomic_json(status_file, {'state': 'cancelled', 'phase': 'cancelled'})
                elif self.closed.is_set() and read_json(status_file, 32768).get('state') not in ('completed', 'failed'):
                    atomic_json(status_file, {'state': 'failed', 'phase': 'interrupted', 'message': '已中断，可重试。'})
                with self.process_lock:
                    self.process = None
                    self.owned_job = None
                    self.active = None
                with self.submit_lock:
                    self.pending_tasks.discard((sid, task['generation']))
                self.jobs.task_done()

    def close(self):
        self.closed.set()
        with self.process_lock:
            if self.owned_job:
                self.owned_job.close()
        self.thread.join(timeout=3)
        while True:
            try:
                task = self.jobs.get_nowait()
            except queue.Empty:
                break
            sid = task['id']
            status_file = sample_dir(self.config, sid) / ('edit-status.json' if task['kind'] == 'edit' else 'status.json')
            if (task['generation'] == self.generations.get(sid) and
                    read_json(status_file, 32768).get('state') in ('queued', 'running')):
                atomic_json(status_file, {'state': 'failed', 'phase': 'interrupted', 'message': '已中断，可重新识谱。'})
            self.pending_tasks.discard((sid, task['generation']))
            self.jobs.task_done()


class ScoreExtension:
    def __init__(self, player):
        self.player = player
        self.folder = player.private / 'extensions/score-lab'
        self.lock = threading.RLock()
        self.lab = None
        self.data_lock = None
        self.enabled = False
        try:
            self.enabled = read_json(self.folder / 'state.json', 32768).get('enabled') is True
        except (OSError, ValueError, AttributeError):
            pass
        # Configurable model location, but task data always belongs to this host.
        # No absolute machine path is part of the distributed source.
        pack = self.folder / 'bundle/config.json'
        default = pack if pack.is_file() else None
        selected = self.folder / 'bundle-config.json'
        self.config = load_config(selected if selected.is_file() else os.environ.get('USIC_SCORE_LAB_CONFIG') or default)
        self.config['dataDir'] = self.folder / 'data'
        self.config['configPath'] = self.folder / 'runtime.json'

    def describe(self):
        return {'id': 'score-lab', 'enabled': self.enabled,
                'runtimeReady': runtime_ready(self.config), 'localOnly': True,
                'entry': PREFIX, 'version': 1}

    def get_lab(self):
        with self.lock:
            if not self.enabled:
                raise PlayerError('SCORE_DISABLED', 409)
            if self.lab and self.lab.closed.is_set():
                if self.lab.thread.is_alive():
                    raise PlayerError('SCORE_BUSY', 409)
                self.close()
            if self.lab is None:
                try:
                    self.data_lock = DataLock(self.config['dataDir'] / 'server.lock')
                    serial = {k: str(v) if isinstance(v, Path) else v for k, v in self.config.items() if k != 'configPath'}
                    atomic_json(self.config['configPath'], serial)
                    self.lab = ManagedLab(self.config, self.player)
                except Exception:
                    if self.data_lock:
                        self.data_lock.__exit__()
                        self.data_lock = None
                    raise PlayerError('SCORE_UNAVAILABLE', 503) from None
            return self.lab

    def enable(self, enabled):
        if type(enabled) is not bool:
            raise PlayerError('INVALID_INPUT')
        with self.lock:
            if not enabled:
                self.close()
            self.enabled = enabled
            atomic_json(self.folder / 'state.json', {'enabled': enabled})
        return self.describe()

    def reload(self):
        with self.lock:
            if self.lab and (self.lab.active or not self.lab.jobs.empty()):
                raise PlayerError('SCORE_BUSY', 409)
            self.close()
            selected = self.folder / 'bundle-config.json'
            self.config = load_config(selected if selected.is_file() else os.environ.get('USIC_SCORE_LAB_CONFIG'))
            self.config['dataDir'] = self.folder / 'data'
            self.config['configPath'] = self.folder / 'runtime.json'
        return self.describe()

    def close(self):
        with self.lock:
            if self.lab:
                self.lab.close()
                # A provider's bounded in-flight download is not force-killed.
                # Keep its OS directory lock until the old thread truly exits.
                if self.lab.thread.is_alive():
                    return
                self.lab = None
            if self.data_lock:
                self.data_lock.__exit__()
                self.data_lock = None

    def submit_track(self, data):
        lab = self.get_lab()
        if not runtime_ready(self.config):
            raise PlayerError('SCORE_RUNTIME_MISSING', 503)
        track = self.player.library.get(data.get('key'))
        offset, seconds = clip_options(data)
        meta = {k: track[k] for k in ('title', 'artist', 'source')}
        meta.update(trackKey=track['key'], offset=offset, duration=seconds, full=seconds is None)
        sid = lab.create(meta, prepare=lambda directory: lab.prepare_track(track['key'], directory))
        return {'id': sid}

    def get(self, handler, path):
        if path == '/api/extensions/score-lab':
            handler.payload(self.describe())
            return True
        if not path.startswith(PREFIX):
            return False
        route = path[len(PREFIX):]
        adapter = MountedHandler(handler, self)
        try:
            adapter.get(route)
        except (FileNotFoundError, NotADirectoryError):
            raise PlayerError('NOT_FOUND', 404) from None
        except (ValueError, KeyError, TypeError):
            raise PlayerError('SCORE_DATA_INVALID', 422) from None
        return True

    def post(self, handler, path):
        if path == '/api/extensions/score-lab':
            handler.payload(self.enable(handler.body().get('enabled')))
            return True
        if path == '/api/extensions/score-lab/reload':
            handler.body()
            handler.payload(self.reload())
            return True
        if path == '/api/scores/track':
            handler.payload(self.submit_track(handler.body()), 202)
            return True
        if path.startswith(PREFIX):
            MountedHandler(handler, self).post(path[len(PREFIX):])
            return True
        return False


class MountedHandler:
    """Explicit routes on the player's origin; no additional listening port."""
    file = LabHandler.file

    def __init__(self, host, extension):
        self.host, self.extension = host, extension
        self.headers, self.command, self.wfile = host.headers, host.command, host.wfile

    def send_headers(self, status, length, content_type, **extra):
        h = self.host
        h.send_response(status)
        for key, value in {'Content-Type': content_type, 'Content-Length': str(length),
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'Cross-Origin-Resource-Policy': 'same-origin',
            'X-Frame-Options': 'SAMEORIGIN',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'self'", **extra}.items():
            h.send_header(key, value)
        h.end_headers()

    def json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_headers(status, len(body), 'application/json; charset=utf-8')
        if self.command != 'HEAD':
            self.wfile.write(body)

    def get(self, route):
        if route in STATIC:
            return self.file(WEB / STATIC[route])
        if route in ('flower.png', 'favicon.ico'):
            return self.file(ROOT / ('web/assets/usic.png' if route == 'flower.png' else 'desktop/assets/usic.ico'))
        lab = self.extension.get_lab()
        config = lab.config
        if route == 'vendor/abcjs-basic-min.js':
            asset = child_file(config['assetsDir'], 'abcjs-basic-min.js')
            if not asset.is_file() or sha256(asset) != ABCJS_SHA256:
                raise PlayerError('SCORE_ASSETS_MISSING', 503)
            return self.file(asset)
        if route == 'api/catalog':
            return self.json({'samples': lab.samples(), 'token': self.extension.player.csrf,
                'runtimeReady': runtime_ready(config), 'embedded': True})
        match = re.fullmatch(r'api/samples/([a-z0-9-]+)/notation', route)
        if match:
            from capabilities.score_lab.notation import notation_snapshot
            params = parse_qs(urlsplit(self.host.path).query, keep_blank_values=True, max_num_fields=1)
            if set(params) - {'revision'}:
                raise ValueError('Invalid notation query')
            return self.json(notation_snapshot(config, match[1], params.get('revision', [None])[0]))
        match = re.fullmatch(r'api/samples/([a-z0-9-]+)', route)
        if match:
            directory = sample_dir(config, match[1])
            info = public_sample(directory)
            if info['status']['state'] == 'completed':
                info['result'] = read_json(directory / 'result.json')
                from capabilities.score_lab.editing import read_current, read_revision_result, EditError
                try:
                    edit = read_current(config, match[1])
                    info['editing'] = edit
                    if edit['revisionId']:
                        info['originalResult'] = info['result']
                        info['result'] = {**info['result'], **read_revision_result(config, match[1], edit['revisionId'])}
                except EditError:
                    info['editingUnavailable'] = True
            return self.json(info)
        match = re.fullmatch(r'api/samples/([a-z0-9-]+)/editor', route)
        if match:
            from capabilities.score_lab.editing import read_current, EditError
            try:
                value = read_current(config, match[1])
            except EditError:
                raise PlayerError('SCORE_EDIT_UNAVAILABLE', 422) from None
            path = sample_dir(config, match[1]) / 'edit-status.json'
            value['status'] = read_json(path, 32768) if path.is_file() else {'state': 'idle'}
            return self.json(value)
        match = re.fullmatch(r'revisions/([a-z0-9-]+)/([a-z0-9-]+)/(media|export)/([a-zA-Z0-9_.-]+)', route)
        if match:
            from capabilities.score_lab.editing import revision_file, EditError
            sid, revision, kind, name = match.groups()
            try:
                file = revision_file(config, sid, revision, name, kind=kind)
            except EditError:
                raise PlayerError('NOT_FOUND', 404) from None
            return self.file(file, download=kind == 'export')
        match = re.fullmatch(r'(media|export)/([a-z0-9-]+)/([a-zA-Z0-9_.-]+)', route)
        if match:
            kind, sid, name = match.groups()
            if name not in (MEDIA if kind == 'media' else EXPORTS):
                raise PlayerError('NOT_FOUND', 404)
            directory = sample_dir(config, sid)
            if public_sample(directory)['status']['state'] != 'completed':
                raise PlayerError('SCORE_NOT_READY', 409)
            return self.file(child_file(directory, *(['result', name] if kind == 'export' else [name])), download=kind == 'export')
        raise PlayerError('NOT_FOUND', 404)

    def post(self, route):
        lab = self.extension.get_lab()
        match = re.fullmatch(r'api/samples/([a-z0-9-]+)/edit', route)
        if match:
            if not runtime_ready(lab.config):
                raise PlayerError('SCORE_RUNTIME_MISSING', 503)
            return self.json(lab.submit_edit(match[1], self.host.body()), 202)
        match = re.fullmatch(r'api/samples/([a-z0-9-]+)/(cancel|retry)', route)
        if match:
            self.host.body()
            sid, action = match.groups()
            lab.cancel(sid) if action == 'cancel' else lab.retry(sid)
            return self.json({'ok': True, 'id': sid}, 200 if action == 'cancel' else 202)
        if route != 'api/import':
            raise PlayerError('NOT_FOUND', 404)
        if not runtime_ready(lab.config):
            raise PlayerError('SCORE_RUNTIME_MISSING', 503)
        if self.headers.get('Transfer-Encoding'):
            raise PlayerError('INVALID_INPUT')
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not lengths[0].isdecimal() or not 0 < int(lengths[0]) <= MAX_UPLOAD:
            raise PlayerError('BODY_LIMIT', 413)
        params = parse_qs(urlsplit(self.host.path).query, max_num_fields=5)
        name = params.get('name', [''])[0]
        ext = Path(name).suffix.lower()
        if ext not in {'.wav', '.mp3', '.flac', '.ogg', '.aiff', '.aif'}:
            raise PlayerError('SCORE_AUDIO_FORMAT')
        seconds = params.get('seconds', ['30'])[0]
        offset, seconds = clip_options({'offset': float(params.get('offset', ['0'])[0]),
            'seconds': 'full' if seconds == 'full' else float(seconds)})
        # Stream to an owned spool first. Queue capacity checked again at commit.
        spool = child_file(lab.config['dataDir'], 'uploads', secrets.token_hex(12) + ext)
        spool.parent.mkdir(parents=True, exist_ok=True)
        try:
            with spool.open('xb') as output:
                remaining = int(lengths[0])
                while remaining:
                    block = self.host.rfile.read(min(remaining, 128 * 1024))
                    if not block:
                        raise PlayerError('SCORE_UPLOAD_INTERRUPTED')
                    output.write(block)
                    remaining -= len(block)
            title = re.sub(r'[\x00-\x1f\x7f]', '', Path(name.replace('\\', '/')).stem)[:80] or '未命名'
            sid = lab.create({'title': title, 'artist': '', 'source': 'local', 'offset': offset,
                'duration': seconds, 'full': seconds is None},
                {'input': 'input' + ext, 'offset': offset, 'seconds': seconds}, input_file=spool)
            # The committed sample owns the input even after a browser navigation.
            spool = None
            return self.json({'id': sid}, 202)
        finally:
            if spool is not None:
                spool.unlink(missing_ok=True)
