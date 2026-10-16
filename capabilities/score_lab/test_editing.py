"""Model-venv regressions and opt-in real-sample, copy-only editing evidence.

unittest: python -m unittest capabilities.score_lab.test_editing -v
evidence: python -m capabilities.score_lab.test_editing --real-samples
"""
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

import mido
from music21 import converter, stream

from . import editing as edit
from .accompaniment import parse_voice_score, tied_events
from .common import ROOT, load_config


HEADER = 'X:1\nT:\nM:4/4\nL:1/4\nQ:1/4=120\nV: Vocal\nV: Ins\nV: Accomp\nK:C\n'
ABC = HEADER + 'V: Vocal\nC2-C2|D4|\nV: Ins\nZ2|\nV: Accomp\n[C,E,G,]4|[D,F,A,]4|\n'
SAMPLES = ('sunny', 'main-character', 'main-character-full', 'solitude-full', '5a6535ce067e73d8ae913c3f')


def inventory(directory):
    """Byte hashes of every original file, excluding independent edit history."""
    return {str(p.relative_to(directory)).replace('\\', '/'): edit._hash_file(p)
            for p in sorted(directory.rglob('*')) if p.is_file() and p.relative_to(directory).parts[0] != 'edits'}


def midi_events(path):
    midi = mido.MidiFile(path)
    assert midi.type == 1 and midi.ticks_per_beat == 10000
    assert [m.tempo for m in midi.tracks[0] if m.type == 'set_tempo'] == [500000]
    result, voices = [], []
    for track in midi.tracks[1:]:
        voices.append(track.name)
        tick, active = 0, {}
        for message in track:
            tick += message.time
            if message.type == 'note_on' and message.velocity:
                assert message.note not in active
                active[message.note] = tick, message.velocity
            elif message.type == 'note_off' or message.type == 'note_on' and not message.velocity:
                start, velocity = active.pop(message.note)
                result.append((track.name, start, tick, message.note, velocity))
        assert not active
    return voices, sorted(result)


def verify_midi_notes(root, result):
    voices, events = midi_events(root / 'edited.mid')
    assert voices == result['voices']
    expected = sorted((t['voice'], round(n['start'] * 20000), round(n['end'] * 20000), n['pitch'], n['velocity'])
                      for t in result['tracks'] for n in t['notes'])
    assert events == expected
    return voices, events


def pcm_digest(path):
    digest = hashlib.sha256()
    with wave.open(str(path), 'rb') as audio:
        params = {'frames': audio.getnframes(), 'sampleRate': audio.getframerate(),
                  'channels': audio.getnchannels(), 'sampleWidth': audio.getsampwidth()}
        while data := audio.readframes(65536):
            digest.update(data)
    return digest.hexdigest(), params


