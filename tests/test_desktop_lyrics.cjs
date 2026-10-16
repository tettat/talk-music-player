'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {pathToFileURL} = require('node:url');
const {EventEmitter} = require('node:events');
const {createDesktopLyrics, sanitizeLyricFrame, fitBounds, LYRICS_COMMAND, LYRICS_UPDATE,
  LYRICS_STATE, LYRICS_SNAPSHOT, LYRICS_TRANSPORT, LOCK_SHORTCUT} = require('../desktop/lyrics-window.cjs');

const frame = {key: 'qq:0039MnYb0qxYhV', title: '合成歌曲', artist: '测试歌手', current: '合成歌词甲',
  next: '合成歌词乙', playing: true, canSkip: true, status: 'ready'};

function fixture(t, {shortcut = true} = {}) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'usic-lyrics-test-'));
  const windows = [], handlers = new Map(), shortcuts = new Map();
  class Window extends EventEmitter {
    constructor(options = {}) {
      super(); this.options = options; this.destroyed = false; this.shown = false; this.sent = [];
      this.bounds = {x: options.x || 0, y: options.y || 0, width: options.width || 900, height: options.height || 700};
      this.webContents = new EventEmitter();
      this.webContents.mainFrame = {url: 'http://127.0.0.1:43303/', origin: 'http://127.0.0.1:43303'};
      this.webContents.isDestroyed = () => this.destroyed;
      this.webContents.send = (...args) => this.sent.push(args);
      this.webContents.setBackgroundThrottling = value => {this.throttled = value;};
      this.webContents.setWindowOpenHandler = handler => {this.openHandler = handler;};
      this.webContents.session = {setPermissionRequestHandler: h => {this.permission = h;},
        setPermissionCheckHandler: h => {this.permissionCheck = h;},
        webRequest: {onBeforeRequest: h => {this.request = h;}}};
      windows.push(this);
    }
    isDestroyed() {return this.destroyed;}
    removeMenu() {}
    setAlwaysOnTop(value) {this.top = value;}
    setIgnoreMouseEvents(value) {this.clickThrough = value;}
    setFocusable(value) {this.focusable = value;}
    getBounds() {return {...this.bounds};}
    setBounds(bounds) {this.bounds = bounds; this.emit('move');}
    showInactive() {this.shown = true;}
    async loadFile(file) {this.webContents.mainFrame = {url: pathToFileURL(file).href, origin: 'file://'};}
    close() {this.emit('close'); this.destroy();}
    destroy() {if (this.destroyed) return; this.destroyed = true; this.shown = false; this.emit('closed');}
  }
  const main = new Window();
  const ipc = new EventEmitter();
  ipc.handle = (channel, handler) => handlers.set(channel, handler);
  ipc.removeHandler = channel => handlers.delete(channel);
  const screen = new EventEmitter();
  screen.displays = [{id: 1, workArea: {x: 0, y: 0, width: 1920, height: 1040}}];
  screen.getPrimaryDisplay = () => screen.displays[0];
  screen.getAllDisplays = () => screen.displays;
  const globalShortcut = {register: (key, handler) => {if (!shortcut) return false; shortcuts.set(key, handler); return true;},
    unregister: key => shortcuts.delete(key)};
  const controller = createDesktopLyrics({BrowserWindow: Window, ipcMain: ipc, screen, globalShortcut,
    mainWindow: main, origin: 'http://127.0.0.1:43303', profile});
  const event = window => ({sender: window.webContents, senderFrame: window.webContents.mainFrame});
  const command = (action, window = main) => handlers.get(LYRICS_COMMAND)(event(window), action);
  t.after(() => {
    controller.dispose();
    assert.equal(path.dirname(path.resolve(profile)), path.resolve(os.tmpdir()));
    assert.ok(path.basename(profile).startsWith('usic-lyrics-test-'));
    fs.rmSync(profile, {recursive: true, force: true});
  });
  return {main, ipc, controller, profile, screen, windows, shortcuts, handlers, event, command,
    overlay: () => windows.at(-1)};
}

