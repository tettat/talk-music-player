'use strict';
// Only a saved sample/revision crosses IPC. Never accept HTML, URLs or paths.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const {trustedSender} = require('./window-controls.cjs');
const {windowPreferences} = require('./security.cjs');
const channel = 'usic:score-pdf';
const printOptions = Object.freeze({pageSize: 'A4', landscape: false, printBackground: false,
  preferCSSPageSize: true, displayHeaderFooter: true,
  margins: {top: 12 / 25.4, bottom: 15 / 25.4, left: 12 / 25.4, right: 12 / 25.4},
  headerTemplate: '<span></span>',
  footerTemplate: '<div style="width:100%;text-align:center;font-family:Arial;font-size:9px;color:#777"><span class="pageNumber"></span> / <span class="totalPages"></span></div>'});

function validateRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(k => !['sampleId', 'revisionId'].includes(k)) ||
      typeof value.sampleId !== 'string' || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value.sampleId) ||
      value.revisionId !== null && (typeof value.revisionId !== 'string' || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value.revisionId))) {
    throw new Error('SCORE_PDF_REQUEST_INVALID');
  }
  return {sampleId: value.sampleId, revisionId: value.revisionId};
}

function defaultFilename(title) {
  let name = String(title || '乐谱').replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, '-').replace(/[. ]+$/g, '').slice(0, 80);
  if (!name || /^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name)) name = '乐谱-' + name;
  return `${name}.pdf`;
}

function resourceAllowed(raw, origin, request) {
  try {
    const url = new URL(raw);
    if (url.origin !== origin || url.username || url.password) return false;
    const prefix = '/extensions/score-lab/';
    return ['print.html', 'print.mjs', 'print-layout.mjs', 'print.css', 'vendor/abcjs-basic-min.js']
      .some(name => url.pathname === prefix + name) ||
      url.pathname === `${prefix}api/samples/${request.sampleId}/notation`;
  } catch {return false;}
}

async function renderScorePDF({BrowserWindow, origin, request, onCreated = () => {}}) {
  request = validateRequest(request);
  if (new URL(origin).origin !== origin || !/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) throw new Error('SCORE_PDF_ORIGIN_INVALID');
  const query = new URLSearchParams({sample: request.sampleId});
  if (request.revisionId !== null) query.set('revision', request.revisionId);
  const url = `${origin}/extensions/score-lab/print.html?${query}`;
  const hidden = new BrowserWindow({show: false, width: 900, height: 1100,
    webPreferences: {...windowPreferences(), backgroundThrottling: false,
      partition: 'usic-score-pdf-' + crypto.randomUUID()}});
  onCreated(hidden);
  const contents = hidden.webContents;
  contents.session.setPermissionRequestHandler((_web, _permission, callback) => callback(false));
  contents.session.setPermissionCheckHandler(() => false);
  contents.session.webRequest.onBeforeRequest((details, callback) => callback({cancel: !resourceAllowed(details.url, origin, request)}));
  contents.setWindowOpenHandler(() => ({action: 'deny'}));
  contents.on('will-navigate', event => event.preventDefault());
  contents.on('will-frame-navigate', event => event.preventDefault());
  contents.on('will-attach-webview', event => event.preventDefault());
  let timer;
  try {
    return await Promise.race([(async () => {
      await hidden.loadURL(url);
      const ready = await contents.executeJavaScript('window.usicScorePrintReady');
      if (ready?.ready !== true || ready.revisionId !== request.revisionId) throw new Error('SCORE_PDF_RENDER_FAILED');
      const data = await contents.printToPDF(printOptions);
      if (!Buffer.isBuffer(data) || data.length < 100 || data.length > 32 * 1024 * 1024 ||
          data.subarray(0, 5).toString() !== '%PDF-') throw new Error('SCORE_PDF_OUTPUT_INVALID');
      return {data, title: ready.title, systems: ready.systems, pages: ready.pages, revisionId: ready.revisionId};
    })(), new Promise((_, reject) => {timer = setTimeout(() => reject(new Error('SCORE_PDF_TIMEOUT')), 60000);})]);
  } finally {
    clearTimeout(timer);
    if (!hidden.isDestroyed()) hidden.destroy();
  }
}

function bindScorePdf({ipcMain, BrowserWindow, dialog, window, origin}) {
  let busy = false, disposed = false, hidden;
  ipcMain.handle(channel, async (event, input) => {
    if (disposed || !trustedSender(event, window, origin)) throw new Error('SCORE_PDF_DENIED');
    if (busy) throw new Error('SCORE_PDF_BUSY');
    const request = validateRequest(input);
    busy = true;
    let pending;
    try {
      const result = await renderScorePDF({BrowserWindow, origin, request, onCreated: value => {hidden = value;}});
      if (disposed) return {cancelled: true};
      const selected = await dialog.showSaveDialog(window, {title: '导出乐谱 PDF',
        defaultPath: defaultFilename(result.title), filters: [{name: 'PDF 乐谱', extensions: ['pdf']}],
        properties: ['showOverwriteConfirmation', 'createDirectory']});
      if (disposed || selected.canceled || !selected.filePath) return {cancelled: true};
      if (!path.isAbsolute(selected.filePath) || selected.filePath.includes('\0') ||
          path.extname(selected.filePath).toLowerCase() !== '.pdf') throw new Error('SCORE_PDF_PATH_INVALID');
      pending = path.join(path.dirname(selected.filePath), '.usic-pdf-' + crypto.randomUUID() + '.tmp');
      await fs.writeFile(pending, result.data, {flag: 'wx', mode: 0o600});
      if (disposed) return {cancelled: true};
      await fs.rename(pending, selected.filePath);
      pending = null;
      return {saved: true}; // Never return a private filesystem path to the renderer.
    } finally {
      busy = false; hidden = null;
      if (pending) await fs.unlink(pending).catch(() => {});
    }
  });
  const dispose = () => {
    disposed = true;
    ipcMain.removeHandler(channel);
    if (hidden && !hidden.isDestroyed()) hidden.destroy();
  };
  window.once('closed', dispose);
  return dispose;
}

module.exports = {validateRequest, defaultFilename, resourceAllowed, printOptions, renderScorePDF, bindScorePdf};
