"""Offline dual-source contracts. Fixtures never claim real playback/login."""
from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
from library import Library, PlayerError, canonical_key, clean_track, valid_key
from listen_store import History
from insights import InsightStore
from providers import ProviderRegistry
from providers.netease import NetEase
from providers.netease import provider as ne
from providers.netease import transport
from server import Player
import test_player as http_fixtures

QQ = {'mid': '0039MnYb0qxYhV', 'songId': 97773, 'title': '晴天', 'artist': '周杰伦',
      'album': '叶惠美', 'albumMid': '000MkMni19ClKG', 'duration': 269}
QQKEY = 'qq:' + QQ['mid']
NE = {'source': 'netease', 'songId': 33190124, 'title': 'Carefree', 'artist': 'Kevin MacLeod',
      'album': 'Carefree', 'duration': 205, 'availability': 'free'}
NEKEY = 'netease:33190124'
SOURCE = {'id': 33190124, 'code': 200, 'type': 'mp3', 'br': 128000, 'size': 3, 'time': 205113,
          'fee': 8, 'freeTrialInfo': None, 'url': 'https://m10.music.126.net/fixture.mp3?fixture=private'}


class IdentityTests(unittest.TestCase):
    def test_qq_legacy_alias_and_netease_never_collide(self):
        self.assertEqual(canonical_key(QQ['mid']), QQKEY)
        self.assertEqual(clean_track(QQ)['key'], QQKEY)
        self.assertEqual(clean_track(NE)['key'], NEKEY)
        numeric = '12345678901234'
        self.assertNotEqual(clean_track({**QQ, 'mid': numeric})['key'],
                            clean_track({**NE, 'songId': int(numeric)})['key'])

    def test_keys_sources_identity_and_url_injection_rejected(self):
        for key in ('netease:0', 'netease:01', 'netease:-1', 'netease:1/../../x', 'other:1',
                    'qq:short', 'netease:9007199254740992', {}, None, True):
            self.assertFalse(valid_key(key), str(key))
        for changes in ({'source': 'other'}, {'key': NEKEY}, {'providerId': 'not-the-mid'}):
            with self.assertRaises(PlayerError): clean_track({**QQ, **changes})
        self.assertEqual(clean_track({**NE, 'albumArt': '//outside.example/a.jpg'})['cover'], '/assets/cover-default.svg')
        self.assertNotIn('url', clean_track({**NE, 'url': 'https://secret.example'}))


