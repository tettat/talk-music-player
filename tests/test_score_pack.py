"""Small synthetic pack fixtures; no downloads, models, venv or app edits."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


MODULE = Path(__file__).resolve().parents[1] / "capabilities/score_lab/build_pack.py"
SPEC = importlib.util.spec_from_file_location("score_pack_builder", MODULE)
pack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack)


class PackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="score-pack-unit-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, rel, data=b"fixture"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_relative_paths_match_native_protocol(self):
        for name in ("python/python.exe", "models/sheetsage2/config.json", "中文 目录/文件"):
            self.assertEqual(pack.relative_name(name), name)
        for name in ("", "/x", "C:/private", "a\\b", "a//b", "a/./b", "a/../b", "../x", "a\0b", "."):
            with self.subTest(name=name), self.assertRaises(pack.PackError):
                pack.relative_name(name)

    def test_containment_and_missing_future_file(self):
        self.assertEqual(pack.contained(self.root, "new/file"), self.root / "new/file")
        with self.assertRaises(pack.PackError):
            pack.contained(self.root, "../escape")

    def test_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        root = self.root / "payload"
        root.mkdir()
        try:
            (root / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Windows symlink privilege unavailable")
        with self.assertRaises(pack.PackError):
            pack.contained(root, "link/file")

    def test_private_paths_are_detected_in_binary_and_json_forms(self):
        private = str(self.root / "private-home")
        scanner = pack.PrivacyScanner([private])
        for form in (private, private.swapcase(), private.replace("\\", "/"), private.replace("\\", "\\\\")):
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                with self.subTest(form=form, encoding=encoding), self.assertRaises(pack.PackError):
                    scanner.check_stream(io.BytesIO(b"binary\0" + form.encode(encoding)), "payload.dll")

    def test_private_path_inside_zip_is_detected(self):
        path = self.root / "library.zip"
        private = self.root / "private-home"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("compiled.pyc", str(private).encode())
        with self.assertRaises(pack.PackError):
            pack.PrivacyScanner([private]).check_file(path, "library.zip")

    def test_safe_file_has_no_false_positive(self):
        path = self.put("safe.py", b"# stdlib/application relative paths only\n")
        pack.PrivacyScanner([self.root / "private-home"]).check_file(path, "safe.py")

    def test_installation_hooks_and_private_state_are_discarded(self):
        for rel in ("direct_url.json", "pkg-1.dist-info/direct_url.json", "distutils-precedence.pth",
                    "editable.egg-link", "__editable___pkg_finder.py", "pkg/__pycache__/a.pyc",
                    "pkg/tests/output.wav", "pkg/test/out.json", "worker.log", "private.db"):
            with self.subTest(rel=rel):
                self.assertIsNotNone(pack.excluded(rel))
        for rel in ("pkg/__init__.py", "pkg/data.npz", "pkg/lib/engine.dll", "pkg/testing/__init__.py",
                    "pkg/tests/LICENSE", "pkg-1.dist-info/licenses/LICENSE.external", "pkg/LICENCE",
                    "music21/test/testRunner.py", "music21/test/test_chord.py"):
            with self.subTest(rel=rel):
                self.assertIsNone(pack.excluded(rel))
        self.assertIsNotNone(pack.excluded("music21/test/__pycache__/testRunner.pyc"))
        self.assertIsNotNone(pack.excluded("music21/test/output.log"))

    def test_copy_requires_original_hash_and_does_not_modify_input(self):
        source = self.put("source.bin", b"immutable source")
        before = source.read_bytes()
        expected = hashlib.sha256(before).hexdigest()
        target = self.root / "payload/output.bin"
        self.assertEqual(pack.copy_checked(source, target, expected), expected)
        self.assertEqual(source.read_bytes(), before)
        source.write_bytes(b"tampered source")
        with self.assertRaises(pack.PackError):
            pack.copy_checked(source, self.root / "payload/rejected.bin", expected)
        self.assertFalse((self.root / "payload/rejected.bin").exists())

    def test_copy_accepts_record_sha256_encoding(self):
        source = self.put("source.bin", b"record fixture")
        value = base64.urlsafe_b64encode(hashlib.sha256(source.read_bytes()).digest()).decode().rstrip("=")
        pack.copy_checked(source, self.root / "target.bin", "sha256=" + value)
        with self.assertRaises(pack.PackError):
            pack.copy_checked(source, self.root / "target2.bin", "md5=invalid")

    def fixture_dist(self, files=None):
        site = self.root / "venv/Lib/site-packages"
        data = {"example/__init__.py": b"VALUE = 1\n",
                "example-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: example\nVersion: 1.0\nLicense: MIT\n",
                "example-1.0.dist-info/LICENSE": b"Example license fixture"}
        data.update(files or {})
        rows = []
        for rel, body in data.items():
            target = site / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            value = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
            rows.append([rel, "sha256=" + value, len(body)])
        rows.append(["example-1.0.dist-info/RECORD", "", ""])
        with (site / "example-1.0.dist-info/RECORD").open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows(rows)
        return site

    def test_only_record_owned_third_party_files_are_selected(self):
        site = self.fixture_dist({"example-1.0.dist-info/direct_url.json": b'{"url":"file:///private"}',
                                  "example.pth": b"import arbitrary\n", "example/tests/result.json": b"{}"})
        (site / "unowned-secret.json").write_text("private account fixture", encoding="utf-8")
        plans, packages, trimmed = pack.dependencies(site)
        selected = [r for _, r, _ in plans]
        self.assertEqual(len(selected), 3)
        self.assertEqual(packages[0]["version"], "1.0")
        self.assertIn("installation-metadata", trimmed)
        self.assertFalse(any("secret" in r or r.endswith(".pth") or "direct_url" in r for r in selected))

    def test_missing_record_owned_file_is_rejected(self):
        site = self.fixture_dist()
        (site / "example/__init__.py").unlink()
        with self.assertRaises(pack.PackError):
            pack.dependencies(site)

    def test_external_installed_launchers_and_docs_are_never_copied(self):
        site = self.fixture_dist()
        record = site / "example-1.0.dist-info/RECORD"
        with record.open("a", encoding="utf-8") as stream:
            stream.write("../../Scripts/example.exe,sha256=not-used,12\n../../share/man/example.1,sha256=not-used,12\n")
        plans, _, trimmed = pack.dependencies(site)
        self.assertEqual(len(plans), 3)
        self.assertEqual(trimmed["console-launcher-or-external-doc"], 2)
        with record.open("a", encoding="utf-8") as stream:
            stream.write("../../../../other-home/account.json,sha256=not-used,12\n")
        with self.assertRaises(pack.PackError):
            pack.dependencies(site)

    def test_unhashed_runtime_file_is_rejected(self):
        site = self.fixture_dist()
        record = site / "example-1.0.dist-info/RECORD"
        with record.open("a", encoding="utf-8") as stream:
            stream.write("example/__init__.py,,10\n")
        with self.assertRaises(pack.PackError):
            pack.dependencies(site)

    def test_regenerated_record_has_no_dangling_source_state(self):
        site = self.fixture_dist({"example-1.0.dist-info/direct_url.json": b"{}"})
        plans, packages, _ = pack.dependencies(site)
        payload = self.root / "payload"
        for source, rel, expected in plans:
            pack.copy_checked(source, payload / rel, expected)
        pack.regenerate_records(payload, packages)
        new = payload / "python/Lib/site-packages/example-1.0.dist-info/RECORD"
        text = new.read_text(encoding="utf-8")
        self.assertNotIn("direct_url", text)
        self.assertNotIn("../", text)
        self.assertIn("example/__init__.py,sha256=", text)
        self.assertIn("example-1.0.dist-info/RECORD,,", text)

    def test_pth_and_bootstrap_do_not_inject_repository_or_cwd(self):
        self.assertEqual(pack.PTH.splitlines(), ["python312.zip", ".", "Lib/site-packages", "import site"])
        self.assertNotIn("sys.path", pack.BOOTSTRAP.replace("No path injection", ""))
        self.assertIn("is_relative_to(_root)", pack.BOOTSTRAP)
        self.assertIn("site.ENABLE_USER_SITE = False", pack.BOOTSTRAP)
        self.assertNotIn(str(pack.ROOT), pack.BOOTSTRAP)

    def test_qa_environment_does_not_inherit_secrets_or_python(self):
        scratch = self.root / "qa"
        scratch.mkdir()
        with patch.dict("os.environ", {"PRIVATE_ACCOUNT_TOKEN": "secret", "PYTHONPATH": "host", "PATH": "host-python"}):
            env = pack.qa_environment(scratch)
        self.assertNotIn("PRIVATE_ACCOUNT_TOKEN", env)
        self.assertNotIn("host-python", env["PATH"])
        self.assertNotEqual(env["PYTHONPATH"], "host")
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")

    def test_scratch_delete_guard_never_accepts_parent_or_external_root(self):
        build = self.root / "build"
        build.mkdir()
        with patch.object(pack, "BUILD", build):
            for target in (build, self.root, self.root / "outside"):
                with self.subTest(target=target), self.assertRaises(pack.PackError):
                    pack.clean_scratch(target)
            owned = build / "owned"
            owned.mkdir()
            pack.clean_scratch(owned)
            self.assertFalse(owned.exists())

    def test_output_locations_are_workspace_contained(self):
        with patch.object(pack, "ROOT", self.root):
            pack.check_output_locations()
            with patch.object(pack, "NAME", "../outside"):
                with self.assertRaises(pack.PackError):
                    pack.check_output_locations()

    def fixture_manifest(self):
        payload = self.root / "payload"
        app = self.root / "app"
        payload.mkdir()
        app.mkdir()
        for rel in pack.PATHS.values():
            target = payload / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel.endswith(".exe"):
                target.write_bytes(b"exe fixture")
            else:
                target.mkdir(exist_ok=True)
        (payload / "python/python312._pth").write_text(pack.PTH, encoding="utf-8")
        code = app / "capabilities/score_lab"
        code.mkdir(parents=True)
        pack.write_json(code / "model.lock.json", {"repositories": {"sheetRepo": {}, "baseRepo": {}}})
        pack.write_json(code / "render-assets.lock.json", {"files": {}})
        shutil.copyfile(code / 'render-assets.lock.json', payload / 'render-assets/manifest.json')
        files, _ = pack.inventory(payload)
        pack.write_json(payload / "score-pack.json", {"schema": 1, **pack.PATHS, "files": files})
        return payload, app

    def test_manifest_covers_every_file_and_counts_itself_separately(self):
        payload, app = self.fixture_manifest()
        with patch.object(pack, "ROOT", app):
            result = pack.verify_pack(payload)
            self.assertTrue(result["passed"])
            self.assertEqual(result["filesCount"], result["hashedFilesCount"] + 1)
            (payload / "extra.txt").write_text("undeclared", encoding="utf-8")
            with self.assertRaises(pack.PackError):
                pack.verify_pack(payload)

    def test_manifest_detects_tamper_and_source_pin_mismatch(self):
        payload, app = self.fixture_manifest()
        with patch.object(pack, "ROOT", app):
            original = (payload / "python/python.exe").read_bytes()
            (payload / "python/python.exe").write_bytes(b"tampered")
            with self.assertRaises(pack.PackError):
                pack.verify_pack(payload)
            (payload / "python/python.exe").write_bytes(original)
            pack.write_json(app / "capabilities/score_lab/model.lock.json", {"repositories": {"sheetRepo": {"model.safetensors": "0" * 64}, "baseRepo": {}}})
            with self.assertRaises(pack.PackError):
                pack.verify_pack(payload)

    def test_host_readiness_marker_is_part_of_payload_not_just_worker_assets(self):
        payload, app = self.fixture_manifest()
        with patch.object(pack, 'ROOT', app):
            marker = payload / 'render-assets/manifest.json'
            marker.unlink()
            files, _ = pack.inventory(payload)
            pack.write_json(payload / 'score-pack.json', {'schema': 1, **pack.PATHS, 'files': files})
            with self.assertRaisesRegex(pack.PackError, 'readiness manifest'):
                pack.verify_pack(payload)

    def test_historical_python_archive_pin_and_license_pins_are_explicit(self):
        self.assertEqual(len(pack.PYTHON_SHA), 64)
        self.assertIn("3.12.10-embed-amd64.zip", pack.PYTHON_URL)
        for spec in pack.LICENSES.values():
            self.assertEqual(len(spec["sha256"]), 64)
            self.assertIn(spec["version"], spec["url"])


if __name__ == "__main__":
    unittest.main()
