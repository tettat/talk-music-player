import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('audit_source', Path(__file__).resolve().parents[1] / 'scripts/audit_source.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class SourceAuditTests(unittest.TestCase):
    def test_private_and_generated_files_rejected(self):
        for name in ('.private/library.sqlite3', 'config.local.json', 'artifacts/process.json',
                     'node_modules/anything.js', 'cache/credential.dpapi', 'song.mp3', '.env.local'):
            self.assertEqual(audit.check_path(name), 'PRIVATE_FILE')

    def test_source_and_examples_allowed(self):
        for name in ('src/bridge.py', 'config.example.json', 'requirements/qq-windows-py312.lock',
                     'analysis/basic_pitch/vendor/basic_pitch/inference.py', 'README.md'):
            self.assertIsNone(audit.check_path(name))

    def test_token_shape_is_flagged_without_echoing_value(self):
        value = 'gh' + 'p_' + 'x' * 30
        self.assertEqual(audit.check_text(value), ['GITHUB_TOKEN_SHAPE'])

    def test_device_specific_home_not_generic_env_path_is_flagged(self):
        value = 'C:' + '\\Users\\' + 'synthetic\\runtime'
        self.assertEqual(audit.check_text(value), ['DEVICE_HOME_PATH'])
        self.assertEqual(audit.check_text('Path(os.environ["SystemRoot"]) / "System32"'), [])


if __name__ == '__main__': unittest.main(verbosity=2)