def fake_piano(playback, assets, output, rate=1000):
    # Deliberately synthetic/fast. Real evidence NEVER uses this renderer.
    pitch = sum(n['pitch'] for t in playback['tracks'] for n in t['notes'])
    with wave.open(str(output / 'piano_mix.wav'), 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(rate)
        audio.writeframes((pitch % 32768).to_bytes(2, 'little') * round(playback['duration'] * rate))
    return {'mix': {'frames': round(playback['duration'] * rate), 'sampleRate': rate, 'channels': 1}}


class ModelEditingTests(unittest.TestCase):
    def setUp(self):
        fixtures = ROOT / 'output/score-editing-tests'
        fixtures.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='model-', dir=fixtures)
        self.root = Path(self.temp.name).resolve()
        self.config = {'dataDir': self.root, 'assetsDir': ROOT / '.private/score-lab/render-assets'}
        self.directory = self.root / 'samples' / 'fixture'
        self.make_source(ABC)
        self.patches = [patch('capabilities.score_lab.worker.render_piano', side_effect=fake_piano),
                        patch('capabilities.score_lab.worker.verify_assets', return_value={})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.assertTrue(self.root.is_relative_to((ROOT / 'output/score-editing-tests').resolve()))
        self.temp.cleanup()

    def make_source(self, abc):
        result = self.directory / 'result'
        (result / 'notation').mkdir(parents=True, exist_ok=True)
        (result / 'arrangement.abc').write_text(abc, encoding='utf-8')
        (result / 'score.abc').write_text(abc, encoding='utf-8')
        score = parse_voice_score(abc)
        count = len(list(score.parts[0].getElementsByClass(stream.Measure)))
        beats = '\n'.join(f'{.1 + .5 * i:.6f} {i % 4 + 1} 4 4' for i in range(count * 4 + 1)) + '\n'
        (result / 'notation/song_beats.txt').write_text(beats, encoding='utf-8')
        original = {'duration': .1 + count * 2, 'key': 'C', 'meter': '4/4', 'notationBpm': 120,
                    'tracks': [{'name': 'Vocal', 'notes': [{'pitch': 60, 'start': .1, 'end': 2.1, 'velocity': 100}]},
                               {'name': 'Chords', 'notes': [{'pitch': 48, 'start': .1, 'end': 2.1, 'velocity': 48}]}],
                    'measures': [{'index': i, 'start': .1 + i * 2, 'end': .1 + (i + 1) * 2,
                                  'leading_rest': 0, 'trailing_rest': 0} for i in range(count)]}
        (self.directory / 'result.json').write_text(json.dumps(original), encoding='utf-8')
        for path in (result / 'transcription.mid', self.directory / 'original.wav', self.directory / 'piano_mix.wav'):
            path.write_bytes(b'original sentinel bytes: never overwrite')

    def draft(self, abc=None):
        current = edit.read_current(self.config, 'fixture')
        return {'abc': abc or current['abc'], 'baseRevision': current['revision'],
                'sourceFingerprint': current['sourceFingerprint']}

    def test_commit_preserves_all_voices_ties_source_bytes_and_midi_seconds(self):
        before = inventory(self.directory)
        changed = ABC.replace('C2-C2', '^C2-^C2').replace('[C,E,G,]', '[C,^E,G,]')
        response = edit.commit_revision(self.config, 'fixture', self.draft(changed), 'first')
        current = edit.read_current(self.config, 'fixture')
        self.assertEqual((response['revision'], current['revisionId'], current['abc']), (1, 'first', changed))
        result = edit.read_revision_result(self.config, 'fixture', 'first')
        self.assertEqual(result['voices'], ['Vocal', 'Ins', 'Accomp'])
        self.assertEqual(result['tracks'][1]['notes'], [])
        self.assertEqual(result['tracks'][0]['notes'][0]['pitch'], 61)
        self.assertEqual(result['tracks'][0]['notes'][0]['start'], .1)
        self.assertAlmostEqual(result['tracks'][0]['notes'][0]['end'], 2.1)
        self.assertEqual(result['tracks'][2]['notes'][1]['pitch'], 53)
        root = self.directory / 'edits/rev-first'
        verify_midi_notes(root, result)
        exported = converter.parse(str(root / 'edited.musicxml'), format='musicxml')
        self.assertEqual([tied_events(p) for p in exported.parts], [tied_events(p) for p in parse_voice_score(changed).parts])
        self.assertEqual(inventory(self.directory), before)
        for kind, names in [('export', result['exports']), ('media', result['media'])]:
            for name in names:
                self.assertEqual(edit.revision_file(self.config, 'fixture', 'first', name, kind=kind), root / name)

    def test_partial_chord_ties_and_reattacks_survive_member_pitch_edit(self):
        source = HEADER + 'V: Vocal\nC4|D4|\nV: Ins\nZ2|\nV: Accomp\n[C,-E,]4|[C,E,]4|\n'
        self.make_source(source)
        changed = source.replace('C,-', '^C,-').replace('[C,E,]', '[^C,E,]')
        edit.commit_revision(self.config, 'fixture', self.draft(changed), 'partial')
        parsed = converter.parse(str(self.directory / 'edits/rev-partial/edited.musicxml'))
        self.assertEqual(tied_events(parsed.parts[2]), [(49, Fraction(0), Fraction(8)),
                                                       (52, Fraction(0), Fraction(4)), (52, Fraction(4), Fraction(8))])

    def test_score_fallback_keeps_unengraved_source_chords_frozen_in_seconds(self):
        source = ABC.replace('V: Accomp\n', '', 1).replace('V: Accomp\n[C,E,G,]4|[D,F,A,]4|\n', '')
        self.make_source(source)
        (self.directory / 'result/arrangement.abc').unlink()
        current = edit.read_current(self.config, 'fixture')
        self.assertEqual(current['sourceAbc'], 'score.abc')
        edit.commit_revision(self.config, 'fixture', self.draft(source.replace('C2-C2', '^C2-^C2')), 'fallback')
        result = edit.read_revision_result(self.config, 'fixture', 'fallback')
        self.assertEqual(result['notatedVoices'], ['Vocal', 'Ins'])
        self.assertEqual(result['frozenPlaybackVoices'], ['Accomp'])
        self.assertEqual(result['voices'], ['Vocal', 'Ins', 'Accomp'])
        self.assertFalse(result['tracks'][2]['editable'])
        self.assertEqual([(n['pitch'], n['start'], n['end'], n['velocity']) for n in result['tracks'][2]['notes']],
                         [(48, .1, 2.1, 48)])
        verify_midi_notes(self.directory / 'edits/rev-fallback', result)
        xml = converter.parse(str(self.directory / 'edits/rev-fallback/edited.musicxml'))
        self.assertEqual(len(xml.parts), 2)  # never claim that fallback engraves three parts

    def test_invalid_pitch_broken_tie_and_rhythm_never_publish(self):
        original = inventory(self.directory)
        for index, abc in enumerate((ABC.replace('C2-C2', '^C2-=C2'), ABC.replace('C2-C2', 'B\'\'\'\'2-B\'\'\'\'2'),
                                     ABC.replace('C2-C2', 'C3-C1'), ABC.replace('M:4/4', 'M:2/2'),
                                     ABC + '%%abc-include https://bad.invalid/score\n',
                                     ABC.replace('[C,E,G,]', '[C,C,G,]'))):
            with self.subTest(index=index), self.assertRaises(ValueError):
                edit.commit_revision(self.config, 'fixture', self.draft(abc), 'invalid-' + str(index))
        self.assertFalse((self.directory / 'edits/current.json').exists())
        self.assertEqual(inventory(self.directory), original)
        self.assertFalse(list((self.directory / 'edits').glob('.pending-*')))

    def test_lost_update_duplicate_nonce_source_change_and_pinned_old_exports(self):
        stale = self.draft()
        edit.commit_revision(self.config, 'fixture', stale, 'one')
        with self.assertRaises(edit.EditConflict):
            edit.commit_revision(self.config, 'fixture', stale, 'two')
        valid = self.draft(ABC.replace('C2-C2', '^C2-^C2'))
        with self.assertRaises(edit.EditConflict):
            edit.commit_revision(self.config, 'fixture', valid, 'one')
        edit.commit_revision(self.config, 'fixture', valid, 'two')
        self.assertEqual(edit.read_current(self.config, 'fixture')['revision'], 2)
        self.assertEqual(edit.read_revision_result(self.config, 'fixture', 'one')['revision'], 1)
        self.assertEqual(edit.revision_file(self.config, 'fixture', 'one', 'edited.mid', kind='export').parent.name, 'rev-one')
        path = self.directory / 'result/arrangement.abc'
        path.write_text(ABC.replace('C2-C2', 'D2-D2'), encoding='utf-8')
        with self.assertRaises(edit.EditConflict):
            edit.read_current(self.config, 'fixture')
        with self.assertRaises(edit.EditConflict):
            edit.commit_revision(self.config, 'fixture', {**valid, 'baseRevision': 2}, 'three')
        # Pinned historical exports remain meaningful even after source reprocessing.
        self.assertEqual(edit.read_revision_result(self.config, 'fixture', 'two')['revision'], 2)

    def test_source_changed_during_render_and_render_failure_leave_pointer_untouched(self):
        draft = self.draft()
        source_path = self.directory / 'result/arrangement.abc'

        def mutate(*args):
            source_path.write_text(ABC.replace('C2-C2', 'D2-D2'), encoding='utf-8')
            return {}

        with patch.object(edit, '_build', side_effect=mutate), self.assertRaises(edit.EditConflict):
            edit.commit_revision(self.config, 'fixture', draft, 'changed')
        source_path.write_text(ABC, encoding='utf-8')
        with patch('capabilities.score_lab.worker.render_piano', side_effect=RuntimeError('synthetic renderer failure')):
            with self.assertRaises(RuntimeError):
                edit.commit_revision(self.config, 'fixture', draft, 'failed')
        self.assertFalse((self.directory / 'edits/current.json').exists())
        self.assertFalse(list((self.directory / 'edits').glob('.pending-*')))

    def test_atomic_pointer_failure_leaves_complete_but_unpublished_orphan(self):
        edit.commit_revision(self.config, 'fixture', self.draft(), 'one')
        before = (self.directory / 'edits/current.json').read_bytes()
        with patch.object(edit, '_atomic_json', side_effect=OSError('synthetic publish failure')):
            with self.assertRaises(OSError):
                edit.commit_revision(self.config, 'fixture', self.draft(), 'orphan')
        self.assertEqual((self.directory / 'edits/current.json').read_bytes(), before)
        self.assertTrue((self.directory / 'edits/rev-orphan/edited.mid').is_file())
        with self.assertRaises(edit.EditError):
            edit.read_revision_result(self.config, 'fixture', 'orphan')
        with self.assertRaises(edit.EditConflict):
            edit.commit_revision(self.config, 'fixture', self.draft(), 'orphan')

    def test_concurrent_same_revision_has_one_writer_and_no_lost_update(self):
        entered, release = threading.Event(), threading.Event()
        original_build = edit._build
        errors = []
        draft = self.draft()

        def waiting(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test writer timed out')
            return original_build(*args)

        def first():
            try:
                edit.commit_revision(self.config, 'fixture', draft, 'same')
            except Exception as exc:
                errors.append(exc)

        with patch.object(edit, '_build', side_effect=waiting):
            thread = threading.Thread(target=first)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(edit.EditConflict):
                    edit.commit_revision(self.config, 'fixture', draft, 'same')
            finally:
                release.set(); thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(edit.read_current(self.config, 'fixture')['revision'], 1)

    def test_revision_tampering_is_detected(self):
        edit.commit_revision(self.config, 'fixture', self.draft(), 'one')
        result_path = self.directory / 'edits/rev-one/result.json'
        result_path.write_bytes(result_path.read_bytes() + b' ')
        with self.assertRaises(edit.EditError):
            edit.read_revision_result(self.config, 'fixture', 'one')

    def test_same_size_media_or_export_tampering_is_detected(self):
        edit.commit_revision(self.config, 'fixture', self.draft(), 'one')
        path = self.directory / 'edits/rev-one/edited.mid'
        content = path.read_bytes()
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        with self.assertRaises(edit.EditError):
            edit.revision_file(self.config, 'fixture', 'one', 'edited.mid', kind='export')


def run_real_samples():
    """Real music21 + FluidR3 + CLI jobs, ALL against independent full copies."""
    config = load_config()
    output = ROOT / 'output/outsource'
    output.mkdir(parents=True, exist_ok=True)
    proof = Path(tempfile.mkdtemp(prefix='usic-editing-proof-', dir=output)).resolve()
    data = proof / 'data'
    samples_root = data / 'samples'
    samples_root.mkdir(parents=True)
    local_config = {key: str(config[key]) for key in ('python', 'sheetRepo', 'baseRepo', 'assetsDir')}
    local_config.update(dataDir=str(data), device='cpu', threads=1, port=0, jobTimeoutSeconds=600)
    config_path = proof / 'config.json'
    config_path.write_text(json.dumps(local_config, indent=2), encoding='utf-8')
    copy_config = load_config(config_path)
    originals = {sid: inventory(config['dataDir'] / 'samples' / sid) for sid in SAMPLES}
    report = {'status': 'running', 'proofDirectory': str(proof.relative_to(ROOT)).replace('\\', '/'),
              'modelPython': str(config['python'].relative_to(ROOT)).replace('\\', '/'),
              'samples': [], 'negativeCases': [], 'originalSamplesNeverWritten': True}
    for sid in SAMPLES:
        source, destination = config['dataDir'] / 'samples' / sid, samples_root / sid
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns('edits'))
        assert inventory(destination) == originals[sid]
        drafts = destination / 'edits/drafts'
        drafts.mkdir(parents=True)
        bootstrap = edit.read_current(copy_config, sid)
        initial_draft = {'abc': bootstrap['abc'], 'baseRevision': 0, 'sourceFingerprint': bootstrap['sourceFingerprint']}

        def cli(draft, token):
            path = drafts / (token + '.json')
            path.write_text(json.dumps(draft, ensure_ascii=False), encoding='utf-8')
            process = subprocess.run([str(config['python']), str(ROOT / 'capabilities/score_lab/editing.py'),
                                      '--config', str(config_path), '--sample', sid, '--draft', str(path), '--revision', token],
                                     cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=650)
            assert process.returncode == 0, f'{sid}: CLI {process.returncode}: {process.stdout} {process.stderr}'
            response = json.loads(process.stdout.strip())
            assert response['ok']
            return response

        print(f'[real editing] {sid}: control revision', flush=True)
        cli(initial_draft, 'control')
        control = edit.read_revision_result(copy_config, sid, 'control')
        control_root = destination / 'edits/rev-control'
        voices, before_midi = verify_midi_notes(control_root, control)
        current = edit.read_current(copy_config, sid)
        preferred_voice = {'sunny': 'Vocal', 'main-character': 'Accomp', 'main-character-full': 'Ins',
                           'solitude-full': 'Vocal', SAMPLES[-1]: 'Accomp'}[sid]
        # Invoke precisely the browser's DOM-free helper, not a different pitch rewriter.
        node_code = '''import fs from 'node:fs'; import * as e from './capabilities/score_lab/web/editor.mjs';
const current=JSON.parse(fs.readFileSync(0,'utf8')); const state=e.createEditor(current);
const selected=e.listPitchTokens(state.abc).find(n=>n.voice===process.argv[1]);
const {state:next,edit}=e.applyPitchEdit(state,selected,1);
console.log(JSON.stringify({draft:e.makeDraft(next),edit,selected}));'''
        node = subprocess.run(['node', '--input-type=module', '-e', node_code, preferred_voice],
                              cwd=ROOT, input=json.dumps(current, ensure_ascii=False), capture_output=True,
                              text=True, encoding='utf-8', timeout=15)
        assert node.returncode == 0, node.stderr
        change = json.loads(node.stdout)
        print(f'[real editing] {sid}: {preferred_voice} {change["edit"]["fromPitch"]} -> {change["edit"]["toPitch"]}', flush=True)
        cli(change['draft'], 'semitone')
        result = edit.read_revision_result(copy_config, sid, 'semitone')
        revision_root = destination / 'edits/rev-semitone'
        after_voices, after_midi = verify_midi_notes(revision_root, result)
        assert voices == after_voices == ['Vocal', 'Ins', 'Accomp']
        removed, added = Counter(before_midi) - Counter(after_midi), Counter(after_midi) - Counter(before_midi)
        assert sum(removed.values()) == sum(added.values()) == 1
        old, new = next(iter(removed)), next(iter(added))
        assert old[:3] == new[:3] and old[4] == new[4] and new[3] == old[3] + 1
        xml = converter.parse(str(revision_root / 'edited.musicxml'), format='musicxml')
        score = parse_voice_score(result['abc'])
        assert len(xml.parts) == 3
        assert [tied_events(p) for p in xml.parts] == [tied_events(p) for p in score.parts]
        before_pcm, before_info = pcm_digest(control_root / 'piano_mix.wav')
        after_pcm, after_info = pcm_digest(revision_root / 'piano_mix.wav')
        assert before_info == after_info and before_pcm != after_pcm
        assert inventory(destination) == originals[sid]
        assert inventory(source) == originals[sid]
        for name in result['exports']:
            assert edit.revision_file(copy_config, sid, 'semitone', name, kind='export') == revision_root / name
        for name in result['media']:
            assert edit.revision_file(copy_config, sid, 'semitone', name, kind='media') == revision_root / name
        # Compare every unaffected canonical event; NOT just aggregate note counts.
        original_events = {str(p.id): tied_events(p) for p in parse_voice_score(bootstrap['abc']).parts}
        changed_events = {str(p.id): tied_events(p) for p in score.parts}
        for voice in voices:
            assert sorted((a, b) for _, a, b in original_events[voice]) == sorted((a, b) for _, a, b in changed_events[voice])
            if voice != preferred_voice:
                assert original_events[voice] == changed_events[voice]
        record = {'id': sid, 'duration': result['duration'], 'revision': result['revision'], 'revisionId': 'semitone',
                  'voices': voices, 'voiceNoteCounts': {t['voice']: len(t['notes']) for t in result['tracks']},
                  'musicxmlRoundtripVerified': True, 'quarterOffsetsDurationsVoicesUnchanged': True,
                  'midiNotesVerifiedAgainstNotesJson': True, 'midiChangedNotes': 1,
                  'changedVoice': preferred_voice, 'fromPitch': old[3], 'toPitch': new[3],
                  'changedOriginalSeconds': {'start': old[1] / 20000, 'end': old[2] / 20000},
                  'controlPcmSha256': before_pcm, 'editedPcmSha256': after_pcm, 'pcmChanged': True,
                  'wav': after_info, 'exports': result['exports'], 'media': result['media'],
                  'controlRevisionDirectory': str(control_root.relative_to(ROOT)).replace('\\', '/'),
                  'editedRevisionDirectory': str(revision_root.relative_to(ROOT)).replace('\\', '/'),
                  'originalFilesVerified': len(originals[sid]), 'originalAndCopiedSourceBytesUnchanged': True,
                  'originalFileSha256': originals[sid], 'sourceFingerprint': bootstrap['sourceFingerprint'],
                  'syntheticTerminalBeats': result['provenance']['syntheticTerminalBeats']}
        report['samples'].append(record)
        if sid == 'sunny':
            valid = {'abc': result['abc'], 'baseRevision': 2, 'sourceFingerprint': result['sourceFingerprint']}
            cases = {
                'invalid ABC / external directive': {**valid, 'abc': valid['abc'] + '%%abc-include https://bad.invalid/\n'},
                'changed rhythm': {**valid, 'abc': valid['abc'].replace('z8', 'z9', 1)},
                'changed meter': {**valid, 'abc': valid['abc'].replace('M:4/4', 'M:2/2', 1)},
                'changed key': {**valid, 'abc': valid['abc'].replace('K:G', 'K:C', 1)},
                'dropped voice measures': {**valid, 'abc': valid['abc'].replace('V: Ins\nZ4|\n', '', 1)},
                'stale baseRevision': {**valid, 'baseRevision': 0},
                'oversize ABC': {**valid, 'abc': 'x' * (edit.MAX_ABC_BYTES + 1)},
            }
            token = edit.inspect_abc(valid['abc'])['pitches'][0]
            cases['out-of-range pitch'] = {**valid, 'abc': valid['abc'][:token['startChar']] + "B''''" + valid['abc'][token['endChar']:]}
            for index, (label, draft) in enumerate(cases.items()):
                try:
                    edit.commit_revision(copy_config, sid, draft, 'negative-' + str(index))
                except edit.EditError as exc:
                    report['negativeCases'].append({'case': label, 'rejected': True, 'code': exc.code})
                else:
                    raise AssertionError('Negative edit published: ' + label)
            try:
                edit.commit_revision(copy_config, sid, valid, 'semitone')
            except edit.EditConflict:
                report['negativeCases'].append({'case': 'same revision ID', 'rejected': True, 'code': 'conflict'})
            else:
                raise AssertionError('Duplicate transaction published')
            try:
                edit.revision_file(copy_config, sid, 'semitone', '../original.wav', kind='media')
            except edit.EditError:
                report['negativeCases'].append({'case': 'path traversal', 'rejected': True, 'code': 'invalid_edit'})
            else:
                raise AssertionError('Traversal allowed')
            canonical = destination / 'result/arrangement.abc'
            source_bytes = canonical.read_bytes()
            try:
                canonical.write_bytes(source_bytes.replace(b'K:G', b'K:C', 1))
                try:
                    edit.commit_revision(copy_config, sid, valid, 'source-changed')
                except edit.EditConflict:
                    report['negativeCases'].append({'case': 'source score changed since draft', 'rejected': True, 'code': 'conflict'})
                else:
                    raise AssertionError('Changed source accepted')
            finally:
                canonical.write_bytes(source_bytes)
            assert edit.read_current(copy_config, sid)['revision'] == 2
            assert inventory(destination) == originals[sid]
    report.update(status='passed', completedAt=datetime.now(timezone.utc).isoformat(),
                  originalByteIntegrityVerified=all(inventory(config['dataDir'] / 'samples' / sid) == originals[sid] for sid in SAMPLES),
                  limitations=['Pitch-only; rhythm, ties, voice declarations, chord labels, meter and key are locked.',
                               'Playback is beat-mapped canonical notation, not untouched unquantized MIDI.',
                               'No QScore three-part integration; server/queue/DOM integration intentionally not changed.'])
    assert report['originalByteIntegrityVerified']
    return report


if __name__ == '__main__':
    if '--real-samples' in sys.argv:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ModelEditingTests)
        unit = unittest.TextTestRunner(verbosity=2).run(suite)
        if not unit.wasSuccessful():
            raise SystemExit(1)
        report = run_real_samples()
        report['modelUnitTests'] = {'ran': unit.testsRun, 'failures': len(unit.failures), 'errors': len(unit.errors)}
        destination = ROOT / 'output/outsource/usic-editing-result.json'
        edit._atomic_json(destination, report)
        print(str(destination), flush=True)
    else:
        unittest.main()
