"""Synthetic cache/result-schema contracts, not live music analysis evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from library import Library, PlayerError
from insights import InsightStore, pitch_document, note_name

TRACK = {'mid': '0039MnYb0qxYhV', 'songId': 97773, 'title': '合成测试曲', 'artist': '测试',
         'album': '测试', 'duration': 120}
SHA = 'a' * 64


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / 'library.sqlite3'
        self.lib = Library(self.path); self.lib.put([TRACK]); self.store = InsightStore(self.lib, clock=lambda: 42)

    def tearDown(self):
        self.lib.close(); self.temp.cleanup()

    def test_empty_is_missing_not_fake_analysis(self):
        self.assertIsNone(self.store.get('lyrics', TRACK['mid']))
        self.assertIsNone(self.store.get('analysis', TRACK['mid']))

    def test_cache_persists_restart_without_changing_favorites(self):
        expected = self.store.put('analysis', TRACK['mid'], {'kind': 'pitch', 'estimated': True})
        self.lib.close(); self.lib = Library(self.path); self.store = InsightStore(self.lib)
        self.assertEqual(self.store.get('analysis', TRACK['mid']), expected)
        self.assertEqual(self.lib.snapshot()['favorites'], [])

    def test_cache_rejects_unknown_kind_nan_or_oversize(self):
        for kind, value in [('evil', {}), ('lyrics', {'n': float('nan')}), ('lyrics', {'n': 'x' * (2 * 1024**2)})]:
            with self.assertRaises(PlayerError): self.store.put(kind, TRACK['mid'], value)

    def test_unknown_track_is_not_created(self):
        with self.assertRaises(PlayerError): self.store.put('lyrics', '00123456789012', {})

    def test_search_pruning_preserves_new_insight_references(self):
        self.store.put('lyrics', TRACK['mid'], {'available': False})
        metadata=self.lib.db.execute('SELECT metadata FROM tracks WHERE key=?',('qq:'+TRACK['mid'],)).fetchone()[0]
        self.lib.db.executemany('INSERT INTO tracks(key,metadata,used) VALUES(?,?,NULL)',
                               [('qq:'+str(i).zfill(14),metadata) for i in range(9999)])
        self.lib.put([{**TRACK,'mid':'99999999999999'}])
        self.assertIsNotNone(self.store.get('lyrics',TRACK['mid']))
        self.assertEqual(self.lib.get(TRACK['mid'])['songId'],TRACK['songId'])

    def raw(self):
        return {'schemaVersion': 1, 'model': {'id': 'spotify/basic-pitch', 'sha256': 'b' * 64, 'version': 'test'},
                'audio': {'sha256': SHA, 'start': 30, 'seconds': 30, 'sampleRate': 22050},
                'notes': [{'start': 31, 'end': 32, 'midi': 60, 'confidence': .8}]}

    def validate(self, raw): return pitch_document(raw, expected_sha=SHA, start=30, seconds=30)

    def test_pitch_projection_is_small_safe_schema(self):
        raw = self.raw(); raw['privatePath'] = 'never-public'; raw['model']['token'] = 'never-public'
        result = self.validate(raw)
        self.assertNotIn('never-public', json.dumps(result))
        self.assertTrue(result['estimated']); self.assertEqual(result['metrics'][0]['value'], 1)

    def test_model_hash_audio_hash_clip_must_match(self):
        for where, key, value in [('model', 'sha256', 'bad'), ('audio', 'sha256', 'c' * 64),
                                  ('audio', 'start', 0), ('audio', 'seconds', 29), ('audio', 'sampleRate', 44100)]:
            raw = self.raw(); raw[where][key] = value
            with self.assertRaises(PlayerError): self.validate(raw)

    def test_bad_notes_rejected_without_silent_fake_result(self):
        for key, value in [('start', -1), ('end', 0), ('midi', True), ('confidence', float('nan'))]:
            raw = self.raw(); raw['notes'][0][key] = value
            with self.assertRaises(PlayerError): self.validate(raw)

    def test_zero_notes_is_legitimate_model_output(self):
        raw = self.raw(); raw['notes'] = []
        self.assertEqual(self.validate(raw)['notes'], [])

    def test_note_naming(self):
        self.assertEqual(note_name(60), 'C4'); self.assertEqual(note_name(69), 'A4')


if __name__ == '__main__': unittest.main(verbosity=2)
