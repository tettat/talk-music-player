"""Model-environment engraving regressions; synthetic, NOT accuracy evidence."""
from fractions import Fraction
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
from music21 import clef, converter, key, stream

from .accompaniment import (conversion_voice, export_voice_musicxml, inject_staff,
                            parse_voice_score, render_measures, single_voice_abc, tied_events)
from .common import ROOT


HEADER = 'X:1\nT:\nM:4/4\nL:1/4\nV: Vocal\nV: Ins\nK:C\n'


class AccompanimentTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = (ROOT / 'output/score-lab-tests').resolve()
        self.fixtures.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='engraving-', dir=self.fixtures)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(self.fixtures))

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(self.fixtures))
        self.temp.cleanup()

    def test_named_voices_and_multibar_rests_are_not_concatenated(self):
        source = HEADER + 'V: Vocal\nC4|D4|\nV: Ins\nZ2|\nV: Vocal\nE4|\nV: Ins\nG4|\n'
        result = parse_voice_score(source)
        self.assertEqual([p.id for p in result.parts], ['Vocal', 'Ins'])
        self.assertEqual([p.highestTime for p in result.parts], [12, 12])
        self.assertEqual([len(list(p.getElementsByClass(stream.Measure))) for p in result.parts], [3, 3])
        self.assertEqual(tied_events(result.parts[1]), [(67, Fraction(8), Fraction(12))])
        self.assertNotIn('Z', single_voice_abc(source, 'Ins'))

    def test_short_meter_changes_survive_musicxml_without_extra_padding(self):
        source = HEADER + ('V: Vocal\nC4|\nV: Ins\nZ|\n'
                           'V: Vocal\nM:3/4\nD3|\nV: Ins\nM:3/4\nZ|\n'
                           'V: Vocal\nM:1/4\nE|\nV: Ins\nM:1/4\nZ|\n')
        abc = inject_staff(source, ['[C,E,G,]4', '[D,F,A,]3', '[E,G,B,]'])
        destination = self.root / 'arrangement.musicxml'
        expected = export_voice_musicxml(abc, destination)
        reread = converter.parse(str(destination))
        self.assertEqual(len(reread.parts), 3)
        self.assertEqual([p.highestTime for p in reread.parts], [8, 8, 8])
        self.assertEqual([tied_events(p) for p in reread.parts], [tied_events(p) for p in expected.parts])
        self.assertEqual([m.timeSignature.ratioString for m in reread.parts[2].getElementsByClass(stream.Measure)],
                         ['4/4', '3/4', '1/4'])
        self.assertIsInstance(reread.parts[2].recurse().getElementsByClass(clef.Clef).first(), clef.BassClef)

    def test_body_and_inline_keys_keep_pitches_and_signature_offsets(self):
        source = HEADER + ('V: Vocal\nF4|\nK:G\nF2[K:C]F2|\n'
                           'V: Ins\nZ|\nK:G\nz2[K:C]z2|\n')
        destination = self.root / 'keys.musicxml'
        parsed = export_voice_musicxml(source, destination)
        self.assertEqual([event[0] for event in tied_events(parsed.parts[0])], [65, 65, 66])
        self.assertEqual(sorted((int(n.pitch.midi), Fraction(n.offset)) for n in parsed.parts[0].flatten().notes),
                         [(65, Fraction(0)), (65, Fraction(6)), (66, Fraction(4))])
        reread = converter.parse(str(destination))
        self.assertEqual([(Fraction(k.offset), k.sharps) for k in reread.parts[0].flatten().getElementsByClass(key.KeySignature)],
                         [(Fraction(0), 0), (Fraction(4), 1), (Fraction(6), 0)])

    def test_accidentals_cross_octaves_but_reset_at_bar_and_key_changes(self):
        source = HEADER + 'V: Vocal\n^Ff=Ff|F2[K:G]Ff|\nV: Ins\nZ2|\n'
        parsed = parse_voice_score(source)
        self.assertEqual([int(n.pitch.midi) for n in parsed.parts[0].flatten().notes], [66, 78, 65, 77, 65, 66, 78])

    def test_partial_chord_ties_preserve_reattacks_across_bars(self):
        measures = [SimpleNamespace(start_t=a, end_t=a + 4, abc_numerator=4, abc_denominator=4,
                                    numerator=4, denominator=4, pad_before=False) for a in (0, 4)]
        score = SimpleNamespace(subbeat_times=np.arange(9, dtype=float), key_arr=np.array(['C'] * 9), measures=measures)
        notation = SimpleNamespace(_duration_units=lambda score, a, b, unit: b - a,
                                   get_key_accidentals=lambda key: [0] * 7,
                                   note_to_abc=lambda pitch, keys, accidentals: {48: 'C,', 52: 'E,'}[pitch])
        notes = [{'pitch': 48, 'start': 0, 'end': 8}, {'pitch': 52, 'start': 0, 'end': 4},
                 {'pitch': 52, 'start': 4, 'end': 8}]
        bars, expected, report = render_measures(score, notes, notation, 4)
        self.assertEqual(report['sourceNotes'], 3)
        self.assertIn('C,-', bars[0])
        self.assertNotIn('E,-', bars[0])
        source = HEADER + 'V: Vocal\nC4|D4|\nV: Ins\nZ2|\n'
        abc = inject_staff(source, bars)
        parsed = export_voice_musicxml(abc, self.root / 'ties.musicxml')
        self.assertEqual(tied_events(parsed.parts[2]), expected)
        self.assertEqual(len(expected), 3)
        with self.assertRaisesRegex(ValueError, 'Overlapping identical'):
            render_measures(score, notes + [notes[0]], notation, 4)

    def test_unsupported_conversion_and_broken_ties_fail_closed(self):
        for music in ('C3/2D5/2|', 'C3|', 'z4-|'):
            with self.subTest(music=music), self.assertRaises(ValueError):
                conversion_voice(single_voice_abc(HEADER + 'V: Vocal\n' + music, 'Vocal'))
        with self.assertRaises(ValueError):
            parse_voice_score(HEADER + 'V: Vocal\nC4|\nV: Ins\nZ2|\n')
        with self.assertRaisesRegex(ValueError, 'Unclosed'):
            tied_events(converter.parse('X:1\nM:4/4\nL:1/4\nK:C\nC4-|', format='abc').parts[0])


if __name__ == '__main__':
    unittest.main()
