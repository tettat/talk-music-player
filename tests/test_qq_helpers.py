"""The extracted critical login/media helpers no longer need a sibling experiment."""
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from providers.qq import adapter, cookie_transport, windows_secret


class QQHelperTests(unittest.TestCase):
    def test_local_modules_only_allow_reviewed_helpers(self):
        for name in ('windows_secret', 'member_source', 'cookie_transport'):
            self.assertIsNotNone(adapter.local_module(name))
        with self.assertRaises(ValueError): adapter.local_module('../settings')

    def test_private_path_escape_is_rejected_before_access(self):
        with self.assertRaisesRegex(ValueError, 'PRIVATE_PATH_ESCAPE'):
            adapter.private_path('../escape')

    def test_empty_response_cookie_preserved(self):
        self.assertEqual(dict(cookie_transport.cookie_snapshot({'empty': '', 'normal': 'value'})),
                         {'empty': '', 'normal': 'value'})

    def test_response_cookie_snapshot_is_immutable(self):
        result = cookie_transport.cookie_snapshot({'empty': ''})
        with self.assertRaises(TypeError): result['empty'] = 'changed'

    def test_unexpected_cookie_types_rejected(self):
        for jar in ([('x', 'y')], {'x': None}, {1: 'value'}):
            with self.assertRaises(ValueError): cookie_transport.cookie_snapshot(jar)

    @unittest.skipUnless(sys.platform == 'win32', 'DPAPI Windows-only')
    def test_dpapi_roundtrip_is_ciphertext_not_plaintext(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'credential.dpapi'
            value = 'synthetic-secret-not-a-user-session'
            windows_secret.save(path, value)
            self.assertNotIn(value.encode(), path.read_bytes())
            self.assertEqual(windows_secret.load(path), value)

    def test_plaintext_credentials_are_never_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'credential.dpapi'
            path.write_text('not-real-session')
            with self.assertRaisesRegex(ValueError, 'PROTECTED_SECRET_FORMAT_REQUIRED'):
                windows_secret.load(path)

    def test_runtime_guard_does_not_accept_unconfigured_python(self):
        with patch.object(adapter.sys, 'prefix', str(Path(tempfile.gettempdir()) / 'wrong-runtime')):
            self.assertEqual(adapter.guard(), 'CONFIGURED_VENV_REQUIRED')


if __name__ == '__main__': unittest.main(verbosity=2)
