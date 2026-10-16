import test from 'node:test';
import assert from 'node:assert/strict';
import {buildTimeline, lyricFrame, fetchLyricDocument, installDesktopLyrics} from '../web/desktop-lyrics.mjs';

const qq = {key: 'qq:0039MnYb0qxYhV', title: '合成歌曲', artist: '测试歌手'};
const ne = {...qq, key: 'netease:186016'};
const document = (key = qq.key) => ({key, available: true, lines: [{time: null, text: '制作信息'},
  {time: 3, text: '合成歌词甲'}, {time: 8, text: '合成歌词乙'}, {time: 13, text: '合成歌词丙'}]});
const flush = () => new Promise(resolve => setImmediate(resolve));

test('binary timeline handles intro, exact boundaries, seeks backwards and end without guessed word timing', () => {
  const doc = document();
  for (const [position, current, next] of [[0, qq.title, '合成歌词甲'], [3, '合成歌词甲', '合成歌词乙'],
    [12, '合成歌词乙', '合成歌词丙'], [8, '合成歌词乙', '合成歌词丙'], [4, '合成歌词甲', '合成歌词乙'], [100, '合成歌词丙', '']]) {
    const frame = lyricFrame({track: qq, position, playing: false}, doc);
    assert.equal(frame.current, current); assert.equal(frame.next, next); assert.equal(frame.playing, false);
  }
});

test('same-time lines are combined, unsorted input normalized, null/invalid timestamps never sung', () => {
  const timeline = buildTimeline({lines: [{time: 8, text: '乙'}, {time: 3, text: '甲'}, {time: 3, text: '和声'},
    {time: null, text: '信息'}, {time: NaN, text: '坏时间'}, {time: -1, text: '坏时间'}, {time: 1, text: ''}]});
  assert.deepEqual(timeline.groups, [{time: 3, text: '甲 · 和声'}, {time: 8, text: '乙'}]);
});

test('idle, absent, error and plain lyrics are explicit rather than fabricated timing', () => {
  assert.equal(lyricFrame({}, null).status, 'idle');
  assert.equal(lyricFrame({track: qq, status: 'error'}, null).next, '歌词暂不可用');
  assert.equal(lyricFrame({track: qq}, {available: false, lines: []}).status, 'unavailable');
  const plain = {available: true, lines: [{time: null, text: '静态甲'}, {time: null, text: '静态乙'}]};
  assert.equal(lyricFrame({track: qq, position: 0}, plain).status, 'untimed');
  assert.equal(lyricFrame({track: qq, position: 100}, plain).current, '静态甲');
});

test('lyric payloads retain exact source key and bound Unicode/hostile text for textContent', () => {
  const doc = {available: true, lines: [{time: 0, text: '<img src=x onerror=alert(1)>' + '😀'.repeat(1000)}]};
  for (const track of [qq, ne]) {
    const frame = lyricFrame({track}, doc);
    assert.equal(frame.key, track.key); assert.ok(frame.current.length <= 500);
    assert.ok(!/[\uD800-\uDBFF]$/.test(frame.current));
  }
});

test('loader reuses cache or active server job and never starts duplicate extraction for loading response', async () => {
  const calls = [], job = 'a'.repeat(32); let polls = 0;
  const api = async path => {
    calls.push(path);
    if (path.startsWith('/api/lyrics/')) return {status: 'loading', job};
    return ++polls === 1 ? {status: 'loading', id: job} : {status: 'ready', lyrics: document()};
  };
  assert.deepEqual(await fetchLyricDocument(api, qq.key, {wait: async () => {}}), document());
  assert.equal(calls.length, 3); assert.equal(calls.includes('/api/lyrics'), false);
});

test('loader creates missing extraction once, handles BUSY, rejects identity/error and bounds polling', async () => {
  let posts = 0;
  const api = async path => {
    if (path.startsWith('/api/lyrics/')) return {status: 'missing'};
    if (++posts === 1) throw {code: 'BUSY'};
    return {status: 'ready', lyrics: document()};
  };
  assert.deepEqual(await fetchLyricDocument(api, qq.key, {wait: async () => {}}), document()); assert.equal(posts, 2);
  await assert.rejects(fetchLyricDocument(async () => ({status: 'ready', lyrics: document(ne.key)}), qq.key),
    error => error.code === 'LYRICS_INVALID');
  await assert.rejects(fetchLyricDocument(async path => path.startsWith('/api/lyrics/') ? {status: 'missing'} :
    {status: 'error', code: 'LOGIN_REQUIRED'}, qq.key), error => error.code === 'LOGIN_REQUIRED');
  let requests = 0;
  await assert.rejects(fetchLyricDocument(async () => {requests++; return {status: 'loading', job: 'b'.repeat(32)};}, qq.key,
    {wait: async () => {}}), error => error.code === 'NETWORK_TIMEOUT');
  assert.equal(requests, 181);
});

