'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');
const {loadConfig} = require('../desktop/config.cjs');
const {validateReady} = require('../desktop/backend.cjs');
const {allowedURL, windowPreferences} = require('../desktop/security.cjs');
const {desktopLaunchOptions} = require('../desktop/launch.cjs');
const {WINDOW_COMMAND, WINDOW_STATE, bindWindowControls} = require('../desktop/window-controls.cjs');

test('Usic branding keeps package metadata consistent and includes real transparent icon assets', () => {
  const root = path.resolve(__dirname, '..');
  const pkg = require('../package.json'), lock = require('../package-lock.json');
  assert.equal(pkg.name, 'usic'); assert.equal(pkg.productName, '小黄花 · Usic');
  assert.equal(lock.name, pkg.name); assert.equal(lock.packages[''].name, pkg.name);
  const png = fs.readFileSync(path.join(root, 'web/assets/usic.png'));
  assert.equal(png.subarray(1, 4).toString(), 'PNG');
  assert.equal(png.readUInt32BE(16), 256); assert.equal(png.readUInt32BE(20), 256);
  assert.equal(png[25], 6); // RGBA, not a flattened black/checkerboard background.
  const ico = fs.readFileSync(path.join(root, 'desktop/assets/usic.ico'));
  assert.equal(ico.readUInt16LE(0), 0); assert.equal(ico.readUInt16LE(2), 1);
  assert.equal(ico.readUInt16LE(4), 7);
  const expected = [16, 24, 32, 48, 64, 128, 256];
  let offset = 6 + expected.length * 16;
  for (const [index, size] of expected.entries()) {
    const entry = 6 + index * 16, bytes = ico.readUInt32LE(entry + 8);
    assert.equal(ico[entry] || 256, size); assert.equal(ico[entry + 1] || 256, size);
    assert.equal(ico.readUInt32LE(entry + 12), offset);
    const frame = ico.subarray(offset, offset + bytes);
    assert.equal(frame.subarray(1, 4).toString(), 'PNG');
    assert.equal(frame.readUInt32BE(16), size); assert.equal(frame.readUInt32BE(20), size);
    assert.equal(frame[25], 6); offset += bytes;
  }
  assert.equal(offset, ico.length);
});

test('desktop and web use Usic display branding without changing legacy data/config contracts', () => {
  const root = path.resolve(__dirname, '..');
  const main = fs.readFileSync(path.join(root, 'desktop/main.cjs'), 'utf8');
  const page = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  assert.match(main, /app\.setName\(displayName\)/);
  assert.match(main, /app\.setAppUserModelId\('com\.usic\.player'\)/);
  assert.match(main, /desktop\/assets\/usic\.ico/);
  assert.match(page, /<title>小黄花 · Usic<\/title>/);
  assert.match(page, /href="\/assets\/usic\.png"/);
  assert.match(page, /name="talk-csrf"/); // Existing renderer/API contract remains compatible.
});

