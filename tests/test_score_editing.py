"""Stdlib/Node editing boundary tests; no model dependencies or live server."""
from fractions import Fraction
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from capabilities.score_lab import editing as edit


ABC = ('X:1\nT:\nM:4/4\nL:1/4\nQ:1/4=120\nV: Vocal\nV: Ins\nV: Accomp\nK:C\n'
       'V: Vocal\nC2-C2|\nV: Ins\nZ|\nV: Accomp\n[C,E,G,]4|\n')


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="score-edit-boundary-")
        self.root = Path(self.temp.name).resolve()
        self.config = {"dataDir": self.root}
        self.directory = self.root / "samples" / "fixture"
        (self.directory / "result").mkdir(parents=True)
        (self.directory / "result" / "arrangement.abc").write_text(ABC, encoding="utf-8")
        (self.directory / "result" / "score.abc").write_text(ABC.replace('V: Accomp\n[C,E,G,]4|\n', ''), encoding="utf-8")
        (self.directory / "result" / "beat.lab").write_text('0 1 4 4\n.5 2 4 4\n1 3 4 4\n1.5 4 4 4\n', encoding="utf-8")
        (self.directory / "result.json").write_text('{"duration":2}', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_import_and_read_boundary_work_without_site_packages(self):
        code = f'''import sys
sys.path.insert(0, {str(ROOT)!r})
class BlockHeavy:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {{'music21','numpy','soundfile','pretty_midi','torch'}}:
            raise AssertionError('heavy import: '+fullname)
sys.meta_path.insert(0, BlockHeavy())
from pathlib import Path
from capabilities.score_lab.editing import read_current, inspect_abc
c=read_current({{'dataDir': Path({str(self.root)!r})}}, 'fixture')
assert c['revision']==0
assert inspect_abc(c['abc'])['voices']==['Vocal','Ins','Accomp']
'''
        result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_canonical_arrangement_fallback_and_changed_source_token(self):
        current = edit.read_current(self.config, "fixture")
        self.assertEqual(current["sourceAbc"], "arrangement.abc")
        self.assertEqual(current["revision"], 0)
        self.assertFalse((self.directory / 'edits').exists())
        path = self.directory / "result" / "arrangement.abc"
        path.write_text(ABC.replace('C2-C2', 'D2-D2'), encoding="utf-8")
        self.assertNotEqual(edit.read_current(self.config, "fixture")["sourceFingerprint"], current["sourceFingerprint"])
        path.unlink()
        self.assertEqual(edit.read_current(self.config, "fixture")["sourceAbc"], "score.abc")

    def test_exact_pitch_only_skeleton_including_chord_member(self):
        before = edit.inspect_abc(ABC)
        after = edit.inspect_abc(ABC.replace('C2-C2', '^C2-^C2').replace('[C,E,G,]', '[C,^E,G,]'))
        self.assertEqual(before['skeleton'], after['skeleton'])
        self.assertEqual(before['voices'], ['Vocal', 'Ins', 'Accomp'])
        for changed in (ABC.replace('C2-C2', 'C3-C1'), ABC.replace('M:4/4', 'M:2/2'),
                        ABC.replace('K:C', 'K:G'), ABC.replace('Z|', 'z4|'),
                        ABC.replace('C2-C2', 'C2C2'), ABC.replace('V: Ins\nZ|\n', ''),
                        ABC.replace('[C,E,G,]', '[C,E,]')):
            with self.subTest(changed=changed):
                try:
                    self.assertNotEqual(edit.inspect_abc(changed)['skeleton'], before['skeleton'])
                except edit.EditError:
                    pass

    def test_unsafe_invalid_oversize_inputs_fail_before_model_import(self):
        invalid = ['%%abc-include https://example.invalid/a.abc\n' + ABC,
                   ABC + 'I:abc-include file:///private\n', ABC.replace('C2-C2', 'C3/2D5/2'),
                   ABC.replace('C2-C2', '[I:include x]C4'), ABC.replace('Z|', 'Z9999|'),
                   ABC.replace('C2-C2', 'C9999999999'), ABC.replace('V: Ins', 'V: Foreign'),
                   ABC.replace('[C,E,G,]', '[]'), ABC + '\x00', 'x' * (edit.MAX_ABC_BYTES + 1)]
        for abc in invalid:
            with self.subTest(abc=abc[:80]), self.assertRaises(edit.EditError):
                edit.inspect_abc(abc)
        current = edit.read_current(self.config, 'fixture')
        draft = {"abc": ABC.replace('C2-C2', 'C3-C1'), "baseRevision": 0,
                 "sourceFingerprint": current['sourceFingerprint']}
        with self.assertRaises(edit.EditError):
            edit.commit_revision(self.config, 'fixture', draft, 'rhythm')
        self.assertFalse((self.directory / 'edits' / 'current.json').exists())

    def test_draft_schema_and_server_owned_path(self):
        current = edit.read_current(self.config, 'fixture')
        valid = {'abc': ABC, 'baseRevision': 0, 'sourceFingerprint': current['sourceFingerprint']}
        for draft in ({**valid, 'baseRevision': True}, {**valid, 'baseRevision': -1},
                      {**valid, 'baseRevision': 1024}, {**valid, 'sourceFingerprint': 'abc'},
                      {**valid, 'extraPath': '../secret'}, {'abc': ABC, 'baseRevision': 0}):
            with self.subTest(draft=draft), self.assertRaises(edit.EditError):
                edit.validate_draft(draft)
        written = edit.write_draft(self.config, 'fixture', valid, 'from-helper')
        self.assertEqual(edit.read_draft(self.config, 'fixture', written), valid)
        with self.assertRaises(edit.EditConflict):
            edit.write_draft(self.config, 'fixture', valid, 'from-helper')
        drafts = self.directory / 'edits' / 'drafts'
        drafts.mkdir(parents=True, exist_ok=True)
        draft_path = drafts / 'request-1.json'
        draft_path.write_text(json.dumps(valid), encoding='utf-8')
        self.assertEqual(edit.read_draft(self.config, 'fixture', draft_path), valid)
        foreign = self.root / 'foreign.json'
        foreign.write_text(json.dumps(valid), encoding='utf-8')
        with self.assertRaises(edit.EditError):
            edit.read_draft(self.config, 'fixture', foreign)
        draft_path.write_text('{"abc":"x","abc":"y"}', encoding='utf-8')
        with self.assertRaises(edit.EditError):
            edit.read_draft(self.config, 'fixture', draft_path)
        draft_path.write_bytes(b' ' * (edit.MAX_DRAFT_BYTES + 1))
        with self.assertRaises(edit.EditError):
            edit.read_draft(self.config, 'fixture', draft_path)

    def test_ids_and_white_lists_cannot_traverse_or_read_arbitrary_files(self):
        for sample in ('../fixture', 'a/b', 'a\\b', '', 'A'):
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                edit.read_current(self.config, sample)
        for revision in ('../x', 'a/b', 'a\\b', '', 'A', 'a' * 65):
            with self.subTest(revision=revision), self.assertRaises(edit.EditError):
                edit.read_revision_result(self.config, 'fixture', revision)
        for name, kind in (('../original.wav', 'media'), ('original.wav', 'media'),
                           ('result.json', 'export'), ('edited.mid', 'media'), ('edited.abc', 'path')):
            with self.subTest(name=name), self.assertRaises(edit.EditError):
                edit.revision_file(self.config, 'fixture', 'one', name, kind=kind)

    def test_symlink_or_junction_target_is_rejected(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        link = self.directory / 'edits'
        if sys.platform == 'win32':
            process = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(foreign)], capture_output=True, text=True)
            if process.returncode:
                self.skipTest('Junction creation unavailable')
        else:
            link.symlink_to(foreign, target_is_directory=True)
        try:
            with self.assertRaises(edit.EditError):
                edit.read_current(self.config, 'fixture')
        finally:
            # unlink/rmdir only the junction itself, never the target contents.
            if sys.platform == 'win32':
                link.rmdir()
            else:
                link.unlink()

    def test_cli_deadline_only_kills_its_owned_child_and_returns_json(self):
        drafts = self.directory / 'edits/drafts'
        drafts.mkdir(parents=True)
        current = edit.read_current(self.config, 'fixture')
        path = drafts / 'request.json'
        path.write_text(json.dumps({'abc': ABC, 'baseRevision': 0, 'sourceFingerprint': current['sourceFingerprint']}), encoding='utf-8')
        config = {**self.config, 'configPath': self.root / 'config.json', 'jobTimeoutSeconds': 7200}

        class Child:
            def __init__(self): self.calls = 0; self.killed = False
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    self.timeout = timeout
                    raise subprocess.TimeoutExpired('owned editing child', timeout)
                return '', ''
            def poll(self): return None
            def kill(self): self.killed = True

        class Job:
            name = None
            process = None
            closed = False
            def close(self): self.closed = True
            def __enter__(self): return self
            def __exit__(self, *args): self.close()

        child, job, output = Child(), Job(), io.StringIO()
        with patch.object(edit, 'load_config', return_value=config), patch.object(edit, 'OwnedJob', return_value=job), \
             patch.object(edit.subprocess, 'Popen', return_value=child), redirect_stdout(output):
            code = edit.main(['--sample', 'fixture', '--draft', str(path), '--revision', 'timeout'])
        self.assertEqual(code, 124)
        self.assertEqual(json.loads(output.getvalue())['code'], 'timeout')
        self.assertEqual(child.timeout, 600)
        self.assertTrue(child.killed and job.closed)
        self.assertIs(job.process, child)
        self.assertFalse((self.directory / 'edits/current.json').exists())


