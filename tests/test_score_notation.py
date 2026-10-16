"""Print snapshots are saved notation, not a new model or audio operation."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capabilities.score_lab.common import atomic_json
from capabilities.score_lab.notation import notation_snapshot, MAX_ABC_BYTES

ABC = 'X:1\nM:4/4\nL:1/8\nK:C\nC2 D2 E2 F2|\n'


class NotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {'dataDir': Path(self.temp.name)}
        self.directory = self.config['dataDir'] / 'samples/fixture'
        atomic_json(self.directory / 'sample.json', {'title': '晴天', 'artist': '测试', 'source': 'local'})
        atomic_json(self.directory / 'status.json', {'state': 'completed'})
        self.result = {'abc': ABC, 'arrangement': {'verified': True, 'abc': ABC + '% accompaniment\n'},
                       'privatePath': str(self.directory), 'audio': 'not-for-print'}
        atomic_json(self.directory / 'result.json', self.result)

    def tearDown(self):
        self.temp.cleanup()

    def test_original_prefers_verified_accompaniment_and_contains_no_paths_or_audio(self):
        value = notation_snapshot(self.config, 'fixture')
        self.assertEqual(value['abc'], self.result['arrangement']['abc'])
        self.assertEqual(set(value), {'id', 'revisionId', 'title', 'artist', 'abc'})
        self.assertIsNone(value['revisionId'])
        self.assertNotIn(str(self.directory), json.dumps(value))

    def test_unverified_arrangement_never_prints_as_authoritative(self):
        self.result['arrangement']['verified'] = False
        atomic_json(self.directory / 'result.json', self.result)
        self.assertEqual(notation_snapshot(self.config, 'fixture')['abc'], ABC)

    def test_requested_saved_revision_not_latest_or_original_is_used(self):
        with patch('capabilities.score_lab.editing.read_revision_result', return_value={'abc': ABC + '% saved edit'}) as read:
            value = notation_snapshot(self.config, 'fixture', 'revision-1')
        read.assert_called_once_with(self.config, 'fixture', 'revision-1')
        self.assertEqual(value['revisionId'], 'revision-1')
        self.assertTrue(value['abc'].endswith('% saved edit'))

    def test_unfinished_or_missing_notation_never_exports(self):
        atomic_json(self.directory / 'status.json', {'state': 'running'})
        with self.assertRaises(ValueError): notation_snapshot(self.config, 'fixture')
        atomic_json(self.directory / 'status.json', {'state': 'completed'})
        atomic_json(self.directory / 'result.json', {'abc': ''})
        with self.assertRaises(ValueError): notation_snapshot(self.config, 'fixture')

    def test_paths_revision_injection_and_oversized_abc_are_rejected(self):
        for revision in ('../private', '', 'x' * 65, {}, 1):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                notation_snapshot(self.config, 'fixture', revision)
        with self.assertRaises(ValueError): notation_snapshot(self.config, '../private')
        atomic_json(self.directory / 'result.json', {'abc': 'x' * (MAX_ABC_BYTES + 1)})
        with self.assertRaises(ValueError): notation_snapshot(self.config, 'fixture')
