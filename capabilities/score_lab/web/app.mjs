import {AudioTransport, abcMeasureIndex, audioURL, formatTime, measureAt, rollLayout} from './timeline.mjs';

const embedded = location.pathname.startsWith('/extensions/score-lab/');
const base = embedded ? '/extensions/score-lab' : '';
const localURL = path => base + path;
if (embedded) document.documentElement.classList.add('embedded');

const $ = id => document.getElementById(id);
const audio = $('audio');
const transport = new AudioTransport(audio);
const state = {catalog: [], selected: null, data: null, mode: 'original', part: 'mix',
  view: 'score', token: '', version: 0, measure: -1, request: null, raf: 0};
const phases = {queued: '等待识别', preparing: '读取片段', loading: '加载模型',
  recognizing: '识别中', notation: '生成乐谱', rendering: '生成回放', ready: '已就绪', error: '未完成', interrupted: '已中断'};
const colors = {vocal: '#8e9f78', instrumental: '#86a6b1', chords: '#c8b680'};
const sourceNames = {qq: 'QQ 音乐', netease: '网易云音乐', local: '本机音频'};
let toastTimer, pollTimer, resizeTimer;
let editHelpers, editDraft, editCurrent, editing = false, editSelection = null, editSaving = false, editTimer;
const editorReady = embedded ? import('./editor.mjs').then(value => {editHelpers = value;}).catch(() => {}) : Promise.resolve();
const dirty = () => Boolean(editDraft && editCurrent && editDraft.abc !== editCurrent.abc);

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS(svg.namespaceURI, 'use');
  use.setAttribute('href', `#i-${name}`); svg.append(use);
  return svg;
}

function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden = true;}, 4500);
}

async function getJSON(url, options) {
  if (embedded && options?.method === 'POST') options = {...options, headers: {...options.headers, 'X-Talk-CSRF': state.token}};
  const response = await fetch(localURL(url), options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.message || ({SCORE_BUSY: '请等待当前任务完成', SCORE_EDIT_CONFLICT: '乐谱已更新，请重新打开', SCORE_RUNTIME_MISSING: '识谱模块未就绪'}[value.code]) || '暂时无法读取');
  return value;
}

function renderLibrary() {
  $('sample-count').textContent = state.catalog.length.toString().padStart(2, '0');
  for (const row of $('library').querySelectorAll('[data-sample]')) {
    if (!state.catalog.some(item => item.id === row.dataset.sample)) row.remove();
  }
  for (const item of state.catalog) {
    let button = [...$('library').querySelectorAll('[data-sample]')].find(row => row.dataset.sample === item.id);
    if (!button) {
      button = document.createElement('button'); button.dataset.sample = item.id;
      const art = document.createElement('span'); art.className = 'sample-art'; art.append(icon('note'));
      const text = document.createElement('span'); text.className = 'sample-text';
      text.append(document.createElement('strong'), document.createElement('small'));
      button.append(art, text); button.addEventListener('click', () => selectSample(item.id));
      $('library').append(button);
    }
    button.className = `sample${item.id === state.selected ? ' active' : ''}`;
    button.setAttribute('aria-pressed', String(item.id === state.selected));
    button.querySelector('strong').textContent = item.title;
    const detail = button.querySelector('small');
    detail.textContent = item.status.state === 'completed' ? item.artist || '本机音频' : phases[item.status.phase] || '待识别';
  }
}

function setEmpty(message) {
  $('empty-message').textContent = message; $('empty-state').hidden = false;
}

async function selectSample(id) {
  const version = ++state.version;
  state.request?.abort(); state.request = new AbortController();
  clearTimeout(pollTimer); transport.stop(); cancelAnimationFrame(state.raf);
  clearTimeout(editTimer); editing = false; editSaving = false; editDraft = editCurrent = editSelection = null;
  syncEditor();
  state.selected = id; state.data = null; state.measure = -1;
  $('score').replaceChildren(); $('chord-lane').replaceChildren(); $('facts').replaceChildren();
  $('play').disabled = true; $('export-button').disabled = true; $('export-menu').hidden = true;
  $('export-button').setAttribute('aria-expanded', 'false'); $('measure-position').textContent = '';
  setEmpty('正在读取…'); renderLibrary(); updateControls(); drawWaveform(0); drawRoll(0);
  try {
    const info = await getJSON(`/api/samples/${id}`, {signal: state.request.signal});
    if (version !== state.version) return;
    applyInfo(info);
  } catch (error) {
    if (error.name !== 'AbortError' && version === state.version) setEmpty(error.message);
  }
}