class ClockTests(unittest.TestCase):
    def test_tempo_changes_not_notation_bpm_and_pickup_padding(self):
        rows = [{'index': 0, 'start': .1, 'end': 1.3, 'leading_rest': .5, 'trailing_rest': 0},
                {'index': 1, 'start': 1.3, 'end': 4.5, 'leading_rest': 0, 'trailing_rest': 0}]
        beats = '.1 3 4 4\n.5 4 4 4\n1.3 1 4 4\n1.8 2 4 4\n2.4 3 4 4\n3.4 4 4 4\n4.5 1 4 4\n'
        clock = edit.ScoreClock([4, 4], rows, beats, 4.5)
        self.assertEqual(clock.note_seconds(Fraction(2), Fraction(3)), (.1, .5))
        self.assertAlmostEqual(clock.seconds(Fraction(7, 2)), .9)
        self.assertEqual(clock.note_seconds(Fraction(4), Fraction(8)), (1.3, 4.5))
        with self.assertRaises(edit.EditError):
            clock.note_seconds(Fraction(1), Fraction(3))
        with self.assertRaises(edit.EditError):
            clock.seconds(-1)

    def test_legacy_terminal_extrapolation_and_end_clipping(self):
        row = {'index': 0, 'start': 0, 'end': 1.8, 'leading_rest': 0, 'trailing_rest': 0}
        clock = edit.ScoreClock([4], [row], '0 1 4 4\n.5 2 4 4\n1 3 4 4\n1.5 4 4 4\n', 1.8, saved_beats=False)
        self.assertEqual(clock.synthetic_terminal_beats, 1)
        self.assertEqual(clock.note_seconds(Fraction(3), Fraction(4)), (1.5, 1.8))
        with self.assertRaises(edit.EditError):
            edit.ScoreClock([4], [row], '0 1 4 4\n0 2 4 4', 1.8)

    def test_short_meter_and_whole_note_padding(self):
        rows = [{'index': 0, 'start': .01, 'end': .27, 'leading_rest': .875, 'trailing_rest': 0},
                {'index': 1, 'start': .27, 'end': 1.8, 'leading_rest': 0, 'trailing_rest': 0}]
        beats = '.01 1 1 8\n.27 1 3 4\n.7 2 3 4\n1.2 3 3 4\n1.8 1 4 4\n'
        clock = edit.ScoreClock([4, 3], rows, beats, 1.8)
        self.assertEqual(clock.note_seconds(Fraction(7, 2), Fraction(4)), (.01, .27))
        self.assertEqual(clock.note_seconds(Fraction(4), Fraction(7)), (.27, 1.8))
        with self.assertRaises(edit.EditError):
            clock.note_seconds(Fraction(3), Fraction(4))