test('lyric IPC accepts bounded text only and strips unneeded private properties', () => {
  assert.deepEqual(sanitizeLyricFrame({...frame, current: '\0合成歌词甲', audioUrl: 'synthetic', cookie: 'synthetic'}), frame);
  for (const value of [null, [], {...frame, key: 'https://example.invalid'}, {...frame, current: 'x'.repeat(513)},
    {...frame, title: {}}, {...frame, playing: 1}, {...frame, canSkip: null}, {...frame, status: 'execute'}]) {
    assert.equal(sanitizeLyricFrame(value), null);
  }
});

test('bounds support negative-coordinate monitors and recover after monitor removal', () => {
  const areas = [{x: 0, y: 0, width: 1920, height: 1040}, {x: -1280, y: -100, width: 1280, height: 1024}];
  const saved = {x: -1100, y: 600, width: 860, height: 210};
  assert.deepEqual(fitBounds(saved, areas), saved);
  const recovered = fitBounds(saved, areas.slice(0, 1));
  assert.ok(recovered.x >= 0 && recovered.y >= 0);
  assert.ok(recovered.x + recovered.width <= 1920 && recovered.y + recovered.height <= 1040);
  for (const value of [null, {x: NaN}, {x: Infinity}, {x: 0, y: 0, width: -100, height: 999999}]) {
    const bounds = fitBounds(value, areas);
    assert.ok(bounds.width >= 320 && bounds.width <= 1200);
    assert.ok(bounds.height >= 180 && bounds.height <= 280);
  }
  assert.equal(fitBounds(null, [{x: 0, y: 0, width: 300, height: 170}]).width, 300);
});

test('lyrics start disabled; toggle creates a secure transparent topmost window without stealing focus', async t => {
  const f = fixture(t);
  assert.equal(f.controller.state().visible, false); assert.equal(f.windows.length, 1);
  const state = await f.command('toggle'), overlay = f.overlay();
  assert.equal(state.visible, true); assert.equal(overlay.shown, true);
  for (const key of ['transparent', 'alwaysOnTop', 'skipTaskbar']) assert.equal(overlay.options[key], true);
  for (const key of ['frame', 'resizable', 'minimizable', 'maximizable', 'hasShadow']) assert.equal(overlay.options[key], false);
  assert.equal(overlay.options.webPreferences.nodeIntegration, false);
  assert.equal(overlay.options.webPreferences.sandbox, true);
  assert.equal(overlay.options.webPreferences.contextIsolation, true);
  assert.equal(overlay.options.webPreferences.webSecurity, true);
  assert.equal(f.main.throttled, false);
  assert.ok(f.main.sent.some(([channel]) => channel === LYRICS_STATE));
});

test('main and overlay have separate command allowlists and reject remote/child senders', async t => {
  const f = fixture(t); await f.command('toggle'); const overlay = f.overlay();
  const handle = f.handlers.get(LYRICS_COMMAND);
  for (const action of ['snapshot', 'font-up', 'close', 'next', 'readFile', null]) {
    await assert.rejects(f.command(action), /LYRICS_COMMAND_INVALID/);
  }
  for (const action of ['toggle', 'state', 'unlock', 'shell', {}]) {
    await assert.rejects(f.command(action, overlay), /LYRICS_COMMAND_INVALID/);
  }
  for (const sender of [{sender: {}, senderFrame: f.main.webContents.mainFrame},
    {sender: f.main.webContents, senderFrame: {...f.main.webContents.mainFrame}},
    {sender: overlay.webContents, senderFrame: null}]) {
    await assert.rejects(handle(sender, 'toggle'), /LYRICS_CONTROL_DENIED/);
  }
  overlay.webContents.mainFrame.url = 'https://example.invalid/';
  await assert.rejects(f.command('close', overlay), /LYRICS_CONTROL_DENIED/);
});