def legacy_database(path, bad_lyrics=False, conflict=False):
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''
        CREATE TABLE tracks(key TEXT PRIMARY KEY,metadata TEXT NOT NULL,used REAL);
        CREATE TABLE favorites(key TEXT PRIMARY KEY REFERENCES tracks(key),created REAL NOT NULL);
        CREATE TABLE playlists(id TEXT PRIMARY KEY,name TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE playlist_tracks(playlist TEXT REFERENCES playlists(id) ON DELETE CASCADE,
          key TEXT REFERENCES tracks(key),position INTEGER NOT NULL,PRIMARY KEY(playlist,key));
        CREATE TABLE settings(id INTEGER PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE talk_listens(id TEXT PRIMARY KEY,key TEXT NOT NULL REFERENCES tracks(key),
          started REAL NOT NULL,updated REAL NOT NULL,seq INTEGER NOT NULL,seconds REAL NOT NULL,
          coverage REAL NOT NULL,segments TEXT NOT NULL,reason TEXT NOT NULL,qualified INTEGER NOT NULL,completed INTEGER NOT NULL);
        CREATE TABLE talk_listen_days(id TEXT NOT NULL REFERENCES talk_listens(id),day TEXT NOT NULL,
          seconds REAL NOT NULL,plays INTEGER NOT NULL,completed INTEGER NOT NULL,skipped INTEGER NOT NULL,PRIMARY KEY(id,day));
        CREATE TABLE talk_lyrics(key TEXT PRIMARY KEY REFERENCES tracks(key),updated REAL NOT NULL,data TEXT NOT NULL);
        CREATE TABLE talk_analysis(key TEXT PRIMARY KEY REFERENCES tracks(key),updated REAL NOT NULL,data TEXT NOT NULL);
        ''')
        track = {**QQ, 'key': QQ['mid'], 'cover': '/api/cover/' + QQ['albumMid']}
        db.execute('INSERT INTO tracks VALUES(?,?,?)', (QQ['mid'], json.dumps(track), 100.))
        if conflict:
            db.execute('INSERT INTO tracks VALUES(?,?,?)', (QQKEY, json.dumps({**track, 'key': QQKEY}), 101.))
        db.execute('INSERT INTO favorites VALUES(?,?)', (QQ['mid'], 102.))
        db.execute('INSERT INTO playlists VALUES(?,?,?)', ('a'*32, '旧列表', 103.))
        db.execute('INSERT INTO playlist_tracks VALUES(?,?,?)', ('a'*32, QQ['mid'], 7))
        state = {'queue': [QQ['mid']], 'current': QQ['mid'], 'position': 42, 'volume': .42, 'mode': 'single'}
        db.execute('INSERT INTO settings VALUES(1,?)', (json.dumps(state),))
        db.execute('INSERT INTO talk_listens VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   ('b'*32, QQ['mid'], 104., 105., 3, 10., .1, '[[0,10]]', 'close', 0, 0))
        db.execute('INSERT INTO talk_listen_days VALUES(?,?,?,?,?,?)', ('b'*32, '2026-10-12', 10., 0, 0, 0))
        lyric = '{broken' if bad_lyrics else json.dumps({'key': QQ['mid'], 'source': 'qq', 'lrc': '[00:01]测试', 'available': True})
        db.execute('INSERT INTO talk_lyrics VALUES(?,?,?)', (QQ['mid'], 106., lyric))
        db.execute('INSERT INTO talk_analysis VALUES(?,?,?)', (QQ['mid'], 107., json.dumps({'key': QQ['mid'], 'kind': 'pitch'})))
        db.commit()


class MigrationTests(unittest.TestCase):
    def test_full_old_library_backup_upgrade_restart_and_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'library.sqlite3'; legacy_database(path)
            lib = Library(path)
            try:
                snap = lib.snapshot()
                self.assertEqual(snap['favorites'], [QQKEY]); self.assertEqual(snap['library'], [QQKEY])
                self.assertEqual(snap['playlists'][0]['keys'], [QQKEY])
                self.assertEqual(snap['state'], {'queue': [QQKEY], 'current': QQKEY, 'position': 42, 'volume': .42, 'mode': 'single'})
                self.assertEqual(lib.get(QQ['mid'])['source'], 'qq')
                history = History(lib); insights = InsightStore(lib)
                self.assertEqual(history.summary(0)['totalSeconds'], 10)
                self.assertEqual(history.recent()['items'][0]['source'], 'qq')
                self.assertEqual(insights.get('lyrics', QQ['mid'])['key'], QQKEY)
                self.assertEqual(insights.get('analysis', QQKEY)['source'], 'qq')
                self.assertEqual(lib.db.execute('SELECT position FROM playlist_tracks').fetchone()[0], 7)
                self.assertEqual(lib.db.execute('SELECT updated FROM talk_lyrics').fetchone()[0], 106.)
                self.assertEqual(lib.db.execute('PRAGMA foreign_key_check').fetchall(), [])
                backup = Path(temp) / 'backups' / lib.migration['backup']
                with closing(sqlite3.connect(backup)) as old:
                    self.assertEqual(old.execute('SELECT key FROM favorites').fetchone()[0], QQ['mid'])
                    self.assertEqual(old.execute('SELECT key FROM talk_listens').fetchone()[0], QQ['mid'])
                    self.assertNotIn('source', [r[1] for r in old.execute('PRAGMA table_info(talk_listens)')])
                lib.favorite(QQ['mid'], False)
                lib.save_state({'queue': [QQ['mid']], 'current': QQ['mid']})
                self.assertEqual(lib.state()['queue'], [QQKEY])
                before = lib.snapshot()
            finally: lib.close()
            lib = Library(path)
            try:
                self.assertIsNone(lib.migration); self.assertEqual(lib.snapshot(), before)
                self.assertEqual(len(list((Path(temp)/'backups').glob('*.sqlite3'))), 1)
            finally: lib.close()

    def test_failure_rolls_back_key_refs_metadata_schema_and_version(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'library.sqlite3'; legacy_database(path, bad_lyrics=True)
            with self.assertRaises(json.JSONDecodeError): Library(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT key FROM tracks').fetchone()[0], QQ['mid'])
                self.assertEqual(db.execute('SELECT key FROM favorites').fetchone()[0], QQ['mid'])
                self.assertEqual(db.execute('SELECT key FROM talk_listens').fetchone()[0], QQ['mid'])
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertNotIn('source', [r[1] for r in db.execute('PRAGMA table_info(tracks)')])
            self.assertEqual(len(list((Path(temp)/'backups').glob('*.sqlite3'))), 1)

    def test_collision_is_not_silently_merged_or_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'library.sqlite3'; legacy_database(path, conflict=True)
            with self.assertRaisesRegex(PlayerError, 'MIGRATION_CONFLICT'): Library(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tracks').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT key FROM favorites').fetchone()[0], QQ['mid'])


class NetEaseTests(unittest.TestCase):
    def test_only_four_first_party_read_routes_no_cookies_no_account_dependency(self):
        class Opener:
            def open(self, req, timeout):
                self.req = req; self.timeout = timeout
                return io.BytesIO(b'{"code":200}')
        opener = Opener()
        with patch.object(transport.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(transport.request('/api/cloudsearch/pc', {'s': 'fixture'})['code'], 200)
        self.assertEqual(opener.req.full_url, 'https://music.163.com/weapi/cloudsearch/pc')
        self.assertIsNone(opener.req.get_header('Cookie')); self.assertIsNone(opener.req.get_header('Authorization'))
        for route in ('https://outside.example', '/api/login', '/api/like', '/api/playlist/create'):
            with self.assertRaises(ValueError): transport.request(route, {})
        with self.assertRaises(ValueError): transport.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://outside.example')

    def test_public_transport_size_budget(self):
        class Opener:
            def open(self, *args, **kwargs): return io.BytesIO(b'x'*(1024**2+1))
        with patch.object(transport.urllib.request, 'build_opener', return_value=Opener()), self.assertRaises(ValueError):
            transport.request('/api/cloudsearch/pc', {})

    def test_exact_id_full_free_standard_only(self):
        actual = clean_track(NE)
        self.assertEqual(ne.source_record({'code': 200, 'data': [SOURCE]}, actual)['id'], actual['songId'])
        for changes in ({'id': 9}, {'code': 404}, {'fee': 1}, {'time': 30000}, {'br': 320000},
                        {'type': 'ncm'}, {'freeTrialInfo': {}}, {'freeTrialInfo': {'end': 30}},
                        {'size': ne.MAX_BYTES+1}, {'url': None}, {'url': 'https://outside.example/a'},
                        {'freeTrialPrivilege': {'cannotListenReason': 1}}):
            with self.subTest(changes=changes), self.assertRaises(PlayerError):
                ne.source_record({'code': 200, 'data': [{**SOURCE, **changes}]}, actual)

    def test_media_tls_host_credential_port_scope(self):
        self.assertEqual(ne.media_url('http://m10.music.126.net/a.mp3'), 'https://m10.music.126.net/a.mp3')
        for url in ('file:///x', 'https://m10.music.126.net.evil.example/a', 'https://u:p@m10.music.126.net/a',
                    'https://m10.music.126.net:123/a', 'https://m10.music.126.net/a#x', 'https://127.0.0.1/x'):
            with self.subTest(url=url), self.assertRaises(ValueError): ne.media_url(url)

    def test_search_availability_and_artwork_not_an_audio_permission(self):
        row = {'id': 33190124, 'name': 'Carefree', 'ar': [{'name': 'Kevin MacLeod'}],
               'al': {'name': 'Carefree', 'picUrl': 'https://outside.example/a.jpg'}, 'dt': 205078, 'fee': 8}
        self.assertEqual(ne.metadata(row)['availability'], 'free')
        self.assertEqual(ne.metadata(row)['cover'], '/assets/cover-default.svg')
        self.assertEqual(ne.metadata({**row, 'fee': 1})['availability'], 'requires_login')
        self.assertEqual(ne.metadata(row, {'st': -200})['availability'], 'unavailable')

    def test_download_full_body_no_cookies_and_no_url_in_media(self):
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}
        class Opener:
            def open(self, req, timeout):
                self.req = req
                return Response(b'abc')
        opener = Opener()
        with patch.object(ne.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(ne.download(SOURCE), b'abc')
        self.assertIsNone(opener.req.get_header('Cookie'))
        with patch.object(ne.urllib.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
            ne.download({**SOURCE, 'size': 4})


class FakeProvider:
    def __init__(self, source, track):
        self.id = source; self.name = source; self.track = clean_track(track); self.calls = []
        self.problem = None; self.qr = None; self.auth_cancel = False; self.auth_status = 'idle'
    def public(self): return {'session': 'valid', 'auth': self.auth_status, 'qrReady': False}
    def run(self, operation, **args):
        self.calls.append((operation, args))
        if self.problem: raise PlayerError(self.problem, 502)
        if operation == 'search': return {'tracks': [self.track], 'hasMore': True}
        if operation == 'lyrics': return {'lyric': '[00:01]fixture', 'translation': '', 'songId': self.track['songId']}
        raise PlayerError('SOURCE_UNAVAILABLE', 422)
    async def login(self, provider): self.auth_status = 'done'


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.app = Player(Path(self.temp.name), live=False)
        self.qq = FakeProvider('qq', QQ); self.ne = FakeProvider('netease', NE)
        self.app.bridge = self.qq; self.app.providers = ProviderRegistry([self.qq, self.ne])
        self.app.library.put([QQ, NE])
    def tearDown(self): self.app.library.close(); self.temp.cleanup()

    def test_aggregate_success_exact_source_keys_and_provider_capabilities(self):
        result = self.app.search('fixture', 1, 'all')
        self.assertEqual([t['key'] for t in result['tracks']], [QQKEY, NEKEY])
        self.assertFalse(result['partial']); self.assertEqual(set(result['sources']), {'qq', 'netease'})
        self.assertIn('free-only', NetEase.capabilities); self.assertNotIn('login', NetEase.capabilities)

    def test_either_source_failure_preserves_the_other_and_reports_reason(self):
        for failed, survivor in ((self.qq, NEKEY), (self.ne, QQKEY)):
            failed.problem = 'LOGIN_REQUIRED' if failed.id == 'qq' else 'NETEASE_UNAVAILABLE'
            result = self.app.search('fixture', 1, 'all')
            self.assertTrue(result['partial']); self.assertEqual(result['tracks'][0]['key'], survivor)
            self.assertEqual(result['sources'][failed.id]['code'], failed.problem)
            failed.problem = None

    def test_independent_provider_locks_never_block_qq_with_netease(self):
        self.app.provider_network['netease'].acquire()
        try: self.assertEqual(self.app.search('fixture', 1, 'qq')['tracks'][0]['key'], QQKEY)
        finally: self.app.provider_network['netease'].release()
        self.app.network.acquire()
        try: self.assertEqual(self.app.search('fixture', 1, 'netease')['tracks'][0]['key'], NEKEY)
        finally: self.app.network.release()

    def test_per_source_pagination_retry_does_not_repeat_or_skip_other_source(self):
        self.app.search('fixture', 1, 'all', {'netease': 2})
        self.assertEqual(self.qq.calls, []); self.assertEqual(self.ne.calls[0][1]['page'], 2)
        for source, pages in (('other', None), ('all', {}), ('all', {'other': 1}), ('qq', {'netease': 1}),
                              ('all', {'qq': True}), ('all', {'qq': 11})):
            with self.assertRaises(PlayerError): self.app.search('fixture', 1, source, pages)

    def test_cross_provider_metadata_is_not_inserted_or_accepted(self):
        self.ne.track = self.qq.track
        result = self.app.search('fixture', 1, 'all')
        self.assertEqual(result['sources']['netease']['code'], 'TRACK_IDENTITY_MISMATCH')

    def test_lyrics_and_source_jobs_route_by_stored_identity_not_ui_or_title(self):
        for key, provider, other in ((NEKEY, self.ne, self.qq), (QQKEY, self.qq, self.ne)):
            provider.calls.clear(); other.calls.clear()
            result, status = self.app.lyrics(key)
            for _ in range(100):
                job = self.app.extra_jobs[result['job']]
                if job['status'] != 'loading': break
                time.sleep(.01)
            self.assertEqual(job['status'], 'ready'); self.assertEqual(job['lyrics']['source'], provider.id)
            self.assertEqual(other.calls, [])
            provider.calls.clear()
            result = self.app.play(key)
            for _ in range(100):
                job = self.app.jobs[result['job']]
                if job['status'] != 'loading': break
                time.sleep(.01)
            self.assertEqual(provider.calls[0][0], 'source'); self.assertEqual(other.calls, [])
            self.assertEqual(job['provider'], provider.id)

    def test_qq_login_only_revokes_qq_media_not_netease(self):
        self.app.media = {'qq-token': {'provider': 'qq'}, 'ne-token': {'provider': 'netease'}}
        self.app.jobs = {'qq-job': {'provider': 'qq'}, 'ne-job': {'provider': 'netease'}}
        self.app.login('qq'); self.app.auth_thread.join(2)
        self.assertEqual(set(self.app.media), {'ne-token'}); self.assertEqual(set(self.app.jobs), {'ne-job'})
        self.assertFalse(self.app.network.locked())

    def test_completed_qq_jobs_never_evict_an_older_running_netease_job(self):
        slow = {'id': 'slow-ne', 'status': 'loading', 'created': time.time()-900, 'provider': 'netease'}
        self.app.jobs = {'slow-ne': slow, **{str(i): {'status': 'ready', 'created': time.time(), 'provider': 'qq'}
                                          for i in range(15)}}
        result = self.app.play(QQKEY)
        for _ in range(100):
            if not self.app.network.locked(): break
            time.sleep(.01)
        self.assertIs(self.app.jobs['slow-ne'], slow)
        self.assertEqual(self.app.jobs[result['job']]['status'], 'error')
        self.assertEqual(len(self.app.jobs), 16)
        self.app.jobs = {str(i): {**slow, 'id': str(i)} for i in range(16)}
        with self.assertRaises(PlayerError) as error: self.app.play(QQKEY)
        self.assertEqual(error.exception.code, 'BUSY')
        self.assertFalse(self.app.network.locked())
        self.assertEqual(len(self.app.jobs), 16)

    def test_mixed_library_favorites_queue_and_listening_statistics_persist_source(self):
        lib = self.app.library
        lib.favorite(NEKEY, True); pid = lib.create_playlist('混合'); lib.playlist(pid, 'add', QQ['mid']); lib.playlist(pid, 'add', NEKEY)
        lib.save_state({'queue': [QQ['mid'], NEKEY], 'current': NEKEY})
        self.assertEqual(lib.snapshot()['playlists'][0]['keys'], [QQKEY, NEKEY])
        self.assertEqual(lib.state()['queue'], [QQKEY, NEKEY])
        for sid, key, seconds in (('1'*32, QQ['mid'], 3), ('2'*32, NEKEY, 7)):
            self.app.history.heartbeat({'id': sid, 'key': key, 'seq': 1, 'seconds': seconds,
                                        'segments': [[0, seconds]], 'reason': 'close'})
        self.assertEqual(self.app.history.summary(0)['totalSeconds'], 10)
        self.assertEqual(self.app.history.summary(0, source='qq')['totalSeconds'], 3)
        self.assertEqual(self.app.history.summary(0, source='netease')['totalSeconds'], 7)
        self.assertEqual(len(self.app.history.recent(source='netease')['items']), 1)
        self.assertEqual(self.app.history.recent(source='netease')['items'][0]['track']['source'], 'netease')
        self.assertEqual(lib.db.execute("SELECT source FROM talk_listens WHERE id=?", ('2'*32,)).fetchone()[0], 'netease')
        for source in ('other', None, True, {}):
            with self.assertRaises(PlayerError): self.app.history.summary(source=source)


class SourceHttpTests(unittest.TestCase):
    request = http_fixtures.HttpTests.request
    tearDown = http_fixtures.HttpTests.tearDown

    def setUp(self):
        http_fixtures.HttpTests.setUp(self)
        self.qq = FakeProvider('qq', QQ); self.ne = FakeProvider('netease', NE)
        self.ne.capabilities = NetEase.capabilities
        self.app.bridge = self.qq; self.app.providers = ProviderRegistry([self.qq, self.ne])
        self.app.library.put([NE])

    def test_per_source_http_search_and_explicit_free_only_capability(self):
        status, _, body = self.request('/api/search', {'query': 'fixture', 'source': 'all', 'pages': {'netease': 2}})
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)['tracks'][0]['key'], NEKEY)
        self.assertEqual(self.qq.calls, []); self.assertEqual(self.ne.calls[0][1]['page'], 2)
        providers = json.loads(self.request('/api/providers')[2])['providers']
        self.assertEqual([p['id'] for p in providers], ['qq', 'netease'])
        self.assertNotIn('login', providers[1]['capabilities'])
        self.assertTrue(json.loads(self.request('/api/features')[2])['aggregation'])
        self.assertEqual(self.request('/aggregation.mjs')[0], 200)

    def test_source_filtered_history_stats_and_invalid_filter(self):
        self.request('/api/listen', {'id': 'c'*32, 'key': NEKEY, 'seq': 1, 'seconds': 1,
                                     'segments': [[0,1]], 'reason': 'close'})
        ne_stats = json.loads(self.request('/api/stats?days=0&source=netease')[2])
        qq_stats = json.loads(self.request('/api/stats?days=0&source=qq')[2])
        self.assertEqual(ne_stats['totalSeconds'], 1); self.assertEqual(qq_stats['totalSeconds'], 0)
        self.assertEqual(json.loads(self.request('/api/history?source=netease')[2])['items'][0]['source'], 'netease')
        for path in ('/api/stats?source=outside', '/api/history?source=outside'):
            self.assertEqual(self.request(path)[0], 400)

    def test_shutdown_guard_includes_independent_netease_work(self):
        self.assertFalse(self.app.busy())
        self.app.provider_network['netease'].acquire()
        try:
            self.assertTrue(self.app.busy())
            self.assertEqual(self.request('/api/close', {})[0], 409)
            self.assertEqual(self.request('/api/health')[0], 200)
        finally: self.app.provider_network['netease'].release()
        self.assertFalse(self.app.busy())

    def test_canonical_and_legacy_insight_routes_export_filename_portable(self):
        for key in (QQKEY, NEKEY):
            self.app.insights.put('lyrics', key, {'available': True, 'format': 'lrc', 'lrc': '[00:01]fixture'})
            status, headers, _ = self.request('/api/lyrics/' + key.replace(':', '%3A') + '/export')
            self.assertEqual(status, 200)
            self.assertNotIn(':', headers['Content-Disposition'])
            self.assertEqual(json.loads(self.request('/api/lyrics/' + key)[2])['lyrics']['key'], key)
        self.assertEqual(json.loads(self.request('/api/lyrics/' + QQ['mid'])[2])['lyrics']['key'], QQKEY)

    def test_failed_netease_source_never_falls_back_to_qq(self):
        self.ne.problem = 'NETEASE_SOURCE_UNAVAILABLE'
        status, _, body = self.request('/api/play', {'key': NEKEY})
        self.assertEqual(status, 202); jid = json.loads(body)['job']
        for _ in range(100):
            result = json.loads(self.request('/api/jobs/' + jid)[2])
            if result['status'] != 'loading': break
            time.sleep(.01)
        self.assertEqual(result['code'], 'NETEASE_SOURCE_UNAVAILABLE')
        self.assertEqual(self.qq.calls, []); self.assertEqual(result['provider'], 'netease')

    def test_ready_job_strips_private_track_fields_and_uses_loopback_media(self):
        media = Path(self.temp.name) / 'synthetic.mp3'; media.write_bytes(b'offline-fixture-not-real-audio')
        def source(operation, **args):
            return {'track': {**clean_track(NE), 'url': 'private-signed-fixture', 'path': 'private-path-fixture'},
                    'path': media, 'media': {'decoderVerified': True, 'completeMp3FrameCoverage': True,
                                           'nonSilentPcmObserved': True}}
        self.ne.run = source
        _, _, body = self.request('/api/play', {'key': NEKEY}); jid = json.loads(body)['job']
        for _ in range(100):
            result = json.loads(self.request('/api/jobs/' + jid)[2])
            if result['status'] != 'loading': break
            time.sleep(.01)
        self.assertEqual(result['status'], 'ready'); self.assertEqual(result['track']['key'], NEKEY)
        self.assertEqual(result['source']['provider'], 'netease')
        self.assertTrue(result['mediaUrl'].startswith('/media/'))
        self.assertNotIn('private-', json.dumps(result)); self.assertNotIn('path', result)


if __name__ == '__main__': unittest.main(verbosity=2)
