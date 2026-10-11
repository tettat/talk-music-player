"""Synthetic subprocess boundaries; the separate live QA proves actual ONNX inference."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import analysis_runner as runner
from library import PlayerError


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.source = self.work / 'source.mp3'; self.source.write_bytes(b'synthetic-boundary-test')
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.model_sha = json.loads(runner.LOCK.read_text())['model']['sha256']

    def tearDown(self): self.temp.cleanup()

    def raw(self):
        return {'schemaVersion': 1, 'model': {'id': 'spotify/basic-pitch', 'version': '0.4.0', 'sha256': self.model_sha},
                'audio': {'sha256': self.sha, 'start': 30, 'seconds': 30, 'sampleRate': 22050}, 'notes': []}

    def execute(self, child):
        with patch.object(runner, 'ready', return_value=True), patch.object(runner.subprocess, 'run', side_effect=child):
            return runner.analyze(self.source, self.work, {'duration': 120}, self.sha)

    def test_copy_is_hash_checked_and_bad_copy_removed(self):
        dest = runner.copy_authorized(self.source, self.work, self.sha)
        self.assertEqual(dest.read_bytes(), self.source.read_bytes())
        with self.assertRaises(PlayerError): runner.copy_authorized(self.source, self.work, '0'*64)
        self.assertFalse(dest.exists())

    def test_only_ordinary_mp3_input_allowed(self):
        source = self.work / 'cache.mgg'; source.write_bytes(b'synthetic')
        with self.assertRaises(PlayerError): runner.copy_authorized(source, self.work, self.sha)

    def test_work_cleanup_cannot_target_external_private_directory(self):
        with self.assertRaises(PlayerError): runner.work_directory(self.work)

    def test_fixed_command_and_restricted_environment(self):
        def child(args, **kwargs):
            self.assertEqual(args[0], str(runner.PYTHON)); self.assertEqual(args[1:3], ['-I', '-B'])
            self.assertFalse(kwargs['shell']); self.assertEqual(kwargs['timeout'], 180)
            self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'], '1')
            self.assertNotIn('USERPROFILE', kwargs['env']); self.assertNotIn('QQ_COOKIE', kwargs['env'])
            (self.work/'result.json').write_text(json.dumps(self.raw()))
            return SimpleNamespace(returncode=0)
        self.assertEqual(self.execute(child)['notes'], [])

    def test_child_failure_and_timeout_are_sanitized(self):
        for child, code in [(lambda *a, **kw: SimpleNamespace(returncode=1), 'ANALYSIS_FAILED'),
                            (lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired('private', 180)), 'ANALYSIS_TIMEOUT')]:
            with self.assertRaises(PlayerError) as caught: self.execute(child)
            self.assertEqual(caught.exception.code, code)

    def test_tampered_hash_or_nonfinite_output_is_rejected(self):
        for variant in ('hash', 'nan'):
            def child(*args, **kwargs):
                raw=self.raw()
                if variant=='hash': raw['model']['sha256']='0'*64
                else: raw['audio']['seconds']=float('nan')
                (self.work/'result.json').write_text(json.dumps(raw))
                return SimpleNamespace(returncode=0)
            with self.assertRaises(PlayerError) as caught: self.execute(child)
            self.assertEqual(caught.exception.code, 'ANALYSIS_INVALID')


if __name__ == '__main__': unittest.main(verbosity=2)
