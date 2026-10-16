'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const {validateRequest, defaultFilename, resourceAllowed, printOptions, renderScorePDF, bindScorePdf} = require('../desktop/score-pdf.cjs');
const origin = 'http://127.0.0.1:43180';
const request = {sampleId: 'fixture', revisionId: null};

function fixture(options = {}) {
  const windows = [], handlers = new Map();
  class Hidden extends EventEmitter {
    constructor(settings) {
      super(); this.settings = settings; this.dead = false; windows.push(this);
      this.webContents = new EventEmitter();
      Object.assign(this.webContents, {
        session: {setPermissionRequestHandler(fn) {this.permissions = fn;}, setPermissionCheckHandler(fn) {this.check = fn;},
          webRequest: {onBeforeRequest(fn) {this.request = fn;}}},
        setWindowOpenHandler(fn) {this.open = fn;},
        executeJavaScript: async () => options.ready || {ready: true, title: '测试乐谱', systems: 2, revisionId: null},
        printToPDF: async value => {this.printed = value; return options.data || Buffer.from('%PDF-1.7\n' + 'fixture'.repeat(30));},
      });
    }
    async loadURL(url) {this.url = url; if (options.load) await options.load(this);}
    isDestroyed() {return this.dead;}
    destroy() {this.dead = true; this.emit('closed');}
  }
  const frame = {origin, url: origin + '/'};
  const contents = {mainFrame: frame, isDestroyed: () => false};
  const owner = new EventEmitter();
  Object.assign(owner, {webContents: contents, isDestroyed: () => false});
  const ipcMain = {handle: (name, fn) => handlers.set(name, fn), removeHandler: name => handlers.delete(name)};
  let selections = 0;
  const dialog = {showSaveDialog: async (_window, settings) => {selections++; return options.selected || {canceled: true};}};
  bindScorePdf({ipcMain, BrowserWindow: Hidden, dialog, window: owner, origin});
  return {Hidden, windows, handlers, owner, frame, contents, selections: () => selections,
    event: {sender: contents, senderFrame: frame}, handler: handlers.get('usic:score-pdf')};
}

test('PDF accepts only an existing saved sample/revision identity, never URLs, paths or HTML', () => {
  assert.deepEqual(validateRequest(request), request);
  assert.equal(validateRequest({...request, revisionId: 'qa-revision-1'}).revisionId, 'qa-revision-1');
  for (const bad of [null, [], {}, {...request, sampleId: '../secret'}, {...request, revisionId: '../revision'},
    {...request, revisionId: ''}, {...request, html: '<svg/>'}, {...request, url: origin}, {...request, path: 'a.pdf'}]) {
    assert.throws(() => validateRequest(bad), /REQUEST_INVALID/);
  }
});

test('default PDF filenames handle Unicode, path metacharacters and reserved Windows names', () => {
  assert.equal(defaultFilename('晴天'), '晴天.pdf');
  assert.equal(defaultFilename('../bad:score? '), '..-bad-score-.pdf');
  assert.equal(defaultFilename('CON'), '乐谱-CON.pdf');
  assert.doesNotMatch(defaultFilename('a\0b\\c'), /[\x00\\]/);
  assert.ok(defaultFilename('x'.repeat(200)).length <= 84);
});

test('print window can fetch only its own fixed notation assets and exact snapshot', () => {
  assert.ok(resourceAllowed(origin + '/extensions/score-lab/print.mjs', origin, request));
  assert.ok(resourceAllowed(origin + '/extensions/score-lab/api/samples/fixture/notation', origin, request));
  for (const url of ['https://example.com/', 'file:///private', origin + '/api/library',
    origin + '/extensions/score-lab/api/samples/other/notation', origin.replace('http://', 'http://u:p@') + '/extensions/score-lab/print.mjs']) {
    assert.equal(resourceAllowed(url, origin, request), false);
  }
});