test('loader stops after user changes track or disables lyrics instead of publishing late data', async () => {
  let current = true, calls = 0;
  const result = await fetchLyricDocument(async () => {calls++; return {status: 'loading', job: 'a'.repeat(32)};}, qq.key,
    {isCurrent: () => current, wait: async () => {current = false;}});
  assert.equal(result, null); assert.equal(calls, 1);
});

function fixture({cached = true, api = async () => ({status: 'ready', lyrics: document(ne.key)})} = {}) {
  const events = new Map(), classes = new Set(), packets = [], commands = [], callbacks = {};
  const button = {hidden: true, disabled: false, title: '', attrs: {},
    classList: {toggle: (value, on) => on ? classes.add(value) : classes.delete(value)},
    setAttribute(name, value) {this.attrs[name] = value;}, addEventListener: (name, fn) => events.set(name, fn),
    removeEventListener: name => events.delete(name)};
  let playback = {track: qq, position: 4, playing: true, canSkip: true};
  let native = {visible: false, locked: false};
  const bridge = {getState: async () => native,
    toggle: async () => {native = {...native, visible: !native.visible}; return native;},
    unlock: async () => {native = {...native, locked: false}; return native;}, publish: frame => packets.push(frame),
    onState: fn => {callbacks.state = fn; return () => {delete callbacks.state;};},
    onCommand: fn => {callbacks.command = fn; return () => {delete callbacks.command;};}};
  const cache = new Map(cached ? [[qq.key, document()]] : []);
  const install = extra => installDesktopLyrics({bridge, doc: {getElementById: () => button}, getPlayback: () => playback,
    api, cache, actions: {'toggle-play': () => commands.push('toggle-play')}, ...extra});
  const controller = install();
  return {controller, bridge, button, events, packets, commands, callbacks, cache, install,
    setPlayback: value => {playback = {...playback, ...value};},
    state: value => {native = {...native, ...value}; callbacks.state(native);}};
}

test('renderer keeps ordinary web/mobile hidden without a desktop bridge', () => {
  const f = fixture(); f.controller.dispose();
  installDesktopLyrics({bridge: null, doc: {getElementById: () => f.button}});
  assert.equal(f.button.hidden, true);
});

test('desktop sync stays independent of detail tab, deduplicates packets and uses original transport callback', async () => {
  const f = fixture(); await flush();
  assert.equal(f.button.hidden, false); assert.equal(f.packets.length, 0);
  f.state({visible: true}); assert.equal(f.packets.at(-1).current, '合成歌词甲');
  f.controller.update(); assert.equal(f.packets.length, 1);
  f.setPlayback({position: 8, playing: false}); f.controller.update();
  assert.equal(f.packets.at(-1).current, '合成歌词乙'); assert.equal(f.packets.at(-1).playing, false);
  f.callbacks.command('toggle-play'); await flush(); assert.deepEqual(f.commands, ['toggle-play']);
  f.controller.dispose(); assert.equal(f.events.size, 0); assert.equal(f.button.hidden, true);
});

test('rapid source changes discard stale lyric response and refreshed lyrics immediately update desktop', async () => {
  let resolve;
  const f = fixture({cached: false, api: async path => path.includes(encodeURIComponent(qq.key))
    ? new Promise(r => {resolve = r;}) : {status: 'ready', lyrics: document(ne.key)}});
  await flush(); f.state({visible: true});
  f.setPlayback({track: ne, position: 9}); f.controller.update(); await flush();
  assert.equal(f.packets.at(-1).key, ne.key); assert.equal(f.packets.at(-1).current, '合成歌词乙');
  resolve({status: 'ready', lyrics: document()}); await flush();
  assert.equal(f.packets.at(-1).key, ne.key); assert.equal(f.cache.has(qq.key), false);
  f.controller.lyricsChanged(ne.key, {key: ne.key, available: true, lines: [{time: 0, text: '刷新后歌词'}]});
  assert.equal(f.packets.at(-1).current, '刷新后歌词'); f.controller.dispose();
});

test('unlock context action is available even if global shortcut registration failed', async () => {
  const f = fixture(); await flush(); f.state({visible: true, locked: true, shortcutAvailable: false});
  let prevented = false; f.events.get('contextmenu')({preventDefault() {prevented = true;}}); await flush();
  assert.equal(prevented, true); assert.equal(f.button.attrs['aria-pressed'], 'true');
  assert.equal(f.button.title, '桌面歌词'); f.controller.dispose();
});

test('a slow source download gets priority; lyrics start once playback finishes loading without duplicate requests', async () => {
  let calls = 0;
  const f = fixture({cached: false, api: async () => {calls++; return {status: 'ready', lyrics: document()};}});
  await flush(); f.setPlayback({loading: true}); f.state({visible: true});
  for (let i = 0; i < 20; i++) f.controller.update();
  assert.equal(calls, 0);
  f.setPlayback({loading: false}); f.controller.update(); f.controller.update(); await flush();
  assert.equal(calls, 1); assert.equal(f.packets.at(-1).current, '合成歌词甲'); f.controller.dispose();
});
