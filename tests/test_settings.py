"""Configuration, extraction, and clean-checkout contracts; no real account needed."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from settings import load_settings
from providers import ProviderRegistry
from library import PlayerError
from server import Player


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='talk settings ')
        # Hosted Windows may expose TEMP through an 8.3 alias. Settings deliberately
        # canonicalize real paths; compare the same filesystem identity, not spelling.
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        (self.root / 'config.local.json').write_text(json.dumps(value), encoding='utf-8')

    def test_default_paths_are_checkout_relative_not_cwd(self):
        result = load_settings(self.root, {})
        self.assertEqual(result.data_dir, self.root / '.private')
        self.assertEqual(result.qq_session_dir, self.root / '.private/qq')
        self.assertTrue(result.python.is_relative_to(self.root))

    def test_machine_paths_are_explicit_local_overrides(self):
        self.write({'data_dir': 'user data', 'python': 'runtime/Scripts/python.exe', 'port': 43220})
        result = load_settings(self.root, {})
        self.assertEqual(result.data_dir, self.root / 'user data')
        self.assertEqual(result.port, 43220)

    def test_env_path_overrides_file_without_exporting_secrets(self):
        self.write({'qq_session_dir': 'original'})
        result = load_settings(self.root, {'TALK_QQ_SESSION_DIR': 'alternate'})
        self.assertEqual(result.qq_session_dir, self.root / 'alternate')

    def test_explicit_file_relative_to_checkout(self):
        (self.root / 'custom.json').write_text('{"port":43221}')
        self.assertEqual(load_settings(self.root, {'TALK_CONFIG_FILE': 'custom.json'}).port, 43221)

    def test_explicit_missing_file_never_silently_uses_defaults(self):
        with self.assertRaisesRegex(ValueError, 'CONFIG_FILE_NOT_FOUND'):
            load_settings(self.root, {'TALK_CONFIG_FILE': 'missing.json'})

    def test_unknown_keys_and_non_object_rejected(self):
        for value in ({'password': 'not-a-real-secret'}, [], None, 'invalid'):
            self.write(value)
            with self.assertRaisesRegex(ValueError, 'CONFIG_KEYS_INVALID'):
                load_settings(self.root, {})

    def test_invalid_numbers_and_booleans_rejected(self):
        for key, value in [('port', True), ('port', 0), ('port', 65536), ('port', '43180'),
                           ('ttl_seconds', 0), ('ttl_seconds', 172801), ('schema_version', True)]:
            self.write({key: value})
            with self.assertRaises(ValueError): load_settings(self.root, {})

    def test_invalid_paths_rejected(self):
        for value in ('', '  ', None, 42, 'a\x00b'):
            self.write({'data_dir': value})
            with self.assertRaisesRegex(ValueError, 'CONFIG_PATH_INVALID'):
                load_settings(self.root, {})

    def test_bom_is_supported(self):
        (self.root / 'config.local.json').write_text('{"port":43222}', encoding='utf-8-sig')
        self.assertEqual(load_settings(self.root, {}).port, 43222)

    def test_oversized_file_rejected(self):
        (self.root / 'config.local.json').write_text(' ' * 32769)
        with self.assertRaisesRegex(ValueError, 'CONFIG_SIZE_REJECTED'):
            load_settings(self.root, {})

    def test_fresh_player_contains_no_prototype_library(self):
        app = Player(self.root / 'fresh', live=False)
        try:
            app.seed()
            self.assertEqual(app.library.snapshot()['library'], [])
        finally: app.library.close()


class RegistryTests(unittest.TestCase):
    def test_only_real_registered_sources_are_advertised(self):
        qq = SimpleNamespace(id='qq', name='QQ Music')
        registry = ProviderRegistry([qq])
        self.assertEqual([p['id'] for p in registry.describe()], ['qq'])
        self.assertIs(registry.get('qq'), qq)

    def test_unsupported_source_cannot_fall_back_to_qq(self):
        with self.assertRaises(PlayerError) as caught:
            ProviderRegistry().get('netease')
        self.assertEqual(caught.exception.code, 'PROVIDER_UNAVAILABLE')

    def test_duplicate_source_rejected(self):
        qq = SimpleNamespace(id='qq', name='QQ Music')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_PROVIDER'):
            ProviderRegistry([qq, qq])


if __name__ == '__main__': unittest.main(verbosity=2)
