"""Optional extension/host contracts. Fixtures do not claim model accuracy."""
import http.client
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
from server import Player, Server
from library import PlayerError
from score_extension import ScoreExtension, ManagedLab, clip_options
from capabilities.score_lab.common import atomic_json, read_json, sample_dir

TRACK = {'mid': '0039MnYb0qxYhV', 'songId': 97773, 'title': 'fixture', 'artist': 'artist',
         'album': 'album', 'albumMid': '000MkMni19ClKG', 'duration': 269}


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.player = Player(self.root, live=False)
        self.extension = ScoreExtension(self.player)
        self.player._score_extension = self.extension
        config = self.extension.config
        config.update(dataDir=self.root / 'scores', configPath=self.root / 'runtime.json',
                      python=self.root / 'runtime/python.exe', sheetRepo=self.root / 'sheet',
                      baseRepo=self.root / 'base', assetsDir=self.root / 'assets')
        for p in (config['python'], config['sheetRepo'] / 'model.safetensors',
                  config['baseRepo'] / 'model.safetensors', config['assetsDir'] / 'manifest.json'):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'test fixture, never executable')
        # Hold dispatch deliberately. These tests exercise persistence/backpressure,
        # not inference. Separate live QA executes the real installed model.
        self.dispatch_patch = patch.object(ManagedLab, 'dispatch', lambda lab: lab.closed.wait())
        self.dispatch_patch.start()
        self.server = Server(('127.0.0.1', 0), self.player)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.extension.close()
        self.dispatch_patch.stop()
        self.player.library.close()
        self.temp.cleanup()

    def request(self, path, data=None, method=None, headers=None):
        auth = {'Origin': self.origin, 'X-Talk-CSRF': self.player.csrf}
        if data is not None:
            auth['Content-Type'] = 'application/json'
            data = json.dumps(data).encode('utf8')
        auth.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method or ('POST' if data is not None else 'GET'), path, body=data, headers=auth)
        response = connection.getresponse()
        status, out, body = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, out, body

    def enable(self):
        self.assertEqual(self.request('/api/extensions/score-lab', {'enabled': True})[0], 200)

    def fixture(self):
        self.enable()
        lab = self.extension.get_lab()
        source = self.root / 'audio.mp3'; source.write_bytes(bytes(range(100)))
        sid = lab.create({'title': 'test', 'artist': '', 'source': 'local', 'offset': 0,
                          'duration': 30, 'full': False},
                         {'input': 'input.mp3', 'offset': 0, 'seconds': 30}, input_file=source)
        return lab, sid, sample_dir(lab.config, sid)

    def test_disabled_is_lazy_and_does_not_start_model(self):
        self.assertFalse(self.extension.describe()['enabled'])
        self.assertIsNone(self.extension.lab)
        self.assertEqual(self.request('/api/features')[0], 200)
        self.assertIsNone(self.extension.lab)
        self.assertEqual(self.request('/extensions/score-lab/api/catalog')[0], 409)
        self.assertIsNone(self.extension.lab)

    def test_state_is_persistent_and_private(self):
        self.enable()
        self.assertTrue(read_json(self.extension.folder / 'state.json')['enabled'])
        body = self.request('/api/extensions/score-lab')[2]
        self.assertNotIn(str(self.root).encode(), body)
        self.assertEqual(self.request('/api/extensions/score-lab', {'enabled': 1})[0], 400)

    def test_mutations_require_host_origin_and_csrf(self):
        for headers in ({'X-Talk-CSRF': ''}, {'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
            self.assertEqual(self.request('/api/extensions/score-lab', {'enabled': True}, headers=headers)[0], 403)
        self.assertFalse(self.extension.enabled)

    def test_frame_is_same_origin_not_cross_origin(self):
        status, headers, _ = self.request('/extensions/score-lab/')
        self.assertEqual(status, 200)
        self.assertEqual(headers['X-Frame-Options'], 'SAMEORIGIN')

    def test_print_assets_and_bounded_saved_notation_snapshot(self):
        for asset in ('print.html', 'print.mjs', 'print-layout.mjs', 'print.css'):
            self.assertEqual(self.request('/extensions/score-lab/' + asset)[0], 200)
        lab, sid, directory = self.fixture()
        url = f'/extensions/score-lab/api/samples/{sid}/notation'
        self.assertEqual(self.request(url)[0], 422)
        atomic_json(directory / 'status.json', {'state': 'completed'})
        atomic_json(directory / 'result.json', {'abc': 'X:1\nK:C\nC|\n'})
        status, headers, body = self.request(url)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['abc'], 'X:1\nK:C\nC|\n')
        self.assertNotIn(str(directory).encode(), body)
        for suffix in ('?revision=../secret', '?revision=', '?revision=a&revision=b', '?path=secret'):
            self.assertEqual(self.request(url + suffix)[0], 422)
        self.assertEqual(self.request(url, headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertIn("frame-ancestors 'self'", headers['Content-Security-Policy'])
        self.assertIn("frame-src 'self'", self.request('/')[1]['Content-Security-Policy'])

    def test_explicit_host_and_module_static_files(self):
        for path in ('/score-extension.mjs', '/score-extension.css',
                     '/extensions/score-lab/embedded.css', '/extensions/score-lab/app.mjs'):
            self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(self.request('/extensions/score-lab/api/catalog')[0], 409)

    def test_upload_is_streamed_and_committed_with_owned_file(self):
        self.enable()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('POST', '/extensions/score-lab/api/import?name=fixture.mp3&seconds=full',
            body=b'audio fixture', headers={'Origin': self.origin, 'X-Talk-CSRF': self.player.csrf,
                                           'Content-Type': 'application/octet-stream'})
        response = conn.getresponse(); body = json.loads(response.read()); conn.close()
        self.assertEqual(response.status, 202)
        directory = sample_dir(self.extension.config, body['id'])
        self.assertEqual((directory / 'input.mp3').read_bytes(), b'audio fixture')
        self.assertIsNone(read_json(directory / 'request.json')['seconds'])
        self.assertTrue(read_json(directory / 'sample.json')['full'])
        self.assertFalse(list((self.extension.config['dataDir'] / 'uploads').iterdir()))

    def test_cancel_retry_preserves_input_and_invalidates_old_queue_generation(self):
        lab, sid, directory = self.fixture()
        self.assertEqual(self.request(f'/extensions/score-lab/api/samples/{sid}/cancel', {})[0], 200)
        self.assertEqual(read_json(directory / 'status.json')['state'], 'cancelled')
        self.assertEqual(self.request(f'/extensions/score-lab/api/samples/{sid}/retry', {})[0], 202)
        self.assertEqual(lab.generations[sid], 2)
        self.assertTrue((directory / 'input.mp3').is_file())
        old, new = lab.jobs.get_nowait(), lab.jobs.get_nowait()
        self.assertNotEqual(old['generation'], new['generation'])
        lab.jobs.task_done(); lab.jobs.task_done()

    def test_backpressure(self):
        self.fixture(); self.fixture(); self.fixture()
        with self.assertRaises(PlayerError) as error:
            self.fixture()
        self.assertEqual(error.exception.code, 'SCORE_BUSY')

    def test_only_library_identity_not_arbitrary_path_can_be_submitted(self):
        self.enable()
        self.assertEqual(self.request('/api/scores/track', {'key': '../secret.mp3'})[0], 400)
        track = self.player.library.put([TRACK])[0]
        before = self.player.library.snapshot()['state']
        status, _, body = self.request('/api/scores/track', {'key': track['key'], 'seconds': 90})
        self.assertEqual(status, 202)
        sid = json.loads(body)['id']
        self.assertEqual(read_json(sample_dir(self.extension.config, sid) / 'sample.json')['source'], 'qq')
        self.assertEqual(self.player.library.snapshot()['state'], before)

    def test_runtime_absent_does_not_change_player_state(self):
        self.enable()
        self.extension.config['python'].unlink()
        track = self.player.library.put([TRACK])[0]
        self.assertEqual(self.request('/api/scores/track', {'key': track['key']})[0], 503)
        self.assertEqual(self.request('/api/library')[0], 200)

    def test_raw_files_and_partial_artifacts_never_served(self):
        lab, sid, directory = self.fixture()
        for path in ('bundle-config.json', 'data', 'config.local.json', 'api/samples/../../secret',
                     f'media/{sid}/worker.log', f'export/{sid}/../request.json'):
            self.assertEqual(self.request('/extensions/score-lab/' + path)[0], 404)
        self.assertEqual(self.request(f'/extensions/score-lab/media/{sid}/original.wav')[0], 409)
        atomic_json(directory / 'status.json', {'state': 'completed', 'phase': 'ready'})
        (directory / 'original.wav').write_bytes(bytes(range(100)))
        status, headers, body = self.request(f'/extensions/score-lab/media/{sid}/original.wav', headers={'Range': 'bytes=10-19'})
        self.assertEqual((status, body), (206, bytes(range(10, 20))))
        self.assertEqual(headers['Content-Range'], 'bytes 10-19/100')

    def test_disable_closes_owned_queue_but_keeps_results(self):
        lab, sid, directory = self.fixture()
        self.assertEqual(self.request('/api/extensions/score-lab', {'enabled': False})[0], 200)
        self.assertTrue(lab.closed.is_set())
        self.assertIsNone(self.extension.lab)
        self.assertTrue((directory / 'input.mp3').is_file())
        self.assertEqual(read_json(directory / 'status.json')['phase'], 'interrupted')

    def test_invalid_clip_values(self):
        for data in ({'offset': True}, {'seconds': False}, {'seconds': float('nan')}, {'seconds': 901},
                     {'offset': -1}, {'offset': float('inf')}):
            with self.assertRaises(PlayerError): clip_options(data)
        self.assertEqual(clip_options({'offset': 40, 'seconds': 'full'}), (0, None))

    def editable_fixture(self):
        lab, sid, directory = self.fixture()
        task = lab.jobs.get_nowait(); lab.jobs.task_done()
        lab.pending_tasks.discard((sid, task['generation']))
        atomic_json(directory / 'status.json', {'state': 'completed', 'phase': 'ready'})
        result = directory / 'result'; result.mkdir()
        abc = 'X:1\nT:\nM:4/4\nL:1/4\nQ:1/4=60\nV: Vocal\nV: Ins\nV: Accomp clef=bass\nK:C\nV: Vocal\nC4|\nV: Ins\nZ|\nV: Accomp\n[C,E,G,]4|\n'
        (result / 'arrangement.abc').write_text(abc)
        (result / 'beat.lab').write_text('0 1 4 4\n1 2 4 4\n2 3 4 4\n3 4 4 4\n4 1 4 4\n')
        atomic_json(directory / 'result.json', {'duration': 4, 'tracks': [], 'measures': []})
        return lab, sid, directory, abc

    def test_editor_is_read_only_bootstrap_and_queues_immutable_draft(self):
        lab, sid, directory, abc = self.editable_fixture()
        status, _, body = self.request(f'/extensions/score-lab/api/samples/{sid}/editor')
        self.assertEqual(status, 200)
        current = json.loads(body)
        self.assertEqual((current['revision'], current['abc']), (0, abc))
        payload = {'abc': abc.replace('C4|', '^C4|'), 'baseRevision': 0,
                   'sourceFingerprint': current['sourceFingerprint']}
        status, _, body = self.request(f'/extensions/score-lab/api/samples/{sid}/edit', payload)
        self.assertEqual(status, 202)
        accepted = json.loads(body)
        self.assertRegex(accepted['revisionId'], r'^[a-f0-9]{24}$')
        self.assertEqual(read_json(directory / 'edit-status.json')['state'], 'queued')
        queued = lab.jobs.get_nowait(); lab.jobs.task_done()
        self.assertEqual(queued['kind'], 'edit')
        self.assertEqual(read_json(directory / queued['draft']), payload)
        self.assertEqual((directory / 'result/arrangement.abc').read_text(), abc)
        # A second tab cannot overwrite the accepted draft or enqueue a duplicate.
        self.assertEqual(self.request(f'/extensions/score-lab/api/samples/{sid}/edit', payload)[0], 409)

    def test_stale_edit_and_structural_changes_rejected_before_model_spawn(self):
        lab, sid, directory, abc = self.editable_fixture()
        current = json.loads(self.request(f'/extensions/score-lab/api/samples/{sid}/editor')[2])
        payload = {'abc': abc, 'baseRevision': 1, 'sourceFingerprint': current['sourceFingerprint']}
        self.assertEqual(self.request(f'/extensions/score-lab/api/samples/{sid}/edit', payload)[0], 409)
        payload['baseRevision'] = 0
        for invalid in (abc.replace('C4|', 'C3|'), abc.replace('V: Ins\nZ|\n', ''),
                        abc + '%%abc-include http://evil.example\n'):
            self.assertEqual(self.request(f'/extensions/score-lab/api/samples/{sid}/edit', {**payload, 'abc': invalid})[0], 422)
        self.assertTrue(lab.jobs.empty())
        self.assertFalse((directory / 'edits/current.json').exists())

    def test_unpublished_revision_and_media_not_on_allowlist_never_exposed(self):
        lab, sid, directory, abc = self.editable_fixture()
        for suffix in ('media/worker.log', 'export/edited.mid', 'media/piano_mix.wav', 'export/../../request.json'):
            self.assertEqual(self.request(f'/extensions/score-lab/revisions/{sid}/unpublished/{suffix}')[0], 404)


if __name__ == '__main__': unittest.main()