test('only the owning main renderer may publish display text, never the lyric window', async t => {
  const f = fixture(t); await f.command('toggle'); const overlay = f.overlay();
  f.ipc.emit(LYRICS_UPDATE, f.event(f.main), {...frame, secret: 'synthetic'});
  assert.deepEqual((await f.command('snapshot', overlay)).frame, frame);
  f.ipc.emit(LYRICS_UPDATE, f.event(overlay), {...frame, current: 'wrong sender'});
  f.ipc.emit(LYRICS_UPDATE, f.event(f.main), {...frame, current: 'x'.repeat(513)});
  assert.equal((await f.command('snapshot', overlay)).frame.current, frame.current);
  assert.ok(overlay.sent.some(([channel]) => channel === LYRICS_SNAPSHOT));
  f.main.webContents.emit('render-process-gone');
  assert.equal((await f.command('snapshot', overlay)).frame.status, 'idle');
});

test('overlay transport forwards one fixed action to original player, not a second audio engine', async t => {
  const f = fixture(t); await f.command('toggle'); const overlay = f.overlay();
  for (const action of ['previous', 'toggle-play', 'next']) await f.command(action, overlay);
  assert.deepEqual(f.main.sent.filter(([channel]) => channel === LYRICS_TRANSPORT).map(([, action]) => action),
    ['previous', 'toggle-play', 'next']);
  assert.equal(f.windows.length, 2);
});

test('locking is click-through with both shortcut and main-player recovery', async t => {
  const f = fixture(t); await f.command('toggle'); const overlay = f.overlay();
  assert.ok(f.shortcuts.has(LOCK_SHORTCUT));
  await f.command('toggle-lock', overlay);
  assert.equal(overlay.clickThrough, true); assert.equal(overlay.focusable, false);
  f.shortcuts.get(LOCK_SHORTCUT)();
  assert.equal(f.controller.state().locked, false); assert.equal(overlay.clickThrough, false);
  await f.command('toggle-lock', overlay); await f.command('unlock');
  assert.equal(overlay.focusable, true);
  await f.command('toggle-lock', overlay); await f.command('toggle'); await f.command('toggle');
  assert.equal(f.controller.state().locked, false); assert.equal(overlay.destroyed, true);
});

test('shortcut collision still allows unlock via main window', async t => {
  const f = fixture(t, {shortcut: false}); await f.command('toggle');
  assert.equal(f.controller.state().shortcutAvailable, false);
  await f.command('toggle-lock', f.overlay()); await f.command('unlock');
  assert.equal(f.controller.state().locked, false);
});

test('font and position persist locally, but never visible/locked state or account data', async t => {
  const f = fixture(t); await f.command('toggle'); const overlay = f.overlay();
  for (let n = 0; n < 10; n++) await f.command('font-up', overlay);
  assert.equal(f.controller.state().fontSize, 48);
  overlay.setBounds({x: 500, y: 600, width: 860, height: 230});
  await f.command('toggle');
  const pref = JSON.parse(fs.readFileSync(path.join(f.profile, 'desktop-lyrics.json'), 'utf8'));
  assert.deepEqual(Object.keys(pref).sort(), ['bounds', 'fontSize', 'version']);
  assert.equal(pref.fontSize, 48); assert.equal(pref.bounds.x, 500);
  await f.command('toggle'); assert.equal(f.overlay().getBounds().x, 500);
  for (let n = 0; n < 10; n++) await f.command('font-down', f.overlay());
  assert.equal(f.controller.state().fontSize, 24);
});