function fixture(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'talk desktop '));
  try { run(root); } finally { fs.rmSync(root, {recursive: true, force: true}); }
}
test('clean-checkout paths are relative to checkout, independent of cwd', () => fixture(root => {
  const cfg = loadConfig(root, {});
  assert.equal(cfg.data_dir, path.join(root, '.private'));
  assert.equal(cfg.qq_session_dir, path.join(root, '.private/qq'));
  assert.equal(cfg.port, 43180);
}));
test('explicit machine paths and environment overrides stay out of source', () => fixture(root => {
  fs.writeFileSync(path.join(root, 'config.local.json'), JSON.stringify({data_dir: 'custom', port: 43300}));
  const cfg = loadConfig(root, {TALK_DATA_DIR: 'env data'});
  assert.equal(cfg.data_dir, path.join(root, 'env data')); assert.equal(cfg.port, 43300);
}));
test('BOM configuration supported on Windows', () => fixture(root => {
  fs.writeFileSync(path.join(root, 'config.local.json'), '\uFEFF{"port":43301}');
  assert.equal(loadConfig(root, {}).port, 43301);
}));
test('PowerShell 5 and 7 accept JSON integers but reject coerced configuration values',
  {skip: process.platform !== 'win32'}, () => {
    for (const shell of ['powershell.exe', 'pwsh.exe']) {
      const result = spawnSync(shell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', path.join(__dirname, 'test_runtime.ps1')], {encoding: 'utf8', windowsHide: true, timeout: 20000});
      if (shell === 'pwsh.exe' && result.error?.code === 'ENOENT') continue;
      assert.equal(result.error, undefined, shell + ' failed to run');
      assert.equal(result.status, 0, shell + ': ' + result.stderr);
      assert.match(result.stdout, /POWERSHELL_RUNTIME_CONFIG_OK/);
    }
  });
test('missing explicit config cannot silently use defaults', () => fixture(root => {
  assert.throws(() => loadConfig(root, {TALK_CONFIG_FILE: 'absent'}), /CONFIG_FILE_NOT_FOUND/);
}));
test('unknown keys, arrays and malformed values rejected', () => fixture(root => {
  for (const data of [{password: 'synthetic'}, [], null, {port: true}, {port: 0}, {ttl_seconds: '60'}, {python: ''}]) {
    fs.writeFileSync(path.join(root, 'config.local.json'), JSON.stringify(data));
    assert.throws(() => loadConfig(root, {}), /CONFIG_/);
  }
}));
test('ready channel accepts only an owned loopback backend', () => {
  const ready = {event: 'ready', port: 43302, pid: 123, url: 'http://127.0.0.1:43302/',
                 instance: 'a'.repeat(32), automaticOfficialClientLaunch: false};
  assert.equal(validateReady(ready), ready);
  for (const change of [{url: 'https://evil.invalid/'}, {port: 80}, {pid: -1}, {instance: 'bad'},
                        {automaticOfficialClientLaunch: true}]) {
    assert.throws(() => validateReady({...ready, ...change}), /BACKEND_READY_INVALID/);
  }
});
test('renderer remains sandboxed with no Node, webview or unsafe content', () => {
  assert.deepEqual(windowPreferences(), {nodeIntegration: false, contextIsolation: true, sandbox: true,
    webSecurity: true, allowRunningInsecureContent: false, webviewTag: false});
});

function controlFixture() {
  const window = new EventEmitter();
  const contents = new EventEmitter();
  const state = {maximized: false, minimized: false, focused: true};
  const calls = [], sent = [], handlers = new Map();
  let destroyed = false;
  contents.mainFrame = {url: 'http://127.0.0.1:43303/', origin: 'http://127.0.0.1:43303'};
  contents.isDestroyed = window.isDestroyed = () => destroyed;
  contents.send = (...args) => sent.push(args);
  window.webContents = contents;
  for (const [method, key] of [['isMaximized', 'maximized'], ['isMinimized', 'minimized'], ['isFocused', 'focused']]) {
    window[method] = () => state[key];
  }
  for (const [method, key, value, event] of [['minimize', 'minimized', true, 'minimize'],
    ['maximize', 'maximized', true, 'maximize'], ['unmaximize', 'maximized', false, 'unmaximize']]) {
    window[method] = () => {calls.push(method); state[key] = value; window.emit(event);};
  }
  window.close = () => {calls.push('close'); destroyed = true; window.emit('closed');};
  const ipcMain = {handle: (channel, handler) => handlers.set(channel, handler),
                   removeHandler: channel => handlers.delete(channel)};
  const dispose = bindWindowControls(ipcMain, window, 'http://127.0.0.1:43303');
  const event = {sender: contents, senderFrame: contents.mainFrame};
  return {window, contents, state, calls, sent, handlers, dispose, event,
    command: (command, sender = event) => handlers.get(WINDOW_COMMAND)(sender, command)};
}

test('window IPC accepts only the owning main frame, never remote frames or other contents', () => {
  const f = controlFixture();
  for (const event of [{...f.event, sender: {}}, {...f.event, senderFrame: {url: f.contents.mainFrame.url}},
                        {...f.event, senderFrame: null}]) {
    assert.throws(() => f.command('close', event), /WINDOW_CONTROL_DENIED/);
  }
  for (const url of ['https://evil.invalid/', 'http://127.0.0.1:43304/',
                     'http://user:pass@127.0.0.1:43303/']) {
    f.contents.mainFrame.url = url;
    assert.throws(() => f.command('close'), /WINDOW_CONTROL_DENIED/);
  }
  f.contents.mainFrame.url = 'http://127.0.0.1:43303/';
  f.contents.mainFrame.origin = 'null';
  assert.throws(() => f.command('close'), /WINDOW_CONTROL_DENIED/);
  assert.deepEqual(f.calls, []);
  f.dispose();
});

test('window actions are a fixed allowlist and maximize toggles back to restore', () => {
  const f = controlFixture();
  for (const command of ['shell', 'readFile', 'quit', null, {}, ['close']]) {
    assert.throws(() => f.command(command), /WINDOW_COMMAND_INVALID/);
  }
  assert.deepEqual(f.command('state'), {maximized: false, minimized: false, focused: true});
  assert.equal(f.command('minimize').minimized, true);
  assert.equal(f.command('toggle-maximize').maximized, true);
  assert.equal(f.command('toggle-maximize').maximized, false);
  assert.equal(f.command('close'), null);
  assert.deepEqual(f.calls, ['minimize', 'maximize', 'unmaximize', 'close']);
  assert.equal(f.handlers.size, 0);
});

test('OS window changes publish state and closed windows dispose IPC/listeners', () => {
  const f = controlFixture();
  f.window.maximize();
  assert.deepEqual(f.sent[0], [WINDOW_STATE, {maximized: true, minimized: false, focused: true}]);
  f.state.focused = false; f.window.emit('blur');
  assert.equal(f.sent[1][1].focused, false);
  f.window.close(); f.dispose(); f.dispose();
  assert.equal(f.handlers.size, 0);
  for (const event of ['maximize', 'unmaximize', 'minimize', 'restore', 'focus', 'blur', 'closed']) {
    assert.equal(f.window.listenerCount(event), 0);
  }
});

test('sandboxed preload exposes window-only methods, strips IPC events and supports unsubscribe', async () => {
  let exposed, capabilities, pdf;
  const invoked = [], listeners = new Map();
  const electron = {contextBridge: {exposeInMainWorld: (name, api) => {
    assert.ok(['talkWindow', 'usicLyrics', 'usicCapabilities', 'usicScorePdf'].includes(name));
    if (name === 'talkWindow') exposed = api;
    if (name === 'usicCapabilities') capabilities = api;
    if (name === 'usicScorePdf') pdf = api;
  }},
    ipcRenderer: {invoke: async (...args) => {invoked.push(args);},
                  on: (channel, listener) => listeners.set(channel, listener),
                  removeListener: (channel, listener) => {assert.equal(listeners.get(channel), listener); listeners.delete(channel);}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../desktop/preload.cjs'), 'utf8'),
    {require: name => {assert.equal(name, 'electron'); return electron;}});
  assert.equal(Object.isFrozen(exposed), true);
  assert.deepEqual(Object.keys(exposed).sort(), ['close', 'getState', 'minimize', 'onState', 'toggleMaximize']);
  assert.equal(Object.isFrozen(capabilities), true);
  assert.deepEqual(Object.keys(capabilities), ['chooseScoreBundle']);
  assert.equal(Object.isFrozen(pdf), true);
  assert.deepEqual(Object.keys(pdf), ['export']);
  await exposed.minimize('untrusted'); await exposed.toggleMaximize(); await exposed.close(); await exposed.getState();
  assert.deepEqual(invoked, ['minimize', 'toggle-maximize', 'close', 'state'].map(action => [WINDOW_COMMAND, action]));
  await capabilities.chooseScoreBundle('../untrusted-path');
  assert.deepEqual(invoked.at(-1), ['usic:score-bundle']);
  assert.throws(() => exposed.onState(null), /WINDOW_CALLBACK_INVALID/);
  const received = [];
  const unsubscribe = exposed.onState((...args) => received.push(args));
  listeners.get(WINDOW_STATE)({sender: 'never exposed'}, {maximized: true, minimized: false, focused: true, secret: 'synthetic'});
  listeners.get(WINDOW_STATE)({}, {maximized: 'bad'});
  assert.equal(received.length, 1);
  assert.equal(received[0].length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(received[0][0])), {maximized: true, minimized: false, focused: true});
  unsubscribe(); assert.equal(listeners.size, 0);
});

function rendererFixture() {
  const classList = () => {
    const classes = new Set();
    return {add: (...values) => values.forEach(value => classes.add(value)),
      remove: (...values) => values.forEach(value => classes.delete(value)), contains: value => classes.has(value),
      toggle: (value, enabled) => enabled ? classes.add(value) : classes.delete(value)};
  };
  const elements = {};
  for (const id of ['window-controls', 'window-minimize', 'window-maximize', 'window-close']) {
    const events = new Map();
    elements[id] = {hidden: true, disabled: false, classList: classList(), attributes: {},
      setAttribute(name, value) {this.attributes[name] = value;},
      addEventListener: (event, listener) => events.set(event, listener),
      removeEventListener: (event, listener) => {assert.equal(events.get(event), listener); events.delete(event);},
      click: () => events.get('click')(), events};
  }
  const doc = {documentElement: {classList: classList()}, getElementById: id => elements[id]};
  return {doc, elements};
}

test('browser and mobile renderer never show desktop buttons or alter layout', async () => {
  const {installWindowControls} = await import('../web/window-controls.mjs');
  const {doc, elements} = rendererFixture();
  const dispose = installWindowControls(undefined, doc);
  assert.equal(elements['window-controls'].hidden, true);
  assert.equal(doc.documentElement.classList.contains('is-desktop'), false);
  dispose();
});

test('custom controls route exact actions and keep maximize label/state synchronized', async () => {
  const {installWindowControls} = await import('../web/window-controls.mjs');
  const {doc, elements} = rendererFixture();
  const actions = []; let listener, removed = false;
  const state = {maximized: false, minimized: false, focused: true};
  const bridge = {getState: async () => ({...state}), onState: callback => {listener = callback; return () => {removed = true;};},
    minimize: async () => {actions.push('minimize'); return {...state, minimized: true};},
    toggleMaximize: async () => {actions.push('toggle-maximize'); state.maximized = !state.maximized; return {...state};},
    close: async () => {actions.push('close'); return null;}};
  const dispose = installWindowControls(bridge, doc);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements['window-controls'].hidden, false);
  assert.equal(doc.documentElement.classList.contains('is-desktop'), true);
  await elements['window-maximize'].click();
  assert.equal(elements['window-maximize'].attributes['aria-label'], '还原');
  assert.equal(elements['window-controls'].classList.contains('is-maximized'), true);
  listener({...state, maximized: false, focused: false});
  assert.equal(elements['window-maximize'].title, '最大化');
  assert.equal(doc.documentElement.classList.contains('is-window-blurred'), true);
  await elements['window-minimize'].click(); await elements['window-close'].click();
  assert.deepEqual(actions, ['toggle-maximize', 'minimize', 'close']);
  dispose();
  assert.equal(removed, true);
  assert.equal(elements['window-controls'].hidden, true);
  assert.equal(elements['window-close'].events.size, 0);
});

