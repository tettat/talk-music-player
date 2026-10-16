'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');
const {contained, verifyBundle, bindScoreBundle} = require('../desktop/score-bundle.cjs');

function fixture(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'Usic score bundle '));
  const expected = path.resolve(os.tmpdir());
  assert.ok(root.startsWith(expected + path.sep));
  return Promise.resolve().then(() => run(root)).finally(() => {
    // Fixed, freshly created temporary root; never a user-selected folder.
    assert.ok(fs.realpathSync(root).startsWith(fs.realpathSync(expected) + path.sep));
    fs.rmSync(root, {recursive: true, force: true});
  });
}

test('capability paths are relative and contained, including Windows drive/UNC escapes', () => fixture(root => {
  fs.mkdirSync(path.join(root, 'models')); fs.writeFileSync(path.join(root, 'models/a.txt'), 'fixture');
  assert.equal(contained(root, 'models/a.txt'), path.join(root, 'models/a.txt'));
  for (const value of ['../a', '/a', 'C:/a', 'C:a', '//server/a', 'models\\a', './a', 'models/../a',
    'models//a', 'a\0', '', null, {}, 'models/a.txt/']) assert.throws(() => contained(root, value));
}));

test('manifest hashes are actually checked before any selected code can run', () => fixture(async root => {
  for (const name of ['python', 'sheet', 'base', 'assets']) fs.mkdirSync(path.join(root, name));
  fs.writeFileSync(path.join(root, 'python/python.exe'), 'not executable fixture');
  const files = {'python/python.exe': '0'.repeat(64)};
  fs.writeFileSync(path.join(root, 'score-pack.json'), JSON.stringify({schema: 1, files,
    python: 'python/python.exe', sheetRepo: 'sheet', baseRepo: 'base', assetsDir: 'assets'}));
  await assert.rejects(verifyBundle(root, path.resolve(__dirname, '..')), /HASH_MISMATCH/);
  files['python/python.exe'] = crypto.createHash('sha256').update('not executable fixture').digest('hex');
  fs.writeFileSync(path.join(root, 'score-pack.json'), JSON.stringify({schema: 1, files,
    python: 'python/python.exe', sheetRepo: 'sheet', baseRepo: 'base', assetsDir: 'assets'}));
  await assert.rejects(verifyBundle(root, path.resolve(__dirname, '..')), /MODEL_MISMATCH/);
}));

test('native bundle chooser rejects another window, subframe and remote origin', async () => {
  const handlers = new Map(), listeners = new Map(); let opened = 0;
  const frame = {origin: 'http://127.0.0.1:43180', url: 'http://127.0.0.1:43180/'};
  const contents = {isDestroyed: () => false, mainFrame: frame};
  const window = {isDestroyed: () => false, webContents: contents, once: (name, fn) => listeners.set(name, fn)};
  bindScoreBundle({ipcMain: {handle: (name, fn) => handlers.set(name, fn), removeHandler: name => handlers.delete(name)},
    dialog: {showOpenDialog: async () => {opened++; return {canceled: true, filePaths: []};}}, window,
    origin: frame.origin, root: path.resolve(__dirname, '..'), dataDir: os.tmpdir()});
  const handler = handlers.get('usic:score-bundle');
  for (const event of [{sender: {}, senderFrame: frame}, {sender: contents, senderFrame: {...frame}},
    {sender: contents, senderFrame: {origin: 'https://evil.example', url: 'https://evil.example/'}}]) {
    await assert.rejects(handler(event), /DENIED/);
  }
  assert.equal(opened, 0);
  assert.deepEqual(await handler({sender: contents, senderFrame: frame}), {cancelled: true});
  assert.equal(opened, 1); listeners.get('closed')(); assert.equal(handlers.size, 0);
});

test('renderer API cannot pass arbitrary paths or generic IPC to native code', async () => {
  const calls = [], values = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../desktop/preload.cjs'), 'utf8'), {
    require: () => ({contextBridge: {exposeInMainWorld: (name, value) => {values[name] = value;}},
      ipcRenderer: {invoke: async (...args) => calls.push(args), on() {}, send() {}, removeListener() {}}}),
  });
  assert.deepEqual(Object.keys(values.usicCapabilities), ['chooseScoreBundle']);
  await values.usicCapabilities.chooseScoreBundle('C:/anything', 'shell');
  assert.deepEqual(calls, [['usic:score-bundle']]);
});