@unittest.skipUnless(shutil.which('node'), 'Node is required for pure helper tests')
class JavascriptTests(unittest.TestCase):
    def test_pitch_chord_ties_accidentals_ranges_and_immutable_history(self):
        code = r'''
import assert from 'node:assert/strict';
import * as e from './capabilities/score_lab/web/editor.mjs';
const h='X:1\nT:中文🌼\nM:4/4\nL:1/4\nV: Vocal\nV: Ins\nV: Accomp\nK:G\n';
const abc=h+'V: Vocal\nFf=Ff|F2[K:C]F2|\nV: Ins\nZ2|\nV: Accomp\n[C,-E,G,]4|[C,E,G,]4|\n';
const tokens=e.listPitchTokens(abc);
assert.deepEqual(tokens.filter(n=>n.voice==='Vocal').map(n=>n.pitch),[66,78,65,77,66,65]);
const changed=e.transposePitch(abc,tokens[0],1);
assert.deepEqual(e.listPitchTokens(changed.abc).filter(n=>n.voice==='Vocal').map(n=>n.pitch),[67,78,65,77,66,65]);
assert.equal(changed.fromPitch,66); assert.equal(changed.toPitch,67);
assert.equal(e.selectPitch(changed.abc,changed.selection).pitch,67);
const chord=tokens.find(n=>n.voice==='Accomp');
const chordRange={startChar:chord.startChar-1,endChar:abc.indexOf(']',chord.startChar)+2};
assert.throws(()=>e.transposePitch(abc,chordRange,1), {code:'ambiguous_selection'});
const chordEdit=e.transposePitch(abc,{...chordRange,chordIndex:0},1);
assert.equal(chordEdit.changedFragments,2);
assert.deepEqual(e.listPitchTokens(chordEdit.abc).filter(n=>n.voice==='Accomp').map(n=>n.pitch),[49,52,55,49,52,55]);
assert.throws(()=>e.transposePitch(abc,chord,4),{code:'pitch_collision'});
assert.throws(()=>e.transposePitch(abc,{startChar:0,endChar:3},1),{code:'not_a_pitch'});
assert.throws(()=>e.transposePitch(abc,{startChar:-1,endChar:3},1),{code:'invalid_selection'});
const min=h.replace('K:G','K:C')+'V: Vocal\nA,,,,4|\nV: Ins\nZ|\nV: Accomp\nZ|\n';
assert.throws(()=>e.transposePitch(min,e.listPitchTokens(min)[0],-1),{code:'pitch_bounds'});
const initial=e.createEditor({abc,revision:0,sourceFingerprint:'a'.repeat(64)});
const {state}=e.applyPitchEdit(initial,tokens[0],1);
assert.equal(initial.abc,abc); assert.equal(initial.past.length,0);
assert.equal(e.undo(state).abc,abc); assert.equal(e.redo(e.undo(state)).abc,state.abc);
assert.equal(e.setDraftAbc(e.undo(state),chordEdit.abc).future.length,0);
assert.deepEqual(e.makeDraft(state),{abc:state.abc,baseRevision:0,sourceFingerprint:'a'.repeat(64)});
assert.throws(()=>e.setDraftAbc(state,state.abc.replace('M:4/4','M:2/2')),{code:'rhythm_locked'});
assert.throws(()=>e.setDraftAbc(state,'%%abc-include https://bad/\n'+state.abc),{code:'unsafe_abc'});
assert.throws(()=>e.setDraftAbc(state,state.abc.replace('=C,','=D,')),{code:'invalid_tie'});
assert.throws(()=>e.makeDraft({...state,baseRevision:true}),{code:'invalid_state'});
console.log('pure editor: pitch/chord/ties/accidental isolation/UTF16/undo/redo/bounds OK');
'''
        process = subprocess.run(['node', '--input-type=module', '-e', code], cwd=ROOT,
                                 capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)


if __name__ == '__main__':
    unittest.main()