test('overlay blocks network, other files, new windows, permissions and navigation', async t => {
  const f = fixture(t); await f.command('toggle'); const overlay = f.overlay();
  for (const url of ['https://example.invalid/', 'http://127.0.0.1:43303/api/account',
    pathToFileURL(path.join(__dirname, '../config.local.json')).href, overlay.webContents.mainFrame.url + '?x']) {
    overlay.request({url}, result => assert.equal(result.cancel, true));
  }
  overlay.request({url: overlay.webContents.mainFrame.url}, result => assert.equal(result.cancel, false));
  assert.deepEqual(overlay.openHandler({url: 'https://example.invalid'}), {action: 'deny'});
  assert.equal(overlay.permissionCheck(), false);
  overlay.permission(null, 'camera', value => assert.equal(value, false));
  let prevented = false;
  overlay.webContents.emit('will-navigate', {preventDefault() {prevented = true;}}, 'file:///other');
  assert.equal(prevented, true);
});

test('closing lyric window leaves music owner alive; closing owner destroys overlay and cleans IPC/shortcut', async t => {
  const f = fixture(t); await f.command('toggle');
  f.overlay().close();
  assert.equal(f.main.destroyed, false); assert.equal(f.controller.state().visible, false);
  assert.equal(f.main.throttled, true); assert.equal(f.shortcuts.size, 0);
  await f.command('toggle'); const overlay = f.overlay();
  f.main.close(); f.controller.dispose();
  assert.equal(overlay.destroyed, true); assert.equal(f.handlers.size, 0); assert.equal(f.shortcuts.size, 0);
  assert.equal(f.ipc.listenerCount(LYRICS_UPDATE), 0);
  assert.equal(f.screen.listenerCount('display-removed'), 0);
});

test('owner close disposes before native renderer teardown and never sends IPC to a closing owner', async t => {
  const f = fixture(t); await f.command('toggle'); const overlay = f.overlay();
  const count = f.main.sent.length;
  // In real Electron, waiting for closed and touching a tearing-down renderer
  // can stall native close. Cleanup belongs at close, not after destruction.
  f.main.emit('close');
  assert.equal(overlay.destroyed, true); assert.equal(f.handlers.size, 0);
  assert.equal(f.main.sent.length, count); assert.equal(f.shortcuts.size, 0);
});

test('sandboxed lyric preloads expose fixed APIs without leaking the IPC event', async () => {
  for (const [file, name] of [['preload.cjs', 'usicLyrics'], ['lyrics-preload.cjs', 'usicLyricOverlay']]) {
    const exposed = new Map(), listeners = new Map(), calls = [], sends = [];
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../desktop', file), 'utf8'), {require: () => ({
      contextBridge: {exposeInMainWorld: (key, api) => exposed.set(key, api)},
      ipcRenderer: {invoke: async (...args) => calls.push(args), send: (...args) => sends.push(args),
        on: (key, fn) => listeners.set(key, fn), removeListener: key => listeners.delete(key)},
    })});
    const bridge = exposed.get(name); assert.equal(Object.isFrozen(bridge), true);
    assert.equal(bridge.invoke, undefined); assert.equal(bridge.send, undefined);
    if (name === 'usicLyrics') {
      await bridge.toggle('ignored'); await bridge.unlock(); await bridge.getState(); bridge.publish(frame);
      const received = []; const unsubscribe = bridge.onCommand((...args) => received.push(args));
      listeners.get(LYRICS_TRANSPORT)({secret: 'synthetic'}, 'toggle-play');
      listeners.get(LYRICS_TRANSPORT)({}, 'shell');
      assert.deepEqual(JSON.parse(JSON.stringify(received)), [['toggle-play']]); unsubscribe();
      assert.equal(sends[0][0], LYRICS_UPDATE);
    } else {
      await bridge.toggleLock('ignored'); await bridge.close(); await bridge.getSnapshot();
      const received = []; const unsubscribe = bridge.onSnapshot((...args) => received.push(args));
      listeners.get(LYRICS_SNAPSHOT)({secret: 'synthetic'}, {frame, fontSize: 32});
      assert.equal(received[0].length, 1); unsubscribe();
    }
    assert.ok(calls.every(call => call[0] === LYRICS_COMMAND));
  }
});