test('failed window IPC does not leave a button disabled', async () => {
  const {installWindowControls} = await import('../web/window-controls.mjs');
  const {doc, elements} = rendererFixture();
  const reject = () => Promise.reject(new Error('WINDOW_CONTROL_DENIED'));
  const dispose = installWindowControls({getState: reject, onState: () => () => {},
    minimize: reject, toggleMaximize: reject, close: reject}, doc);
  await elements['window-minimize'].click();
  assert.equal(elements['window-minimize'].disabled, false);
  dispose();
});

test('window controls stay interactive above modal dialogs and return home on close', async () => {
  const {keepControlsAboveDialogs} = await import('../web/window-controls.mjs');
  // Explicit hosts make reparenting observable without browser/global dependencies.
  const home = {}, first = {open: false}, second = {open: false};
  for (const node of [home, first, second]) node.appendChild = child => {child.parentElement = node;};
  const controls = {parentElement: home};
  let notify, disconnected = false; const observed = [];
  class Observer {
    constructor(callback) {notify = callback;}
    observe(target, options) {observed.push([target, options]);}
    disconnect() {disconnected = true;}
  }
  const doc = {defaultView: {MutationObserver: Observer}, querySelectorAll: () => [first, second]};
  const dispose = keepControlsAboveDialogs(controls, doc);
  assert.equal(observed.length, 2);
  assert.deepEqual(observed[0][1], {attributes: true, attributeFilter: ['open']});
  second.open = true; notify([{target: second}]);
  assert.equal(controls.parentElement, second);
  first.open = true; notify([{target: first}]); // Most recently opened, not DOM order.
  assert.equal(controls.parentElement, first);
  first.open = false; notify([{target: first}]);
  assert.equal(controls.parentElement, second);
  second.open = false; notify([{target: second}]);
  assert.equal(controls.parentElement, home);
  first.open = true; notify([{target: first}]); dispose();
  assert.equal(disconnected, true);
  assert.equal(controls.parentElement, home);
});

