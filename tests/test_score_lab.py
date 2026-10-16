"""Score-lab boundary regressions. Fixtures are NOT model/accuracy evidence."""
import copy
import http.client
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capabilities.score_lab import common
from capabilities.score_lab.accompaniment import count_bars, inject_staff, quantize_notes, split_duration
from capabilities.score_lab.server import Handler, Lab, LocalServer, byte_range
from capabilities.score_lab.lifetime import DataLock


class CoreTests(unittest.TestCase):
    def test_config_is_project_relative_not_machine_specific(self):
        config = common.load_config(common.ROOT / "output/nonexistent-score-lab-config.json")
        self.assertEqual(config["dataDir"], common.ROOT / ".private/score-lab")
        self.assertNotIn("transcription-20261010", str(config["sheetRepo"]))
        self.assertEqual(config["port"], 0)

    def test_paths_cannot_escape_private_sample_root(self):
        for name in ("../secret", "..", "a/b", "A", "a\\b", "", "a" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                common.sample_dir({"dataDir": common.ROOT / ".private/score-lab"}, name)
        with self.assertRaises(ValueError):
            common.child_file(common.ROOT / "output", "..", "config.local.json")

    def test_ranges(self):
        for value, expected in ((None, (0, 99, False)), ("bytes=4-9", (4, 9, True)),
                                ("bytes=90-999", (90, 99, True)), ("bytes=90-", (90, 99, True)),
                                ("bytes=-6", (94, 99, True)), ("bytes=-999", (0, 99, True))):
            with self.subTest(value=value):
                self.assertEqual(byte_range(value, 100), expected)

    def test_ranges_reject_invalid(self):
        for value in ("bytes=0-1,4-5", "bytes=100-", "bytes=6-5", "bytes=-0", "bytes=-", "nibbles=0-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                byte_range(value, 100)

    def test_original_time_playback_has_only_one_note_view(self):
        data = {"duration": 1, "tracks": [{"name": "Vocal", "notes": [
            {"pitch": 60, "start": .1, "end": .5, "velocity": 90}]}],
                "raw_time_events": [{"pitch": 60, "start": .1, "end": .5}]}
        result = common.validate_playback(data)
        self.assertEqual(sum(len(t["notes"]) for t in result["tracks"]), 1)
        self.assertEqual(result["tracks"][0]["role"], "vocal")
        self.assertEqual(result["tracks"][0]["notes"][0]["pitch"], 60)

    def test_invalid_playback_is_rejected(self):
        valid = {"duration": 1, "tracks": [{"name": "Vocal", "notes": [
            {"pitch": 60, "start": .1, "end": .5, "velocity": 90}]}]}
        for key, value in (("pitch", 60.5), ("pitch", 128), ("start", float("nan")),
                           ("end", 2), ("end", .1), ("velocity", -1)):
            bad = copy.deepcopy(valid); bad["tracks"][0]["notes"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                common.validate_playback(bad)
        for seconds in (0, common.MAX_CLIP + 1, float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                common.validate_playback({**valid, "duration": seconds})

    def test_long_playback_retains_original_seconds(self):
        data = {"duration": 300.288, "tracks": [{"name": "Vocal", "notes": [
            {"pitch": 60, "start": 299.1, "end": 300.2}]}]}
        self.assertEqual(common.validate_playback(data)["tracks"][0]["notes"][0]["start"], 299.1)

    def test_role_classification(self):
        self.assertEqual(common.role_of("Vocal Melody"), "vocal")
        self.assertEqual(common.role_of("Ins"), "instrumental")
        self.assertEqual(common.role_of("Chords"), "chords")

    def test_chord_quantization_uses_model_grid_without_collapsing_reattacks(self):
        notes = [{"pitch": 48, "start": .04, "end": .49}, {"pitch": 48, "start": .51, "end": .99}]
        quantized, report = quantize_notes(notes, [0, .25, .5, .75, 1])
        self.assertEqual([(n["id"], n["start"], n["end"]) for n in quantized], [(0, 0, 2), (1, 2, 4)])
        self.assertEqual(report["expandedShortNotes"], 0)
        self.assertEqual(report["sourceNotes"], 2)
        self.assertEqual(report["maxQuantizationShiftSeconds"], .04)
        for grid in ([0], [0, 0], [0, -.5], [0, float('nan')]):
            with self.subTest(grid=grid), self.assertRaises(ValueError):
                quantize_notes(notes, grid)

    def test_short_chord_is_explicitly_reported_not_dropped(self):
        notes, report = quantize_notes([{"pitch": 48, "start": .98, "end": .99}], [0, .5, 1])
        self.assertEqual((notes[0]["start"], notes[0]["end"]), (1, 2))
        self.assertEqual(report["expandedShortNotes"], 1)

    def test_chord_duration_and_section_boundaries(self):
        for value in range(1, 129):
            self.assertEqual(sum(split_duration(value)), value)
        with self.assertRaises(ValueError):
            split_duration(0)
        self.assertEqual(count_bars('"literal|"C4|Z2|'), 3)
        source = 'X:1\nM:4/4\nL:1/4\nV: Vocal\nV: Ins\nK:C\nV: Vocal\nC4|\nV: Ins\nZ|\n% verse\nV: Vocal\nM:3/4\nD3|\nV: Ins\nM:3/4\nZ|\n'
        result = inject_staff(source, ['[C,E,G,]4', '[D,F,A,]3'])
        self.assertIn('V: Accomp\n[C,E,G,]4|\n% verse', result)
        self.assertIn('V: Accomp\nM:3/4\n[D,F,A,]3|', result)
        self.assertEqual(source.count('V: Vocal'), result.count('V: Vocal'))
        with self.assertRaises(ValueError):
            inject_staff(source, ['[C,E,G,]4'])


class HttpTests(unittest.TestCase):
    def setUp(self):
        fixtures = common.ROOT / "output/score-lab-tests"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="http-", dir=fixtures)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(fixtures.resolve()))
        config = common.load_config(common.ROOT / "output/nonexistent-score-lab-config.json")
        config.update(dataDir=self.root, python=self.root / "python.exe", sheetRepo=self.root / "sheet",
                      baseRepo=self.root / "base", assetsDir=self.root / "assets")
        # Do not spawn a model. Only the queue boundary is exercised here.
        self.lab = Lab.__new__(Lab)
        self.lab.config, self.lab.token, self.lab.instance = config, "test-token", "test-instance"
        self.lab.jobs = queue.Queue(maxsize=3)
        for path in (config["python"], config["sheetRepo"] / "model.safetensors",
                     config["baseRepo"] / "model.safetensors", config["assetsDir"] / "manifest.json"):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"unit-test-fixture")
        self.directory = common.sample_dir(config, "fixture")
        self.directory.mkdir(parents=True)
        common.atomic_json(self.directory / "sample.json", {"title": "fixture", "artist": "", "source": "local",
                           "offset": 0, "duration": 1, "createdAt": "2026-01-01"})
        common.atomic_json(self.directory / "status.json", {"state": "completed", "phase": "ready"})
        common.atomic_json(self.directory / "result.json", {"duration": 1, "tracks": []})
        (self.directory / "original.wav").write_bytes(bytes(range(100)))
        (self.directory / "result").mkdir()
        (self.directory / "result/score.abc").write_text("X:1\nK:C\nC|\n")
        self.server = LocalServer(("127.0.0.1", 0), Handler)
        self.server.lab = self.lab
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        # Verified workspace-contained, explicitly named temporary test directory.
        self.assertTrue(self.root.is_relative_to((common.ROOT / "output/score-lab-tests").resolve()))
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        status, values, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, values, data

    def auth(self):
        return {"Origin": f"http://127.0.0.1:{self.server.server_port}", "X-Score-Lab-Token": self.lab.token}

    def test_catalog_and_health(self):
        status, headers, body = self.request("/api/catalog")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["samples"][0]["id"], "fixture")
        self.assertNotIn(str(self.root), body.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(self.request("/api/health")[2])["instance"], "test-instance")

    def test_no_external_host_or_frame(self):
        self.assertEqual(self.request("/", headers={"Host": "evil.example"})[0], 403)
        self.assertIn("frame-ancestors 'none'", self.request("/")[1]["Content-Security-Policy"])

    def test_explicit_javascript_mime(self):
        for path in ("/app.mjs", "/timeline.mjs"):
            self.assertEqual(self.request(path)[1]["Content-Type"], "text/javascript")
        self.assertEqual(self.request("/style.css")[1]["Content-Type"], "text/css; charset=utf-8")

    def test_audio_byte_range_and_head(self):
        status, headers, body = self.request("/media/fixture/original.wav", headers={"Range": "bytes=10-19"})
        self.assertEqual((status, body), (206, bytes(range(10, 20))))
        self.assertEqual(headers["Content-Range"], "bytes 10-19/100")
        self.assertEqual(self.request("/media/fixture/original.wav", method="HEAD")[2], b"")
        self.assertEqual(self.request("/media/fixture/original.wav", headers={"Range": "bytes=999-"})[0], 416)

    def test_private_paths_and_logs_are_not_served(self):
        for path in ("/config.local.json", "/api/samples/..", "/media/fixture/worker.log",
                     "/export/fixture/../../config.local.json", "/media/fixture/%2e%2e%2fstatus.json"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_exports_and_unfinished_media(self):
        status, headers, body = self.request("/export/fixture/score.abc")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        common.atomic_json(self.directory / "status.json", {"state": "running"})
        self.assertEqual(self.request("/media/fixture/original.wav")[0], 409)

    def test_post_requires_origin_and_token(self):
        for headers in ({}, {"Origin": "https://evil.example", "X-Score-Lab-Token": self.lab.token},
                        {"Origin": f"http://127.0.0.1:{self.server.server_port}", "X-Score-Lab-Token": "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/import?name=a.wav", "POST", b"a", headers)[0], 403)

    def test_upload_is_private_and_queued_without_running_model(self):
        status, headers, body = self.request("/api/import?name=my%20song.mp3&offset=12", "POST", b"fixture", self.auth())
        sample_id = json.loads(body)["id"]
        self.assertEqual(status, 202)
        self.assertEqual(self.lab.jobs.get_nowait(), sample_id)
        target = common.sample_dir(self.lab.config, sample_id)
        self.assertEqual(common.read_json(target / "request.json")["offset"], 12)
        self.assertEqual(common.read_json(target / "request.json")["seconds"], 30)
        self.assertEqual((target / "input.mp3").read_bytes(), b"fixture")
        self.assertEqual(self.request(f"/media/{sample_id}/original.wav")[0], 409)

    def test_long_and_whole_imports_are_queued_with_the_selected_length(self):
        for choice, expected in (("90", 90), ("full", None), ("900", 900)):
            with self.subTest(choice=choice):
                status, _, body = self.request(f"/api/import?name=a.wav&seconds={choice}", "POST", b"fixture", self.auth())
                self.assertEqual(status, 202)
                sample_id = json.loads(body)["id"]
                self.assertEqual(self.lab.jobs.get_nowait(), sample_id)
                directory = common.sample_dir(self.lab.config, sample_id)
                self.assertEqual(common.read_json(directory / "request.json")["seconds"], expected)
                meta = common.public_sample(directory)
                self.assertEqual(meta["duration"], expected)
                self.assertEqual(meta["full"], expected is None)

    def test_invalid_uploads(self):
        for path in ("/api/import?name=a.exe", "/api/import?name=a.wav&offset=nan",
                     "/api/import?name=a.wav&offset=-1", "/api/import?name=a.wav&seconds=901",
                     "/api/import?name=a.wav&seconds=nan", "/api/import?name=a.wav&seconds=0",
                     "/api/import?name=a.wav&seconds=bogus"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, "POST", b"x", self.auth())[0], 400)
        self.assertEqual(self.request("/api/import?name=a.wav", "POST", b"", self.auth())[0], 413)

    def test_queue_backpressure(self):
        for i in range(3):
            self.lab.jobs.put_nowait(str(i))
        self.assertEqual(self.request("/api/import?name=a.wav", "POST", b"x", self.auth())[0], 429)

    def test_bind_is_exclusive(self):
        with self.assertRaises(OSError):
            LocalServer(("127.0.0.1", self.server.server_port), Handler)

    def test_only_one_server_owns_private_data(self):
        with DataLock(self.root / "server.lock"):
            with self.assertRaises(OSError):
                with DataLock(self.root / "server.lock"):
                    self.fail("Second owner acquired the private data")
        with DataLock(self.root / "server.lock"):
            pass

    def test_windows_utf8_bom_configuration_is_supported(self):
        path = self.root / "bom.json"
        path.write_text('{"title":"小黄花"}', encoding="utf-8-sig")
        self.assertEqual(common.read_json(path)["title"], "小黄花")

    def test_recovery_is_limited_to_interrupted_api_jobs(self):
        common.atomic_json(self.directory / "status.json", {"state": "running"})
        self.lab.recover()
        self.assertEqual(common.read_json(self.directory / "status.json")["state"], "running")
        common.atomic_json(self.directory / "request.json", {"input": "input.mp3", "offset": 0, "seconds": 30})
        self.lab.recover()
        self.assertEqual(common.read_json(self.directory / "status.json")["phase"], "interrupted")

    def test_disconnected_import_response_does_not_cancel_accepted_job(self):
        original = Handler.json

        def disconnect_accepted(handler, value, status=200):
            if status == 202:
                raise ConnectionAbortedError("synthetic client navigation")
            return original(handler, value, status)

        with patch.object(Handler, "json", disconnect_accepted):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request("/api/import?name=a.wav", "POST", b"fixture", self.auth())
        sample_id = self.lab.jobs.get_nowait()
        target = common.sample_dir(self.lab.config, sample_id)
        self.assertEqual(common.read_json(target / "status.json")["state"], "queued")

    def test_cancelled_range_is_not_an_internal_error_response(self):
        with patch.object(Handler, "file", side_effect=ConnectionAbortedError("synthetic range cancel")), \
                patch.object(Handler, "json") as json_response:
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request("/media/fixture/original.wav")
            json_response.assert_not_called()


if __name__ == "__main__":
    unittest.main()
