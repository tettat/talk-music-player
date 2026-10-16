/** DOM-free pitch-only ABC editing helpers. See ../EDITING.txt.
 * Character ranges are JS UTF-16, zero-based, end-exclusive (abcjs convention).
 * No eval, HTML, imports, URLs, network, or third-party editor runtime.
 */
export const EDITOR_PROTOCOL = 1;
export const PITCH_LIMITS = Object.freeze({ min: 21, max: 109 });
const MAX_ABC_BYTES = 128 * 1024;
const MAX_HISTORY = 50;
const roles = new Set(['Vocal', 'Ins', 'Accomp']);
const pitchPattern = /(?:\^{1,2}|_{1,2}|=)?[A-Ga-g][,']{0,4}/y;
const keyPattern = /^[A-G](?:#{1,2}|b{1,2})?(?:m|maj|min)?$/;
const natural = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
const majorKeys = { C: 0, G: 1, D: 2, A: 3, E: 4, B: 5, 'F#': 6, 'C#': 7,
  F: -1, Bb: -2, Eb: -3, Ab: -4, Db: -5, Gb: -6, Cb: -7 };
const minorKeys = { A: 0, E: 1, B: 2, 'F#': 3, 'C#': 4, 'G#': 5, 'D#': 6, 'A#': 7,
  D: -1, G: -2, C: -3, F: -4, Bb: -5, Eb: -6, Ab: -7 };

export class ScoreEditError extends Error {
  constructor(code, message) { super(message); this.name = 'ScoreEditError'; this.code = code; }
}
function fail(code, message) { throw new ScoreEditError(code, message); }
function normalize(abc) {
  if (typeof abc !== 'string' || !abc || new TextEncoder().encode(abc).length > MAX_ABC_BYTES ||
      /[\u0000-\u0008\u000b-\u001f\u007f]/.test(abc.replaceAll('\r\n', '\n')) ||
      /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/u.test(abc)) {
    fail('invalid_abc', 'ABC is empty, oversized, or contains invalid characters');
  }
  return abc.replaceAll('\r\n', '\n').replace(/\n*$/, '\n');
}
function keyDefaults(value) {
  if (!keyPattern.test(value)) fail('unsupported_key', 'Pitch helper supports major/minor canonical keys only');
  const minor = /(?:m|min)$/.test(value);
  const root = value.replace(/(?:maj|min|m)$/, '');
  const count = (minor ? minorKeys : majorKeys)[root];
  if (count === undefined) fail('unsupported_key', 'Unsupported key signature');
  return Object.fromEntries([...((count < 0 ? 'BEADGCF' : 'FCGDAEB').slice(0, Math.abs(count)))].map(c => [c, Math.sign(count)]));
}
function bounds(pitch) {
  if (!Number.isInteger(pitch) || pitch < PITCH_LIMITS.min || pitch > PITCH_LIMITS.max) {
    fail('pitch_bounds', 'FluidR3 piano pitches must be MIDI 21–109');
  }
}
function explicit(note, pitch = note.pitch, keepLetter = false) {
  bounds(pitch);
  if (keepLetter) {
    const alter = pitch - note.naturalPitch;
    return (alter > 0 ? '^'.repeat(alter) : alter < 0 ? '_'.repeat(-alter) : '=') + note.letter + note.octave;
  }
  const names = ['C', '^C', 'D', '^D', 'E', 'F', '^F', 'G', '^G', 'A', '^A', 'B'];
  let name = names[pitch % 12];
  const octave = Math.floor(pitch / 12) - 5;
  if (octave >= 1) name = name.toLowerCase();
  const suffix = octave >= 1 ? "'".repeat(octave - 1) : ','.repeat(-octave);
  return (name.startsWith('^') ? '' : '=') + name + suffix;
}

function scan(abc) {
  abc = normalize(abc);
  const notes = [], skeleton = [], voices = new Map();
  let header = true, active = null, meter = [4, 4], unit = null, defaults = {}, cursor = 0, eventId = 0;
  const seen = new Set();
  for (const raw of abc.match(/[^\n]*\n/g) ?? []) {
    const line = raw.slice(0, -1);
    if (line.length > 8192) fail('invalid_abc', 'ABC line exceeds size limit');
    if (line.startsWith('%%')) fail('unsafe_abc', 'ABC directives/includes are forbidden');
    if (!line.trim() || line.startsWith('%')) { skeleton.push(raw); cursor += raw.length; continue; }
    const field = /^([A-Za-z]):(.*)$/.exec(line);
    if (field) {
      const [, name, rawValue] = field, value = rawValue.trim();
      if (!['X', 'T', 'M', 'L', 'Q', 'V', 'K'].includes(name)) fail('unsafe_abc', 'Unsupported ABC field/directive');
      if (name === 'V') {
        const voice = value.split(/\s/)[0];
        if (!roles.has(voice)) fail('unsupported_voice', 'Unknown voice must not be silently dropped');
        if (header) {
          if (voices.has(voice)) fail('invalid_abc', 'Duplicate voice declaration');
          voices.set(voice, null);
        } else {
          if (!voices.has(voice) || value !== voice) fail('invalid_abc', 'Invalid voice switch');
          if (voices.get(voice) === null) voices.set(voice, { voice, position: 0, within: 0, bars: 0,
            meter: [...meter], defaults: { ...defaults }, accidentals: {} });
          active = voices.get(voice);
        }
      } else if (name === 'M') {
        const m = /^([1-9][0-9]?)\/(1|2|4|8|16|32|64)$/.exec(value);
        if (!m || Number(m[1]) > 32) fail('invalid_abc', 'Unsupported meter');
        if (header) meter = [Number(m[1]), Number(m[2])];
        else if (active) active.meter = [Number(m[1]), Number(m[2])];
        else fail('invalid_abc', 'Body meter without a voice');
      } else if (name === 'L') {
        const m = /^1\/(1|2|4|8|16|32|64|128)$/.exec(value);
        if (!header || unit !== null || !m) fail('invalid_abc', 'Unsupported unit length');
        unit = Number(m[1]);
      } else if (name === 'K') {
        const keys = keyDefaults(value);
        if (header) {
          if (!unit || !voices.size || !seen.has('M') || !seen.has('X') || !seen.has('T')) fail('invalid_abc', 'Missing canonical header');
          defaults = keys; header = false;
        } else if (active) { active.defaults = keys; active.accidentals = {}; }
        else fail('invalid_abc', 'Body key without a voice');
      } else if (!header) fail('invalid_abc', 'Unsupported body field');
      else if (name === 'X' && value !== '1') fail('invalid_abc', 'Exactly one tune is required');
      else if (name === 'Q' && !/^1\/4=[1-9][0-9]{0,2}$/.test(value)) fail('invalid_abc', 'Unsupported notation tempo');
      if (header && name !== 'V') {
        if (seen.has(name)) fail('invalid_abc', 'Duplicate header field');
        seen.add(name);
      }
      skeleton.push(raw); cursor += raw.length; continue;
    }
    if (header || !active) fail('invalid_abc', 'Music must belong to a declared voice');
    const out = [];
    let i = 0;
    function readPitch() {
      pitchPattern.lastIndex = i;
      const match = pitchPattern.exec(line);
      if (!match) fail('invalid_abc', 'Invalid ABC pitch');
      const parts = /^(\^{1,2}|_{1,2}|=)?([A-Ga-g])([,']*)$/.exec(match[0]);
      const [, marks = '', letter, octave] = parts, step = letter.toUpperCase();
      if (marks) active.accidentals[step] = [...marks].reduce((a, c) => a + (c === '^' ? 1 : c === '_' ? -1 : 0), 0);
      const alter = active.accidentals[step] ?? active.defaults[step] ?? 0;
      const naturalPitch = 60 + natural[step] + (letter === letter.toLowerCase() ? 12 : 0) +
        [...octave].reduce((a, c) => a + (c === "'" ? 12 : -12), 0);
      const note = { startChar: cursor + i, endChar: cursor + pitchPattern.lastIndex,
        voice: active.voice, eventId, letter, octave, naturalPitch, pitch: naturalPitch + alter,
        offset: active.position + active.within, duration: 0, outgoing: false, chain: null };
      bounds(note.pitch);
      notes.push(note);
      if (notes.length > 60000) fail('invalid_abc', 'Too many pitch fragments');
      i = pitchPattern.lastIndex; out.push('@');
      return note;
    }
    function readDuration() {
      const text = /^\d*/.exec(line.slice(i))[0];
      if (text.length > 4 || text && (text.startsWith('0') || Number(text) > 1024)) fail('invalid_abc', 'Invalid/oversize duration');
      i += text.length; out.push(text);
      return Number(text || 1) * 4 / unit;
    }
    function endBar() {
      if (active.within !== active.meter[0] * 4 / active.meter[1]) fail('invalid_rhythm', 'Measure duration disagrees with its meter');
      active.position += active.within; active.within = 0; active.accidentals = {}; active.bars += 1;
    }
    while (i < line.length) {
      const ch = line[i];
      if (ch === ' ' || ch === '\t') { out.push(ch); i += 1; }
      else if (ch === '"') {
        const end = line.indexOf('"', i + 1);
        if (end < 0 || end - i > 129) fail('invalid_abc', 'Invalid chord label');
        out.push(line.slice(i, end + 1)); i = end + 1;
      } else if (line.startsWith('[K:', i)) {
        const end = line.indexOf(']', i + 3);
        if (end < 0) fail('invalid_abc', 'Invalid inline key');
        active.defaults = keyDefaults(line.slice(i + 3, end)); active.accidentals = {};
        out.push(line.slice(i, end + 1)); i = end + 1;
      } else if (ch === '[') {
        out.push('['); i += 1; eventId += 1;
        const chord = [];
        while (i < line.length && line[i] !== ']') {
          const note = readPitch(); chord.push(note);
          if (chord.length > 16) fail('invalid_abc', 'Chord exceeds pitch limit');
          if (line[i] === '-') { note.outgoing = true; out.push('-'); i += 1; }
        }
        if (!chord.length || i === line.length) fail('invalid_abc', 'Invalid chord');
        out.push(']'); i += 1;
        const duration = readDuration(), outgoing = line[i] === '-';
        if (outgoing) { out.push('-'); i += 1; }
        for (const note of chord) { note.duration = duration; note.outgoing ||= outgoing; }
        if (new Set(chord.map(n => n.pitch)).size !== chord.length) fail('pitch_collision', 'Duplicate chord pitch');
        active.within += duration;
      } else if (ch === 'Z') {
        const match = /^Z([1-9][0-9]{0,3})?\|/.exec(line.slice(i));
        if (!match || active.within) fail('invalid_abc', 'Invalid multimeasure rest');
        const count = Number(match[1] || 1);
        active.position += count * active.meter[0] * 4 / active.meter[1]; active.bars += count; active.accidentals = {};
        out.push(match[0]); i += match[0].length;
      } else if (ch === 'z') { out.push('z'); i += 1; active.within += readDuration(); }
      else if (ch === '|') { endBar(); out.push('|'); i += 1; }
      else {
        eventId += 1;
        const note = readPitch(); note.duration = readDuration();
        if (line[i] === '-') { note.outgoing = true; out.push('-'); i += 1; }
        active.within += note.duration;
      }
      if (active.bars > 2048) fail('invalid_abc', 'Too many expanded measures');
    }
    skeleton.push(out.join('') + '\n'); cursor += raw.length;
  }
  const states = [...voices.values()];
  if (header || !notes.length || states.some(s => !s || s.within || !s.bars) ||
      new Set(states.map(s => s.bars)).size !== 1) fail('invalid_abc', 'Incomplete/unequal voice timelines');
  const ties = new Map();
  notes.forEach((note, index) => {
    const key = `${note.voice}:${note.pitch}`, previous = ties.get(key);
    if (previous && previous.offset + previous.duration !== note.offset) fail('invalid_tie', 'Discontinuous tie');
    note.chain = previous ? previous.chain : index;
    if (note.outgoing) ties.set(key, note); else ties.delete(key);
  });
  if (ties.size) fail('invalid_tie', 'Unclosed tie; edit every tied segment together');
  return { abc, notes, skeleton: skeleton.join('') };
}

/** Selectable individual pitch tokens, including each member of every chord. */
export function listPitchTokens(abc) { return scan(abc).notes.map(n => ({ ...n })); }

/** A whole chord range is ambiguous unless chordIndex (0-based) is supplied. */
export function selectPitch(abc, selection) {
  const scanned = scan(abc);
  if (!selection || !Number.isInteger(selection.startChar) || !Number.isInteger(selection.endChar) ||
      selection.startChar < 0 || selection.endChar < selection.startChar || selection.endChar > scanned.abc.length) {
    fail('invalid_selection', 'Invalid end-exclusive character range');
  }
  const candidates = scanned.notes.filter(n => selection.startChar === selection.endChar ?
    n.startChar <= selection.startChar && selection.startChar < n.endChar :
    n.startChar < selection.endChar && n.endChar > selection.startChar);
  if (!candidates.length) fail('not_a_pitch', 'Select a pitch token, not a rest/header/chord label');
  if (new Set(candidates.map(n => n.eventId)).size !== 1) fail('ambiguous_selection', 'Select one musical event at a time');
  const index = selection.chordIndex ?? (candidates.length === 1 ? 0 : null);
  if (!Number.isInteger(index) || index < 0 || index >= candidates.length) fail('ambiguous_selection', 'Choose a chord member with chordIndex');
  return { ...candidates[index] };
}

/** Transpose one note and its COMPLETE tie chain, preserving all other pitches.
 * Every pitch is made explicit, preventing accidental spill into later notes.
 * Re-render abcjs afterwards: prior character ranges are invalid after an edit.
 */
export function transposePitch(abc, selection, semitones) {
  if (!Number.isInteger(semitones) || Math.abs(semitones) > 12) fail('invalid_delta', 'Use an integer semitone delta between -12 and 12');
  const scanned = scan(abc), selected = selectPitch(scanned.abc, selection);
  const selectedNotes = scanned.notes.filter(n => n.chain === selected.chain);
  for (const note of selectedNotes) bounds(note.pitch + semitones);
  const replaced = new Map(scanned.notes.map(n => [n, n.chain === selected.chain ? n.pitch + semitones : n.pitch]));
  const groups = new Map();
  for (const note of scanned.notes) {
    const pitches = groups.get(note.eventId) ?? new Set();
    const pitch = replaced.get(note);
    if (pitches.has(pitch)) fail('pitch_collision', 'Edit would duplicate a pitch in the chord');
    pitches.add(pitch); groups.set(note.eventId, pitches);
  }
  let result = '', cursor = 0, newSelection;
  for (const note of scanned.notes) {
    result += scanned.abc.slice(cursor, note.startChar);
    const startChar = result.length;
    const isSelected = note.chain === selected.chain;
    result += explicit(note, replaced.get(note), !isSelected);
    if (note.startChar === selected.startChar) newSelection = { startChar, endChar: result.length };
    cursor = note.endChar;
  }
  result += scanned.abc.slice(cursor);
  const checked = scan(result);
  if (checked.skeleton !== scanned.skeleton || checked.notes.some((n, i) => n.pitch !== replaced.get(scanned.notes[i]))) {
    fail('unsafe_edit', 'Pitch/tie structure changed unexpectedly');
  }
  return { abc: result, selection: newSelection, changedFragments: selectedNotes.length,
    fromPitch: selected.pitch, toPitch: selected.pitch + semitones };
}

function validState(state) {
  if (!state || state.schemaVersion !== EDITOR_PROTOCOL || !Number.isInteger(state.baseRevision) ||
      state.baseRevision < 0 || state.baseRevision >= 1024 || typeof state.sourceFingerprint !== 'string' ||
      !/^[a-f0-9]{64}$/.test(state.sourceFingerprint) || typeof state.abc !== 'string' ||
      !Array.isArray(state.past) || !Array.isArray(state.future) || state.past.length > MAX_HISTORY ||
      state.future.length > MAX_HISTORY || [...state.past, ...state.future].some(a => typeof a !== 'string')) {
    fail('invalid_state', 'Invalid editor state');
  }
}
export function createEditor(current) {
  const state = { schemaVersion: EDITOR_PROTOCOL, abc: scan(current?.abc).abc,
    baseRevision: current.revision, sourceFingerprint: current.sourceFingerprint, past: [], future: [] };
  validState(state);
  return state;
}
/** Advanced ABCsafe source entry; only pitch lexemes may differ. Server is authoritative. */
export function setDraftAbc(state, abc) {
  validState(state);
  const before = scan(state.abc), after = scan(abc);
  if (before.skeleton !== after.skeleton) fail('rhythm_locked', 'Only pitch lexemes may change; preserve all other source text');
  if (before.abc === after.abc) return state;
  return { ...state, abc: after.abc, past: [...state.past, state.abc].slice(-MAX_HISTORY), future: [] };
}
export function applyPitchEdit(state, selection, semitones) {
  validState(state);
  const edit = transposePitch(state.abc, selection, semitones);
  return { state: setDraftAbc(state, edit.abc), edit };
}
export function undo(state) {
  validState(state);
  if (!state.past.length) return state;
  return { ...state, abc: state.past.at(-1), past: state.past.slice(0, -1), future: [state.abc, ...state.future].slice(0, MAX_HISTORY) };
}
export function redo(state) {
  validState(state);
  if (!state.future.length) return state;
  return { ...state, abc: state.future[0], past: [...state.past, state.abc].slice(-MAX_HISTORY), future: state.future.slice(1) };
}
/** POST this value; revisionId is a separate fresh transaction ID, not a path. */
export function makeDraft(state) {
  validState(state);
  return { abc: state.abc, baseRevision: state.baseRevision, sourceFingerprint: state.sourceFingerprint };
}
