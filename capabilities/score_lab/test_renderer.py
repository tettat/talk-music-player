"""Run with the model Python. Synthetic sampler tests, NOT model accuracy tests."""
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from .common import ROOT
from .worker import prepare_clip, render_piano


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = (ROOT / "output/score-lab-tests").resolve()
        self.fixtures.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="render-", dir=self.fixtures)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(self.fixtures))
        self.assets = self.root / "assets/soundfonts/acoustic_grand_piano-mp3"
        self.assets.mkdir(parents=True)
        tone = np.sin(2 * np.pi * 261.6256 * np.arange(44100) / 44100).astype("float32") * .1
        # A synthetic WAV fixture named like the looked-up sample; soundfile
        # detects the container. No downloaded piano/music/model is used here.
        sf.write(self.assets / "C4.mp3", np.column_stack([tone, tone]), 44100, format="WAV")
        self.playback = {"duration": 1, "tracks": [{"role": "vocal", "notes": [
            {"pitch": 60, "start": .2, "end": .5, "velocity": 100}]}]}

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(self.fixtures))
        self.temp.cleanup()

    def test_render_has_exact_frames_and_note_time_bounds(self):
        evidence = render_piano(self.playback, self.root / "assets", self.root)
        pcm, rate = sf.read(self.root / "piano_mix.wav", always_2d=True)
        self.assertEqual((len(pcm), rate, pcm.shape[1]), (44100, 44100, 2))
        self.assertTrue(np.isfinite(pcm).all())
        self.assertEqual(np.max(np.abs(pcm[:8820])), 0)
        self.assertEqual(np.max(np.abs(pcm[22050:])), 0)
        self.assertGreater(evidence["mix"]["rms"], 0)
        self.assertLessEqual(evidence["mix"]["peak"], .961)

    def test_no_invented_instrumental_track_or_octave_change(self):
        render_piano(self.playback, self.root / "assets", self.root)
        self.assertFalse((self.root / "piano_instrumental.wav").exists())
        pcm, _ = sf.read(self.root / "piano_vocal.wav")
        frequencies = np.fft.rfftfreq(len(pcm[10000:20000]), 1 / 44100)
        spectrum = np.abs(np.fft.rfft(pcm[10000:20000, 0]))
        self.assertLess(abs(frequencies[spectrum.argmax()] - 261.6256), 5)

    def test_unsupported_pitch_fails_instead_of_silent_transpose(self):
        self.playback["tracks"][0]["notes"][0]["pitch"] = 120
        with self.assertRaises(ValueError):
            render_piano(self.playback, self.root / "assets", self.root)

    def test_prepare_clip_bounds_and_original_unchanged(self):
        original = self.root / "input.wav"
        sf.write(original, np.ones((88200, 2), dtype="float32") * .15, 44100)
        before = original.read_bytes()
        pcm, rate, waveform = prepare_clip(original, self.root / "original.wav", .5, 1)
        self.assertEqual((len(pcm), rate, len(waveform)), (44100, 44100, 240))
        self.assertEqual(original.read_bytes(), before)
        with self.assertRaises(ValueError):
            prepare_clip(original, self.root / "bad.wav", 2, 1)

    def test_previously_verified_clip_stays_byte_identical(self):
        original = self.root / "input.wav"
        sf.write(original, np.ones((44100, 2), dtype="float32") * .15, 44100, subtype="FLOAT")
        prepare_clip(original, self.root / "original.wav", 30, 1, precut=True)
        self.assertEqual(original.read_bytes(), (self.root / "original.wav").read_bytes())

    def test_long_and_whole_clips_keep_their_exact_frame_count(self):
        # Low-rate synthetic silence keeps this boundary test small and fast.
        source = self.root / "long.wav"
        sf.write(source, np.zeros((91 * 8000, 1), dtype="float32"), 8000)
        pcm, rate, waveform = prepare_clip(source, self.root / "whole.wav", 0, None)
        self.assertEqual((len(pcm), rate), (91 * 8000, 8000))
        self.assertEqual(len(waveform), 728)
        pcm, _, _ = prepare_clip(source, self.root / "ninety.wav", .5, 90)
        self.assertEqual(len(pcm), 90 * 8000)
        pcm, _, _ = prepare_clip(source, self.root / "remainder.wav", 1, None)
        self.assertEqual(len(pcm), 90 * 8000)

    def test_whole_clip_over_limit_is_rejected_not_silently_truncated(self):
        source = self.root / "too-long.wav"
        sf.write(source, np.zeros((901 * 8000, 1), dtype="float32"), 8000)
        with self.assertRaisesRegex(ValueError, "15-minute"):
            prepare_clip(source, self.root / "bad.wav", 0, None)
        for seconds in (0, 901, float("nan")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                prepare_clip(source, self.root / "bad.wav", 0, seconds)


if __name__ == "__main__":
    unittest.main()