test('desktop modal CSS overrides the open-dialog transform animation for fixed controls', () => {
  const css = fs.readFileSync(path.join(__dirname, '../web/styles.css'), 'utf8');
  assert.match(css, /\.is-desktop\s+\.dialog\[open\]\s*\{\s*animation:\s*none\s*;/);
});
test('navigation permits same origin only, not credentials or remote schemes', () => {
  const origin = 'http://127.0.0.1:43303';
  assert.equal(allowedURL(origin + '/api/health', origin), true);
  for (const url of ['https://example.com', 'file:///private', 'javascript:alert(1)',
                     'http://127.0.0.1:43304/', 'http://user:pass@127.0.0.1:43303/']) {
    assert.equal(allowedURL(url, origin), false);
  }
});
test('interactive Electron launch is visible and never inherits run-as-Node', () => {
  const source = {ELECTRON_RUN_AS_NODE: '1', TALK_DATA_DIR: 'qa'};
  const options = desktopLaunchOptions(source);
  assert.equal(options.windowsHide, false);
  assert.equal(options.shell, false);
  assert.equal(options.env.ELECTRON_RUN_AS_NODE, undefined);
  assert.equal(options.env.TALK_DATA_DIR, 'qa');
  assert.equal(source.ELECTRON_RUN_AS_NODE, '1');
});