function applyInfo(info) {
  const index = state.catalog.findIndex(s => s.id === info.id);
  if (index !== -1) state.catalog[index] = info;
  $('song-title').textContent = info.title;
  $('song-artist').textContent = info.artist || sourceNames[info.source] || '本机音频';
  const durationKnown = Number.isFinite(info.duration) && info.duration > 0;
  $('source-label').textContent = `${sourceNames[info.source] || '本机音频'} / ${info.full && info.offset === 0 ? '整首' : durationKnown ? `${Math.round(info.duration)}s` : '音频'}`;
  $('clip-range').textContent = durationKnown ? `${formatTime(info.offset)} — ${formatTime(info.offset + info.duration)}` : '';
  renderLibrary();
  if (info.status.state !== 'completed') {
    const windows = info.status.windows > 1 ? ` · ${info.status.window} / ${info.status.windows}` : '';
    setEmpty(info.status.state === 'failed' ? info.status.message || '识别未完成' : (phases[info.status.phase] || '识别中') + windows);
    if (['queued', 'running'].includes(info.status.state)) pollSample(info.id, state.version);
    return;
  }
  state.data = info.result;
  state.originalData = info.originalResult || info.result;
  editCurrent = info.editing || null;
  if (editHelpers && editCurrent) {
    try {editDraft = editHelpers.createEditor(editCurrent);} catch {editDraft = null;}
  }
  $('edit-toggle').hidden = !embedded || !editDraft;
  const pitches = state.data.tracks.flatMap(t => t.notes.map(n => n.pitch));
  state.pitchLow = Math.max(21, Math.min(48, ...pitches) - 2);
  state.pitchHigh = Math.min(109, Math.max(72, ...pitches) + 2);
  $('roll-scroll').scrollLeft = 0;
  if (!state.data.audioParts.includes(state.part)) state.part = 'mix';
  $('seek').max = info.result.duration; $('seek').value = 0; $('total').textContent = formatTime(info.result.duration);
  renderFacts(); renderScore(); renderChords(); renderExports(); updateControls();
  $('export-button').disabled = false;
  void loadAudio(0, false);
  tick();
  syncEditor();
}

function pollSample(id, version) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const info = await getJSON(`/api/samples/${id}`);
      if (version === state.version && id === state.selected) applyInfo(info);
    } catch (error) {
      if (version === state.version) {setEmpty(error.message); pollSample(id, version);}
    }
  }, 1200);
}

function renderFacts() {
  const d = state.data;
  const key = d.key ? /^[A-G][b#]?$/.test(d.key) ? `${d.key} 大调` : d.key.replace(':major', ' 大调').replace(':minor', ' 小调') : '';
  const facts = [key, d.notationBpm && `谱面 ${d.notationBpm} BPM`, d.meter,
    `${d.counts.vocal + d.counts.instrumental} 个旋律音符`, `${d.chords.length} 个和弦`].filter(Boolean);
  $('facts').replaceChildren(...facts.map(value => {const tag = document.createElement('span'); tag.className = 'fact'; tag.textContent = value; return tag;}));
}

// Display adaptations only. Original model ABC and original-time MIDI stay
// separate from the derived accompaniment arrangement.
function displayABC(original) {
  let meter = [4, 4], unit = [1, 8];
  // abcjs 6.6.3 accepts spacing as ABC directives, not in its format object.
  const compact = original.replace(/^X:.*$/m, '$&\n%%topspace 1\n%%titlespace 1\n%%musicspace 4\n%%sysstaffsep 30');
  return compact.replaceAll('name="Vocal Melody"', 'name="人声旋律"').replaceAll('name="Ins Melody"', 'name="器乐旋律"').split(/\r?\n/).map(line => {
    const m = line.match(/^M:\s*(\d+)\/(\d+)/); if (m) meter = m.slice(1).map(Number);
    const l = line.match(/^L:\s*(\d+)\/(\d+)/); if (l) unit = l.slice(1).map(Number);
    if (/^[A-Za-z]:|^%/.test(line)) return line;
    return line.replace(/"[^"\n]*"|\[[^\]\n]*\]|Z(\d*)\|/g, (token, count) =>
      count === undefined ? token : (`z${meter[0] * unit[1] / (meter[1] * unit[0])}|`).repeat(Number(count || 1)));
  }).join('\n');
}

