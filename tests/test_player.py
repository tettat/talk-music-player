"""Offline contracts. Synthetic media tests are not live provider/audio evidence."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
from library import Library, PlayerError, clean_track, text, valid_mid
from server import Player, Server, byte_range

TRACK = {'mid': '0039MnYb0qxYhV', 'songId': 97773, 'title': '晴天', 'artist': '周杰伦',
         'album': '叶惠美', 'albumMid': '000MkMni19ClKG', 'duration': 269}
TRACK_KEY = 'qq:' + TRACK['mid']


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'library.sqlite3'
        self.lib = Library(self.path)
        self.track = self.lib.put([TRACK])[0]

    def tearDown(self):
        self.lib.close()
        self.temp.cleanup()

    def test_search_metadata_not_faked_as_library_or_history(self):
        self.assertEqual(self.lib.snapshot()['library'], [])
        self.assertEqual(self.lib.snapshot()['tracks'], [])

    def test_metadata_uses_only_fixed_local_cover_route(self):
        self.assertEqual(self.track['cover'], '/api/cover/000MkMni19ClKG')
        self.assertEqual(self.track['key'], TRACK_KEY)
        self.assertEqual(self.track['source'], 'qq')

    def test_reject_mid_traversal(self):
        for mid in ['../secret', 'http://example.com', '', 'x' * 33, '<script>bad</script>']:
            self.assertFalse(valid_mid(mid))

    def test_reject_invalid_song_id(self):
        for value in [None, True, -1, '97773', 2**53]:
            with self.assertRaises(PlayerError):
                clean_track({**TRACK, 'songId': value})

    def test_reject_preview_invalid_durations(self):
        for value in [0, -1, True, 1801, float('nan')]:
            with self.assertRaises(PlayerError):
                clean_track({**TRACK, 'duration': value})

    def test_bad_album_mid_never_becomes_url(self):
        self.assertEqual(clean_track({**TRACK, 'albumMid': '../../secret'})['cover'], '/assets/cover-default.svg')

    def test_favorite_toggle_and_deduplicate(self):
        self.lib.favorite(TRACK['mid'], True)
        self.lib.favorite(TRACK['mid'], True)
        self.assertEqual(self.lib.snapshot()['favorites'], [TRACK_KEY])
        self.assertEqual(self.lib.snapshot()['library'], [TRACK_KEY])
        self.lib.favorite(TRACK['mid'], False)
        self.assertEqual(self.lib.snapshot()['favorites'], [])

    def test_playlist_add_rename_remove_delete(self):
        pid = self.lib.create_playlist('夜晚')
        self.lib.playlist(pid, 'add', TRACK['mid'])
        self.lib.playlist(pid, 'add', TRACK['mid'])
        self.lib.playlist(pid, 'rename', '夜航')
        self.assertEqual(self.lib.snapshot()['playlists'][0], {'id': pid, 'name': '夜航', 'keys': [TRACK_KEY]})
        self.lib.playlist(pid, 'remove', TRACK['mid'])
        self.assertEqual(self.lib.snapshot()['playlists'][0]['keys'], [])
        self.lib.playlist(pid, 'delete')
        self.assertEqual(self.lib.snapshot()['playlists'], [])

    def test_playlist_input_limits(self):
        for name in ['', '   ', 'x' * 41, None]:
            with self.assertRaises(PlayerError): self.lib.create_playlist(name)

    def test_unknown_track_not_inserted_by_user(self):
        with self.assertRaises(PlayerError): self.lib.favorite('00000000000000', True)

    def test_parameterized_strings_remain_data(self):
        name = "x'); DROP TABLE tracks;--"
        self.lib.create_playlist(name)
        self.assertEqual(self.lib.snapshot()['playlists'][0]['name'], name)
        self.assertEqual(self.lib.get(TRACK['mid'])['title'], '晴天')

    def test_invalid_settings_atomic(self):
        before = self.lib.state()
        for data in [{'volume': 2}, {'position': -1}, {'volume': True}, {'mode': 'invalid'},
                     {'queue': [TRACK['mid'], TRACK['mid']]}, {'queue': [{}]},
                     {'queue': ['unknown']}, {'position': float('nan')}, {'secret': 'no'}]:
            with self.assertRaises(PlayerError): self.lib.save_state(data)
            self.assertEqual(self.lib.state(), before)

    def test_queue_includes_unsaved_search_metadata_on_reload(self):
        self.lib.save_state({'queue': [TRACK['mid']], 'current': TRACK['mid'], 'position': 42})
        self.assertEqual(self.lib.snapshot()['tracks'][0]['title'], '晴天')

    def test_real_sqlite_restart_persistence(self):
        self.lib.favorite(TRACK['mid'], True)
        pid = self.lib.create_playlist('通勤')
        self.lib.playlist(pid, 'add', TRACK['mid'])
        self.lib.save_state({'volume': .42, 'mode': 'single', 'queue': [TRACK['mid']]})
        before = self.lib.snapshot()
        self.lib.close(); self.lib = Library(self.path)
        self.assertEqual(self.lib.snapshot(), before)

    def test_whitespace_and_controls(self):
        self.assertEqual(text('  夜\n晚  ', 40), '夜晚')


class RangeTests(unittest.TestCase):
    def test_whole_file(self): self.assertEqual(byte_range(None, 100), (0, 99, False))
    def test_bounded(self): self.assertEqual(byte_range('bytes=4-9', 100), (4, 9, True))
    def test_open_ended(self): self.assertEqual(byte_range('bytes=90-', 100), (90, 99, True))
    def test_suffix(self): self.assertEqual(byte_range('bytes=-8', 100), (92, 99, True))
    def test_large_suffix(self): self.assertEqual(byte_range('bytes=-1000', 100), (0, 99, True))
    def test_end_clamped(self): self.assertEqual(byte_range('bytes=4-999', 100), (4, 99, True))
    def test_multiple_ranges_refused(self):
        with self.assertRaises(PlayerError): byte_range('bytes=0-3,8-9', 100)
    def test_invalid_ranges(self):
        for value in ['bytes=-0', 'bytes=-', 'bytes=100-', 'bytes=9-2', 'items=0-3', 'bytes=1.5-9']:
            with self.assertRaises(PlayerError): byte_range(value, 100)


class FakeBridge:
    qr = None
    auth_cancel = False
    def public(self): return {'session': 'required', 'auth': 'idle', 'qrReady': False}
    def run(self, operation, **args):
        if operation == 'search': return {'tracks': [TRACK], 'hasMore': False}
        raise PlayerError('LOGIN_REQUIRED', 401)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Player(Path(self.temp.name), live=False)
        self.app.bridge = FakeBridge()
        self.app.library.put([TRACK], used=True)
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.app.library.close(); self.temp.cleanup()

    def request(self, path, data=None, headers=None, method=None, raw=None):
        method = method or ('GET' if data is None and raw is None else 'POST')
        h = {'Host': self.host, 'Connection': 'close'}
        if data is not None or raw is not None:
            h.update({'Content-Type': 'application/json', 'X-Talk-CSRF': self.app.csrf})
        h.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, body=raw if raw is not None else json.dumps(data) if data is not None else None, headers=h)
        response = connection.getresponse(); body = response.read(); connection.close()
        return response.status, dict(response.headers), body

    def request_headers(self, path, headers=None, length=1):
        # Header validation must reject before waiting for an upload. Sending an
        # intentionally unread body can race a Windows TCP reset on connection close.
        h = {'Host': self.host, 'Connection': 'close', 'Content-Type': 'application/json',
             'X-Talk-CSRF': self.app.csrf, 'Content-Length': str(length)}
        h.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.putrequest('POST', path, skip_host=True)
            for name, value in h.items():
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    def test_page_csrf_and_security_headers(self):
        status, headers, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(self.app.csrf.encode(), body)
        self.assertNotIn(b'__CSRF__', body)
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_host_dns_rebinding_refused(self):
        self.assertEqual(self.request('/', headers={'Host': 'evil.invalid'})[0], 403)

    def test_window_controls_module_is_static_and_hidden_without_desktop_bridge(self):
        status, headers, body = self.request('/window-controls.mjs')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
        self.assertIn(b'installWindowControls', body)
        page = self.request('/')[2]
        self.assertIn('aria-label="窗口控制" hidden'.encode('utf-8'), page)

    def test_cross_origin_get_refused(self):
        self.assertEqual(self.request('/api/library', headers={'Origin': 'https://evil.invalid'})[0], 403)

    def test_desktop_lyrics_module_is_narrow_static_and_requires_native_bridge(self):
        status, headers, body = self.request('/desktop-lyrics.mjs')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
        self.assertIn(b'installDesktopLyrics', body)
        self.assertTrue(json.loads(self.request('/api/features')[2])['desktopLyrics'])
        self.assertIn('aria-label="桌面歌词" title="桌面歌词" aria-pressed="false" hidden'.encode(), self.request('/')[2])
        self.assertEqual(self.request('/desktop/lyrics/index.html')[0], 404)

    def test_cross_site_browser_get_refused(self):
        self.assertEqual(self.request('/', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_post_requires_csrf(self):
        self.assertEqual(self.request_headers('/api/favorite', {'X-Talk-CSRF': 'wrong'})[0], 403)
        self.assertEqual(self.app.library.snapshot()['favorites'], [])

    def test_post_requires_same_origin(self):
        self.assertEqual(self.request_headers('/api/favorite', {'Origin': 'http://evil.invalid'})[0], 403)
        self.assertEqual(self.app.library.snapshot()['favorites'], [])

    def test_no_private_file_routes(self):
        for path in ['/.private/library.sqlite3', '/../src/server.py', '/%2e%2e/src/server.py', '/src/bridge.py']:
            self.assertEqual(self.request(path)[0], 404)

    def test_json_required(self):
        self.assertEqual(self.request_headers('/api/state', {'Content-Type': 'text/plain'})[0], 415)

    def test_nan_and_malformed_json_rejected(self):
        for raw in ['{"volume":NaN}', '{', '[]']:
            self.assertEqual(self.request('/api/state', raw=raw)[0], 400)

    def test_body_limit(self):
        status, _, body = self.request_headers('/api/state', length=128 * 1024 + 1)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body), {'code': 'BODY_LIMIT'})

    def test_favorite_api(self):
        status, _, body = self.request('/api/favorite', {'key': TRACK['mid'], 'enabled': True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['favorites'], [TRACK_KEY])

    def test_playlist_api(self):
        status, _, body = self.request('/api/playlists', {'name': 'test'})
        self.assertEqual(status, 201)
        pid = json.loads(body)['id']
        self.assertEqual(self.request('/api/playlist', {'id': pid, 'action': 'add', 'value': TRACK['mid']})[0], 200)

    def test_search_and_validation(self):
        status, _, body = self.request('/api/search', {'query': '晴天'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['tracks'][0]['title'], '晴天')
        self.assertEqual(self.request('/api/search', {'query': '', 'page': 1})[0], 400)
        self.assertEqual(self.request('/api/search', {'query': '晴天', 'page': 0})[0], 400)

    def test_source_job_error_is_sanitized(self):
        status, _, body = self.request('/api/play', {'key': TRACK['mid']})
        self.assertEqual(status, 202)
        jid = json.loads(body)['job']
        for _ in range(50):
            row = json.loads(self.request('/api/jobs/' + jid)[2])
            if row['status'] != 'loading': break
            time.sleep(.01)
        self.assertEqual(row['code'], 'LOGIN_REQUIRED')
        self.assertNotIn('exception', row)
        self.assertNotIn('path', row)

    def test_busy_refuses_parallel_provider_work(self):
        self.app.network.acquire()
        try: self.assertEqual(self.request('/api/search', {'query': '晴天'})[0], 409)
        finally: self.app.network.release()

    def test_media_ranges_head_and_expiration(self):
        path = Path(self.temp.name) / 'synthetic.bin'; path.write_bytes(bytes(range(100)))
        token = 'A' * 43
        self.app.media[token] = {'path': path, 'expires': time.time() + 10}
        status, headers, body = self.request('/media/' + token, headers={'Range': 'bytes=4-9'})
        self.assertEqual((status, body), (206, bytes(range(4, 10))))
        self.assertEqual(headers['Content-Range'], 'bytes 4-9/100')
        self.assertEqual(self.request('/media/' + token, method='HEAD')[2], b'')
        self.assertEqual(self.request('/media/' + token, headers={'Range': 'bytes=101-'})[0], 416)
        self.app.media[token]['expires'] = 0
        self.assertEqual(self.request('/media/' + token)[0], 410)

    def test_invalid_provider_and_cover_do_not_start_network(self):
        self.assertEqual(self.request('/api/auth/start', {'provider': 'invalid'})[0], 400)
        self.assertEqual(self.request('/api/cover/../bad')[0], 404)

    def test_fixed_public_assets_and_head(self):
        for path in ['/styles.css', '/app.js', '/assets/brand.svg', '/assets/cover-default.svg']:
            self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(self.request(path, method='HEAD')[2], b'')

    def test_unsupported_methods_have_no_cors_or_mutation(self):
        for method in ['OPTIONS', 'PUT', 'DELETE', 'PATCH', 'TRACE']:
            status, headers, body = self.request('/api/state', method=method)
            self.assertEqual(status, 405)
            self.assertNotIn('Access-Control-Allow-Origin', headers)
            self.assertEqual(json.loads(body)['code'], 'METHOD_NOT_ALLOWED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
