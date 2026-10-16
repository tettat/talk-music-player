"""No user-profile writes: only synthetic fixtures and the owned packaging API."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('usic_package', ROOT / 'scripts/package_portable.py')
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class PackagingTests(unittest.TestCase):
    def test_private_names_are_rejected(self):
        for name in ('.private/credential.dpapi', 'config.local.json', '.venv/pyvenv.cfg',
                     'cache/personal.mp3', 'src/library.sqlite3', '../secret.py', '/secret.py',
                     '.env', 'web/model.onnx', 'logs/user.log'):
            self.assertFalse(package.allowed_name(name), name)
        self.assertTrue(package.allowed_name('desktop/packaging.cjs'))

    def test_sensitive_text_rejected_without_echoing_value(self):
        token = b'ghp_' + b'Z' * 36
        credential = b'{"access_token":"' + b'Q' * 32 + b'"}'
        home = b'C:' + bytes([92]) + b'Users' + bytes([92]) + b'SyntheticDevice'
        for content in (token, credential, home):
            issues = package.audit_text('synthetic.txt', content)
            self.assertTrue(issues)
            self.assertNotIn(content.decode(), ';'.join(issues))

    def test_zip_traversal_rejected(self):
        with tempfile.TemporaryDirectory(prefix='Usic 中文 zip ') as temporary:
            root = Path(temporary)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('../escape.txt', 'synthetic')
            with self.assertRaisesRegex(ValueError, 'ZIP_TRAVERSAL_REJECTED'):
                package.safe_extract(archive, root / 'out')
            self.assertFalse((root / 'escape.txt').exists())

    def test_cleanup_never_accepts_user_directory(self):
        with tempfile.TemporaryDirectory(prefix='Usic cleanup ') as temporary:
            root = Path(temporary)
            sentinel = root / 'keep.txt'
            sentinel.write_text('keep')
            with self.assertRaisesRegex(ValueError, 'UNSAFE_STAGING_DELETE'):
                package.clean_owned(root)
            self.assertTrue(sentinel.is_file())

    def test_unlisted_file_detected(self):
        with tempfile.TemporaryDirectory(prefix='Usic allowlist ') as temporary:
            root = Path(temporary)
            (root / 'app/.private').mkdir(parents=True)
            (root / 'app/.private/session.dpapi').write_bytes(b'synthetic')
            with self.assertRaisesRegex(ValueError, 'UNLISTED_APP_FILE'):
                package.scan_tree(root)


if __name__ == '__main__':
    unittest.main()