function renderScore() {
  if (!state.data) return;
  $('empty-state').hidden = true;
  $('score-caption').textContent = state.view === 'score' ? 'SheetSage2 · 量化谱面' : 'SheetSage2 · 原始时间';
  $('score-scroll').hidden = state.view !== 'score'; $('roll-scroll').hidden = state.view !== 'roll';
  const scoreABC = editing && editDraft ? editDraft.abc : state.data.arrangement?.verified ? state.data.arrangement.abc : state.data.abc;
  if (!scoreABC) {if (state.view === 'score') setEmpty('此片段未生成五线谱'); drawRoll(audio.currentTime || 0); return;}
  if (!window.ABCJS) {if (state.view === 'score') setEmpty('谱面组件未加载'); drawRoll(audio.currentTime || 0); return;}
  try {
    const width = Math.max(270, document.querySelector('.workspace-toolbar').clientWidth);
    const tunes = window.ABCJS.renderAbc('score', editing ? scoreABC : displayABC(scoreABC), {
      staffwidth: (width - 82) / (embedded ? .86 : 1), scale: embedded ? .86 : 1,
      responsive: 'resize', add_classes: true, oneSvgPerLine: true,
      paddingtop: 12, paddingbottom: 15, paddingleft: 5, paddingright: 10,
      wrap: {minSpacing: 1.3, maxSpacing: 2.6, preferredMeasuresPerLine: width < 500 ? 2 : 4},
      selectTypes: editing ? ['note'] : false, selectionColor: '#b89951',
      clickListener: (element, _tune, classes, analysis) => {
        if (editing) {selectEditorNote(element, analysis); return;}
        const index = abcMeasureIndex(classes);
        const measure = state.data.measures.find(m => m.index === index);
        if (measure) seek(measure.start);
      },
    });
    if (!tunes.length) throw new Error('Empty score');
    if (tunes.some(t => t.warnings?.length)) console.warn('ABC rendering warnings', tunes.flatMap(t => t.warnings || []));
    state.measure = -1;
  } catch (error) {
    if (state.view === 'score') setEmpty('谱面暂不可用，请切换音符视图');
    console.warn('Score rendering unavailable', error);
  }
  drawRoll(audio.currentTime || 0);
}

function renderChords() {
  $('chord-lane').replaceChildren(...state.data.chords.map(chord => {
    const span = document.createElement('span');
    span.style.left = `${chord.start / state.data.duration * 100}%`;
    span.style.width = `${(chord.end - chord.start) / state.data.duration * 100}%`;
    span.textContent = chordName(chord.label);
    return span;
  }));
}