test('PDF rendering uses an isolated, hidden, sandboxed vector print page and disposes it', async () => {
  const f = fixture();
  const result = await renderScorePDF({BrowserWindow: f.Hidden, origin, request});
  const hidden = f.windows[0];
  assert.ok(result.data.subarray(0, 5).equals(Buffer.from('%PDF-')));
  assert.equal(result.title, '测试乐谱'); assert.equal(hidden.dead, true);
  assert.equal(hidden.settings.show, false); assert.equal(hidden.settings.webPreferences.sandbox, true);
  assert.equal(hidden.settings.webPreferences.nodeIntegration, false);
  assert.ok(hidden.settings.webPreferences.partition.startsWith('usic-score-pdf-'));
  assert.equal(hidden.url, origin + '/extensions/score-lab/print.html?sample=fixture');
  assert.equal(hidden.printed.pageSize, 'A4'); assert.equal(printOptions.preferCSSPageSize, true);
  assert.doesNotMatch(printOptions.headerTemplate + printOptions.footerTemplate, /class=["'](?:url|date)["']/);
  assert.equal(hidden.webContents.open().action, 'deny');
});

test('failed or mismatched render never produces an empty/wrong PDF and destroys its window', async () => {
  for (const options of [{ready: {ready: false}}, {ready: {ready: true, revisionId: 'other'}},
    {data: Buffer.from('not a PDF')}]) {
    const f = fixture(options);
    await assert.rejects(renderScorePDF({BrowserWindow: f.Hidden, origin, request}), /RENDER_FAILED|OUTPUT_INVALID/);
    assert.equal(f.windows[0].dead, true);
  }
});

test('PDF IPC rejects subframes, foreign senders and remote frames before opening anything', async () => {
  const f = fixture();
  for (const event of [{sender: {}, senderFrame: f.frame}, {sender: f.contents, senderFrame: {...f.frame}},
    {sender: f.contents, senderFrame: {origin: 'https://evil.example', url: 'https://evil.example/'}}]) {
    await assert.rejects(f.handler(event, request), /DENIED/);
  }
  assert.equal(f.windows.length, 0); assert.equal(f.selections(), 0);
});

test('cancelling the native save dialog does not create a file and allows a later export', async () => {
  const f = fixture();
  assert.deepEqual(await f.handler(f.event, request), {cancelled: true});
  assert.deepEqual(await f.handler(f.event, request), {cancelled: true});
  assert.equal(f.selections(), 2); assert.ok(f.windows.every(w => w.dead));
});

test('native save writes only a confirmed PDF, atomically, without returning private paths', async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'Usic PDF test '));
  try {
    const file = path.join(temp, '测试乐谱.pdf');
    const f = fixture({selected: {canceled: false, filePath: file}});
    assert.deepEqual(await f.handler(f.event, request), {saved: true});
    assert.equal(fs.readFileSync(file).subarray(0, 5).toString(), '%PDF-');
    assert.deepEqual(fs.readdirSync(temp), ['测试乐谱.pdf']);
    const bad = fixture({selected: {canceled: false, filePath: path.join(temp, 'not-pdf.exe')}});
    await assert.rejects(bad.handler(bad.event, request), /PATH_INVALID/);
    assert.equal(fs.existsSync(path.join(temp, 'not-pdf.exe')), false);
  } finally {
    assert.ok(fs.realpathSync(temp).startsWith(fs.realpathSync(os.tmpdir()) + path.sep));
    fs.rmSync(temp, {recursive: true, force: true});
  }
});

test('only one PDF job is admitted; owner shutdown destroys its own hidden window', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  const f = fixture({load: () => gate});
  const first = f.handler(f.event, request);
  await assert.rejects(f.handler(f.event, request), /BUSY/);
  f.owner.emit('closed'); assert.equal(f.windows[0].dead, true); assert.equal(f.handlers.size, 0);
  release(); assert.deepEqual(await first, {cancelled: true}); assert.equal(f.selections(), 0);
});

test('sandboxed PDF bridge is fixed, strips extra arguments and exposes no filesystem primitive', async () => {
  const values = {}, calls = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../desktop/preload.cjs'), 'utf8'), {
    require: () => ({contextBridge: {exposeInMainWorld: (name, value) => {values[name] = value;}},
      ipcRenderer: {invoke: async (...args) => calls.push(args), on() {}, send() {}, removeListener() {}}}),
  });
  assert.deepEqual(Object.keys(values.usicScorePdf), ['export']);
  await values.usicScorePdf.export('fixture', null, 'arbitrary path');
  assert.equal(calls[0][0], 'usic:score-pdf');
  assert.deepEqual({...calls[0][1]}, request);
});
