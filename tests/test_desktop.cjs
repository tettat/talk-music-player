'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {loadConfig} = require('../desktop/config.cjs');
const {validateReady} = require('../desktop/backend.cjs');
const {allowedURL, windowPreferences} = require('../desktop/security.cjs');
const {desktopLaunchOptions} = require('../desktop/launch.cjs');

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
test('renderer is isolated with no Node bridge, webview or unsafe content', () => {
  assert.deepEqual(windowPreferences(), {nodeIntegration: false, contextIsolation: true, sandbox: true,
    webSecurity: true, allowRunningInsecureContent: false, webviewTag: false});
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