function chordName(label) {
  const match = label.match(/^([A-G][b#]?)(?::([^/]+))?(?:\/([b#]?)(\d+))?$/);
  if (!match) return label;
  const [, root, quality = 'maj', accidental = '', degree] = match;
  const name = root + (quality === 'maj' ? '' : quality.startsWith('min') ? quality.replace('min', 'm') : quality);
  if (!degree) return name;
  const roots = {C:0,D:2,E:4,F:5,G:7,A:9,B:11};
  const intervals = [0,2,4,5,7,9,11];
  const pitch = roots[root[0]] + (root[1] === '#' ? 1 : root[1] === 'b' ? -1 : 0) +
    intervals[(Number(degree)-1)%7] + (accidental === '#' ? 1 : accidental === 'b' ? -1 : 0);
  return `${name}/${['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'][(pitch+24)%12]}`;
}

function renderExports() {
  const formats = {'transcription.mid': 'MIDI · 全部', 'melody_vocal.mid': 'MIDI · 人声旋律',
    'melody_instrumental.mid': 'MIDI · 器乐旋律', 'chords.mid': 'MIDI · 和弦',
    'score.musicxml': state.data.arrangement ? '旋律 MusicXML' : 'MusicXML',
    'score.abc': state.data.arrangement ? '模型原始 ABC' : 'ABC',
    'arrangement.musicxml': 'MusicXML · 含伴奏', 'arrangement.abc': 'ABC · 含伴奏', 'notes.json': '音符 JSON',
    'edited.abc': 'ABC', 'edited.musicxml': 'MusicXML', 'edited.mid': 'MIDI'};
  $('export-menu').replaceChildren(...state.data.exports.map(name => {
    const link = document.createElement('a'); link.textContent = formats[name] || name;
    link.href = localURL(state.data.revisionId ? `/revisions/${state.selected}/${state.data.revisionId}/export/${name}` : `/export/${state.selected}/${name}`); link.download = name;
    link.addEventListener('click', () => {$('export-menu').hidden = true; $('export-button').setAttribute('aria-expanded', 'false');});
    return link;
  }));
}

async function loadAudio(seconds, resume) {
  $('play').disabled = true;
  try {
    const url = state.mode === 'piano' && state.data.revisionId ? `/revisions/${state.selected}/${state.data.revisionId}/media/piano_${state.part}.wav` : audioURL(state.selected, state.mode, state.part);
    const loaded = await transport.load(localURL(url), seconds, resume);
    if (loaded) {$('play').disabled = false; tick();}
  } catch (error) {toast(error.message); $('play').disabled = true;}
  updateControls();
}

function updateControls() {
  for (const mode of ['original', 'piano']) {
    $(`mode-${mode}`).classList.toggle('active', state.mode === mode);
    $(`mode-${mode}`).setAttribute('aria-pressed', String(state.mode === mode));
    $(`mode-${mode}`).disabled = !state.data || mode === 'piano' && (dirty() || editSaving);
  }
  document.querySelector('.parts').classList.toggle('inactive', state.mode === 'original');
  document.querySelectorAll('[data-part]').forEach(button => {
    const active = state.part === button.dataset.part;
    button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    button.disabled = !state.data || !state.data.audioParts.includes(button.dataset.part) || dirty() || editSaving;
    button.title = button.disabled ? '此片段未识别到该声部' : '';
  });
  const playing = !audio.paused;
  $('play').replaceChildren(icon(playing ? 'pause' : 'play'));
  $('play').setAttribute('aria-label', playing ? '暂停' : '播放');
  for (const id of ['seek', 'back', 'next', 'repeat', 'speed']) $(id).disabled = !state.data;
  if (state.mode === 'piano' && (dirty() || editSaving)) $('play').disabled = true;
  $('export-button').disabled = !state.data || dirty() || editSaving;
}

async function switchMode(mode, part = state.part) {
  if (mode === 'piano' && (dirty() || editSaving)) {toast('请先保存编辑'); return;}
  if (!state.data || (mode === state.mode && part === state.part)) return;
  const seconds = audio.currentTime || 0, resume = !audio.paused;
  state.mode = mode; state.part = part; updateControls();
  await loadAudio(seconds, resume);
}

async function togglePlay() {
  if (!state.data || $('play').disabled || transport.loading) return;
  try {
    if (audio.paused) {
      if (audio.currentTime >= state.data.duration - .02) audio.currentTime = 0;
      await audio.play();
    } else audio.pause();
  } catch {toast('播放未开始，请再点一次播放');}
  updateControls(); tick();
}

function pitchName(pitch) {return ['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'][pitch % 12] + (Math.floor(pitch / 12) - 1);}
function syncEditor() {
  $('editor-toolbar').hidden = !editing;
  $('edit-toggle').setAttribute('aria-pressed', String(editing));
  document.documentElement.classList.toggle('is-editing', editing);
  $('edit-undo').disabled = editSaving || !editDraft?.past.length;
  $('edit-redo').disabled = editSaving || !editDraft?.future.length;
  $('edit-save').disabled = editSaving || !dirty();
  $('edit-discard').disabled = editSaving;
  for (const id of ['edit-up', 'edit-down', 'edit-chord-members']) $(id).disabled = editSaving || !editSelection;
  $('edit-toggle').disabled = editSaving;
  $('edit-source').disabled = editSaving;
  if (!editing) {$('edit-source').hidden = true; $('edit-source-toggle').setAttribute('aria-expanded', 'false');}
  $('edit-status').textContent = editSaving ? '保存中…' : dirty() ? '未保存' : editCurrent?.revision ? `修订 ${editCurrent.revision}` : '';
  if (editDraft && document.activeElement !== $('edit-source')) $('edit-source').value = editDraft.abc;
  if (!editSelection) {$('edit-selection').textContent = '选择音符'; $('edit-chord-members').hidden = true;}
  updateControls();
}
function selectEditorNote(element, analysis) {
  if (!editDraft || editSaving || !Number.isInteger(element.startChar) || !Number.isInteger(element.endChar)) return;
  try {
    const candidates = editHelpers.listPitchTokens(editDraft.abc).filter(n => n.startChar < element.endChar && n.endChar > element.startChar);
    if (!candidates.length) return;
    const candidate = candidates.find(n => n.letter + n.octave === analysis?.clickedName) || candidates[0];
    editSelection = {startChar: candidate.startChar, endChar: candidate.endChar};
    $('edit-selection').textContent = `${{Vocal:'人声',Ins:'器乐',Accomp:'伴奏'}[candidate.voice]} · ${pitchName(candidate.pitch)}`;
    $('edit-chord-members').replaceChildren(...candidates.map(n => {
      const option = document.createElement('option'); option.value = n.startChar; option.textContent = pitchName(n.pitch);
      option.selected = n.startChar === candidate.startChar; return option;
    }));
    $('edit-chord-members').hidden = candidates.length < 2;
    syncEditor();
  } catch {toast('此音符暂不可编辑');}
}
function changePitch(delta) {
  if (!editDraft || !editSelection || editSaving) return;
  try {
    const changed = editHelpers.applyPitchEdit(editDraft, editSelection, delta);
    editDraft = changed.state; editSelection = changed.edit.selection;
    $('edit-selection').textContent = pitchName(changed.edit.toPitch);
    $('edit-chord-members').hidden = true;
    audio.pause(); renderScore(); syncEditor();
  } catch (error) {toast(error.code === 'pitch_collision' ? '与和弦中的另一个音重复' : '此音高暂不可用');}
}
async function saveEdits() {
  if (!dirty() || editSaving) return;
  const id = state.selected, version = state.version;
  audio.pause(); editSaving = true; syncEditor();
  try {
    const accepted = await getJSON(`/api/samples/${id}/edit`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(editHelpers.makeDraft(editDraft))});
    let current;
    for (let attempt = 0; attempt < 620; attempt++) {
      await new Promise(resolve => {editTimer = setTimeout(resolve, 1000);});
      if (version !== state.version) return;
      current = await getJSON(`/api/samples/${id}/editor`);
      if (current.revisionId === accepted.revisionId && current.status.state === 'completed') break;
      if (current.status.state === 'failed') throw new Error('音高编辑未保存，请检查音符后重试');
    }
    if (current?.revisionId !== accepted.revisionId) throw new Error('保存超时，请重新打开乐谱');
    const info = await getJSON(`/api/samples/${id}`);
    if (version !== state.version) return;
    editSaving = false; editing = false; editSelection = null;
    applyInfo(info); toast('已保存');
  } catch (error) {if (version === state.version) {editSaving = false; toast(error.message); syncEditor();}}
}
$('edit-toggle').addEventListener('click', () => {
  if (!editDraft || editSaving) return;
  if (editing && dirty()) {toast('请保存或还原编辑'); return;}
  editing = !editing; editSelection = null;
  if (editing) {state.view = 'score'; $('view-score').setAttribute('aria-pressed', 'true'); $('view-score').classList.add('active'); $('view-roll').setAttribute('aria-pressed', 'false'); $('view-roll').classList.remove('active');}
  syncEditor(); renderScore();
});
$('edit-up').addEventListener('click', () => changePitch(1));
$('edit-down').addEventListener('click', () => changePitch(-1));
$('edit-undo').addEventListener('click', () => {editDraft = editHelpers.undo(editDraft); editSelection = null; renderScore(); syncEditor(); if (!dirty()) void loadAudio(audio.currentTime || 0, false);});
$('edit-redo').addEventListener('click', () => {editDraft = editHelpers.redo(editDraft); editSelection = null; audio.pause(); renderScore(); syncEditor();});
$('edit-discard').addEventListener('click', () => {editDraft = editHelpers.createEditor(editCurrent); editSelection = null; renderScore(); syncEditor(); void loadAudio(audio.currentTime || 0, false);});
$('edit-save').addEventListener('click', saveEdits);
$('edit-chord-members').addEventListener('change', event => {
  const note = editHelpers.listPitchTokens(editDraft.abc).find(n => n.startChar === Number(event.target.value));
  if (note) {editSelection = {startChar: note.startChar, endChar: note.endChar}; $('edit-selection').textContent = pitchName(note.pitch); syncEditor();}
});
$('edit-source-toggle').addEventListener('click', () => {const el = $('edit-source'); el.hidden = !el.hidden; $('edit-source-toggle').setAttribute('aria-expanded', String(!el.hidden)); if (!el.hidden) {el.value = editDraft.abc; el.focus();}});
$('edit-source').addEventListener('change', event => {
  try {editDraft = editHelpers.setDraftAbc(editDraft, event.target.value); editSelection = null; audio.pause(); renderScore(); syncEditor();}
  catch {event.target.value = editDraft.abc; toast('只支持修改音高，请保留节奏、声部与和弦标记');}
});
document.addEventListener('keydown', event => {
  if (!editing || editSaving || event.target.closest('input,textarea,select,dialog')) return;
  if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {event.preventDefault(); changePitch(event.key === 'ArrowUp' ? 1 : -1);}
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {event.preventDefault(); void saveEdits();}
});

function seek(seconds) {transport.seek(seconds); drawRoll(audio.currentTime || 0, true); tick();}

function contextFor(canvas, width, height) {
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
  }
  const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
  return ctx;
}

function drawWaveform(seconds) {
  const canvas = $('waveform'), width = canvas.clientWidth, height = 34;
  if (!width) return;
  const ctx = contextFor(canvas, width, height), values = state.data?.waveform || [];
  const x = seconds / (state.data?.duration || 30) * width;
  values.forEach((value, i) => {
    const left = i / values.length * width, barHeight = Math.max(2, Math.pow(value, .62) * 29);
    ctx.fillStyle = left <= x ? '#a0ad89' : '#d5dbc9';
    ctx.fillRect(left, (height - barHeight) / 2, Math.max(1, width / values.length - 1.6), barHeight);
  });
}

function drawRoll(seconds, follow = !audio.paused) {
  if ($('roll-scroll').hidden || !state.data) return;
  const canvas = $('piano-roll');
  const pane = $('roll-scroll'), height = 300;
  let layout = rollLayout(state.data.duration, pane.clientWidth - 44, pane.scrollLeft);
  $('roll-space').style.width = `${layout.total}px`;
  const cursor = 34 + seconds * layout.scale;
  if (follow && (cursor > pane.scrollLeft + layout.width - 35 || cursor < pane.scrollLeft)) {
    pane.scrollLeft = Math.max(0, cursor - layout.width * .3);
    layout = rollLayout(state.data.duration, pane.clientWidth - 44, pane.scrollLeft);
  }
  const width = layout.width;
  canvas.style.width = `${width}px`; canvas.style.height = `${height}px`;
  const ctx = contextFor(canvas, width, height);
  const low = state.pitchLow, high = state.pitchHigh;
  const rowHeight = (height - 22) / (high - low + 1), left = 34;
  const x = t => left + t * layout.scale - layout.left;
  const y = p => 17 + (high - p) * rowHeight;
  ctx.font = '8px Segoe UI'; ctx.textAlign = 'right';
  for (let p = low; p <= high; p++) {
    if ([1,3,6,8,10].includes(p % 12)) {ctx.fillStyle = '#f2f3ec'; ctx.fillRect(left, y(p), width - left, rowHeight);}
    if (p % 12 === 0) {ctx.fillStyle = '#adb5a0'; ctx.fillText(`C${Math.floor(p/12)-1}`, left - 7, y(p) + rowHeight);}
  }
  ctx.fillStyle = '#a7b29b'; ctx.textAlign = 'center';
  for (let t = Math.ceil(layout.start / 5) * 5; t <= layout.end; t += 5) {
    if (x(t) >= left + 16 && x(t) <= width - 16) ctx.fillText(formatTime(t), x(t), 8);
  }
  // Sustained notes can begin before the viewport. Keep them out of the
  // fixed pitch-label gutter instead of painting through C2/C3/... labels.
  ctx.save(); ctx.beginPath(); ctx.rect(left, 12, width - left, height - 12); ctx.clip();
  ctx.strokeStyle = '#e4e8dc'; ctx.lineWidth = 1;
  for (const measure of state.data.measures) {
    if (measure.start < layout.start || measure.start > layout.end) continue;
    ctx.beginPath(); ctx.moveTo(x(measure.start), 12); ctx.lineTo(x(measure.start), height); ctx.stroke();
  }
  for (const track of state.data.tracks) {
    const dim = state.mode === 'piano' && state.part !== 'mix' && state.part !== track.role;
    for (const note of track.notes) {
      if (note.end < layout.start || note.start > layout.end) continue;
      ctx.globalAlpha = dim ? .17 : note.start <= seconds && seconds < note.end ? 1 : .66;
      ctx.fillStyle = colors[track.role];
      ctx.fillRect(x(note.start), y(note.pitch) + .4, Math.max(2, x(note.end) - x(note.start) - .7), Math.max(2, rowHeight - .8));
    }
  }
  ctx.globalAlpha = 1; ctx.strokeStyle = '#6f845a'; ctx.beginPath(); ctx.moveTo(x(seconds), 11); ctx.lineTo(x(seconds), height); ctx.stroke();
  ctx.restore();
}

function tick() {
  cancelAnimationFrame(state.raf);
  const seconds = audio.currentTime || 0, duration = state.data?.duration || 30;
  $('elapsed').textContent = formatTime(seconds); $('seek').value = seconds;
  $('wave-cursor').style.left = `${Math.min(100, seconds / duration * 100)}%`;
  drawWaveform(seconds); drawRoll(seconds);
  const measure = measureAt(state.data?.measures || [], seconds);
  if ((measure?.index ?? -1) !== state.measure) {
    $('score').querySelectorAll('.measure-playing').forEach(e => e.classList.remove('measure-playing'));
    state.measure = measure?.index ?? -1;
    if (measure) {
      const elements = [...$('score').querySelectorAll(`.abcjs-mm${measure.index}`)];
      elements.forEach(e => e.classList.add('measure-playing'));
      const system = elements.find(e => e.classList.contains('abcjs-note'))?.closest('svg');
      if (system && state.view === 'score' && !audio.paused) {
        const pane = $('score-scroll'), bounds = pane.getBoundingClientRect(), row = system.getBoundingClientRect();
        if (row.top < bounds.top || row.bottom > bounds.bottom) {
          pane.scrollTo({top: pane.scrollTop + row.top - bounds.top - 12,
            behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
        }
      }
    }
    $('measure-position').textContent = measure ? `${measure.index + 1} / ${state.data.measures.length}` : '';
  }
  if (!audio.paused && state.data) state.raf = requestAnimationFrame(tick);
}

$('play').addEventListener('click', togglePlay);
$('mode-original').addEventListener('click', () => switchMode('original'));
$('mode-piano').addEventListener('click', () => switchMode('piano'));
document.querySelectorAll('[data-part]').forEach(button => button.addEventListener('click', () => switchMode('piano', button.dataset.part)));
$('back').addEventListener('click', () => seek(audio.currentTime - 5));
$('next').addEventListener('click', () => seek(audio.currentTime + 5));
$('seek').addEventListener('input', event => seek(Number(event.target.value)));
$('roll-scroll').addEventListener('scroll', () => drawRoll(audio.currentTime || 0, false));
$('repeat').addEventListener('click', () => {audio.loop = !audio.loop; $('repeat').setAttribute('aria-pressed', String(audio.loop));});
$('speed').addEventListener('change', event => {audio.playbackRate = Number(event.target.value); audio.preservesPitch = true;});
$('volume').addEventListener('input', event => {audio.volume = Number(event.target.value);});
audio.volume = .7; audio.preservesPitch = true;
for (const type of ['play', 'pause', 'ended']) audio.addEventListener(type, () => {if (type === 'play') state.measure = -1; updateControls(); tick();});
audio.addEventListener('seeked', tick);
for (const view of ['score', 'roll']) $(`view-${view}`).addEventListener('click', () => {
  state.view = view;
  for (const name of ['score', 'roll']) {$(`view-${name}`).classList.toggle('active', name === view); $(`view-${name}`).setAttribute('aria-pressed', String(name === view));}
  renderScore(); tick();
});
$('export-button').addEventListener('click', () => {$('export-menu').hidden = !$('export-menu').hidden; $('export-button').setAttribute('aria-expanded', String(!$('export-menu').hidden));});
document.addEventListener('click', event => {if (!event.target.closest('.export-wrap')) {$('export-menu').hidden = true; $('export-button').setAttribute('aria-expanded', 'false');}});
document.addEventListener('keydown', event => {
  if (event.target.closest('input,textarea,select,dialog') || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.code === 'Space' && !event.target.closest('button,a')) {event.preventDefault(); void togglePlay();}
  if (event.code === 'ArrowLeft') {event.preventDefault(); seek(audio.currentTime - 5);}
  if (event.code === 'ArrowRight') {event.preventDefault(); seek(audio.currentTime + 5);}
});
$('import-button').addEventListener('click', () => $('import-dialog').showModal());
$('close-import').addEventListener('click', () => $('import-dialog').close());
$('file-input').addEventListener('change', event => {$('file-label').textContent = event.target.files[0]?.name || '选择音频';});
$('clip-length').addEventListener('change', event => {
  $('offset').disabled = event.target.value === 'full';
  if ($('offset').disabled) $('offset').value = 0;
});
$('import-form').addEventListener('submit', async event => {
  event.preventDefault();
  const file = $('file-input').files[0]; if (!file) return;
  if (file.size > 64 * 1024 * 1024) {toast('请选择 64 MB 以内的音频'); return;}
  $('submit-import').disabled = true;
  try {
    const query = new URLSearchParams({name: file.name, offset: $('offset').value, seconds: $('clip-length').value});
    const info = await getJSON(`/api/import?${query}`, {method: 'POST',
      headers: {'Content-Type': 'application/octet-stream', 'X-Score-Lab-Token': state.token}, body: file});
    $('import-dialog').close(); $('import-form').reset(); $('offset').disabled = false; $('file-label').textContent = '选择音频';
    const catalog = await getJSON('/api/catalog'); state.catalog = catalog.samples;
    if (embedded) {window.parent.postMessage({type: 'usic-score:queued'}, location.origin); return;}
    await selectSample(info.id);
  } catch (error) {toast(error.message);} finally {$('submit-import').disabled = false;}
});
window.addEventListener('resize', () => {clearTimeout(resizeTimer); resizeTimer = setTimeout(() => {renderScore(); tick();}, 160);});
window.addEventListener('pagehide', () => {transport.stop(); cancelAnimationFrame(state.raf); clearTimeout(pollTimer); clearTimeout(editTimer); ++state.version;});
if (embedded) {
  $('back-to-scores').hidden = false;
  $('back-to-scores').addEventListener('click', () => {transport.stop(); window.parent.postMessage({type: 'usic-score:home'}, location.origin);});
  audio.addEventListener('play', () => window.parent.postMessage({type: 'usic-score:play'}, location.origin));
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== window.parent) return;
    if (event.data?.type === 'usic-score:pause') transport.stop();
    if (event.data?.type === 'usic-score:import') $('import-dialog').showModal();
  });
}

try {
  await editorReady;
  const catalog = await getJSON('/api/catalog'); state.catalog = catalog.samples; state.token = catalog.token;
  $('import-button').disabled = !catalog.runtimeReady;
  if (!catalog.runtimeReady) $('import-button').title = '请先配置本机模型环境';
  renderLibrary();
  const requested = new URLSearchParams(location.search).get('sample');
  if (requested && catalog.samples.some(s => s.id === requested)) await selectSample(requested);
  else if (catalog.samples.length) await selectSample(catalog.samples.find(s => s.status.state === 'completed')?.id || catalog.samples[0].id);
  else {setEmpty('导入一首歌'); updateControls();}
} catch {setEmpty('本机服务未连接');}
