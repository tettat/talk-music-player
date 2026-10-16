'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {windowPreferences} = require('./security.cjs');
const {trustedSender} = require('./window-controls.cjs');

const LYRICS_COMMAND = 'usic:lyrics-command';
const LYRICS_UPDATE = 'usic:lyrics-update';
const LYRICS_STATE = 'usic:lyrics-state';
const LYRICS_SNAPSHOT = 'usic:lyrics-snapshot';
const LYRICS_TRANSPORT = 'usic:lyrics-transport';
const LOCK_SHORTCUT = 'CommandOrControl+Alt+L';
const statuses = new Set(['idle', 'loading', 'ready', 'untimed', 'unavailable', 'error']);
const emptyFrame = () => ({key: null, title: '', artist: '', current: '♪', next: '',
  playing: false, canSkip: false, status: 'idle'});

function sanitizeLyricFrame(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      !(value.key === null || typeof value.key === 'string' &&
        /^(?:qq:[A-Za-z0-9]{10,32}|netease:[1-9][0-9]{0,15}|[A-Za-z0-9]{10,32})$/.test(value.key)) ||
      typeof value.playing !== 'boolean' || typeof value.canSkip !== 'boolean' || !statuses.has(value.status)) return null;
  const result = {key: value.key, playing: value.playing, canSkip: value.canSkip, status: value.status};
  for (const [field, limit] of [['title', 160], ['artist', 160], ['current', 512], ['next', 512]]) {
    if (typeof value[field] !== 'string' || value[field].length > limit) return null;
    result[field] = value[field].replace(/[\u0000-\u001f\u007f]/g, '');
  }
  return result; // Never forward URLs, cookies, arbitrary properties or IPC events.
}

function fitBounds(saved, areas) {
  const primary = areas[0];
  const valid = saved && ['x', 'y', 'width', 'height'].every(k => Number.isFinite(saved[k]) && Math.abs(saved[k]) < 1e6);
  const target = valid ? {...saved} : {x: primary.x + (primary.width - Math.min(860, primary.width - 32)) / 2,
    y: primary.y + primary.height - 258, width: 860, height: 210};
  const overlap = a => Math.max(0, Math.min(target.x + target.width, a.x + a.width) - Math.max(target.x, a.x)) *
    Math.max(0, Math.min(target.y + target.height, a.y + a.height) - Math.max(target.y, a.y));
  const area = areas.reduce((best, a) => overlap(a) > overlap(best) ? a : best, primary);
  const width = Math.max(1, Math.min(area.width, Math.max(320, Math.min(1200, target.width))));
  const height = Math.max(1, Math.min(area.height, Math.max(180, Math.min(280, target.height))));
  return {x: Math.round(Math.max(area.x, Math.min(area.x + area.width - width, target.x))),
    y: Math.round(Math.max(area.y, Math.min(area.y + area.height - height, target.y))),
    width: Math.round(width), height: Math.round(height)};
}

function createDesktopLyrics({BrowserWindow, ipcMain, screen, globalShortcut, mainWindow, origin, profile}) {
  const html = path.join(__dirname, 'lyrics', 'index.html');
  const pageURL = pathToFileURL(html).href;
  const allowedFiles = new Set(['index.html', 'overlay.css', 'overlay.js'].map(name =>
    pathToFileURL(path.join(__dirname, 'lyrics', name)).href));
  const prefFile = path.join(profile, 'desktop-lyrics.json');
  let prefs = {};
  try {
    if (fs.statSync(prefFile).size <= 4096) prefs = JSON.parse(fs.readFileSync(prefFile, 'utf8'));
  } catch { /* Corrupt or absent UI preferences must not prevent playback. */ }
  let fontSize = [24, 28, 32, 36, 40, 44, 48].includes(prefs?.fontSize) ? prefs.fontSize : 32;
  let bounds = prefs?.bounds;
  let overlay = null, visible = false, locked = false, shortcutAvailable = false, disposed = false;
  let frame = emptyFrame(), saveTimer;
  const state = () => ({visible, locked, fontSize, shortcutAvailable});
  const snapshot = () => ({...state(), frame: {...frame}});
  const alive = window => window && !window.isDestroyed() && !window.webContents.isDestroyed();
  const save = () => {
    clearTimeout(saveTimer);
    if (alive(overlay)) bounds = overlay.getBounds();
    try {
      fs.writeFileSync(prefFile + '.tmp', JSON.stringify({version: 1, fontSize, bounds}));
      fs.renameSync(prefFile + '.tmp', prefFile);
    } catch { /* Read-only/full disks must not affect music or closing the app. */ }
  };
  const publish = () => {
    if (!disposed && alive(mainWindow)) mainWindow.webContents.send(LYRICS_STATE, state());
    if (alive(overlay)) overlay.webContents.send(LYRICS_SNAPSHOT, snapshot());
  };
  const setLocked = value => {
    locked = visible && value;
    if (alive(overlay)) {
      overlay.setIgnoreMouseEvents(locked, {forward: true});
      overlay.setFocusable(!locked);
    }
    publish();
  };
  const releaseShortcut = () => {
    if (shortcutAvailable) globalShortcut.unregister(LOCK_SHORTCUT);
    shortcutAvailable = false;
  };
  const hide = () => {
    visible = false; locked = false;
    releaseShortcut();
    if (!disposed && alive(mainWindow)) mainWindow.webContents.setBackgroundThrottling(true);
    if (alive(overlay)) {save(); overlay.destroy();}
    overlay = null;
    publish();
  };
  const workAreas = () => {
    const primary = screen.getPrimaryDisplay();
    return [primary, ...screen.getAllDisplays().filter(d => d.id !== primary.id)].map(d => d.workArea);
  };
  const show = async () => {
    visible = true; locked = false;
    try {shortcutAvailable = globalShortcut.register(LOCK_SHORTCUT, () => setLocked(!locked));}
    catch {shortcutAvailable = false;}
    let owned;
    try {owned = new BrowserWindow({...fitBounds(bounds, workAreas()), title: '小黄花 · 桌面歌词',
      frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
      resizable: false, maximizable: false, minimizable: false, fullscreenable: false,
      alwaysOnTop: true, skipTaskbar: true, show: false, autoHideMenuBar: true,
      webPreferences: {...windowPreferences(), backgroundThrottling: false, partition: 'usic-desktop-lyrics',
        preload: path.join(__dirname, 'lyrics-preload.cjs')}});}
    catch {hide(); throw new Error('LYRICS_WINDOW_FAILED');}
    overlay = owned;
    owned.removeMenu();
    owned.setAlwaysOnTop(true, 'floating');
    const contents = owned.webContents;
    contents.session.setPermissionRequestHandler((_web, _permission, cb) => cb(false));
    contents.session.setPermissionCheckHandler(() => false);
    contents.session.webRequest.onBeforeRequest((details, cb) => cb({cancel: !allowedFiles.has(details.url)}));
    contents.setWindowOpenHandler(() => ({action: 'deny'}));
    contents.on('will-navigate', event => event.preventDefault());
    contents.on('will-attach-webview', event => event.preventDefault());
    owned.on('move', () => {clearTimeout(saveTimer); saveTimer = setTimeout(save, 350); saveTimer.unref();});
    owned.on('closed', () => {
      if (overlay !== owned) return;
      overlay = null; visible = false; locked = false;
      releaseShortcut();
      if (!disposed && alive(mainWindow)) mainWindow.webContents.setBackgroundThrottling(true);
      publish();
    });
    owned.on('close', save);
    mainWindow.webContents.setBackgroundThrottling(false);
    publish();
    try {
      await owned.loadFile(html);
      if (!disposed && visible && overlay === owned && alive(owned)) {publish(); owned.showInactive();}
    } catch {
      if (disposed || overlay !== owned || !visible) return state(); // Owner closed during page load.
      if (overlay === owned) hide();
      throw new Error('LYRICS_WINDOW_FAILED');
    }
    return state();
  };
  const overlaySender = event => alive(overlay) && event.sender === overlay.webContents &&
    event.senderFrame === overlay.webContents.mainFrame && event.senderFrame?.url === pageURL;
  const handle = async (event, command) => {
    if (disposed) throw new Error('LYRICS_CONTROL_DENIED');
    if (trustedSender(event, mainWindow, origin)) {
      if (command === 'state') return state();
      if (command === 'toggle') {if (visible) {hide(); return state();} return show();}
      if (command === 'unlock') {setLocked(false); return state();}
      throw new Error('LYRICS_COMMAND_INVALID');
    }
    if (!overlaySender(event)) throw new Error('LYRICS_CONTROL_DENIED');
    if (command === 'snapshot') return snapshot();
    if (command === 'close') {hide(); return state();}
    if (command === 'toggle-lock') {setLocked(!locked); return state();}
    if (command === 'font-up' || command === 'font-down') {
      fontSize = Math.max(24, Math.min(48, fontSize + (command === 'font-up' ? 4 : -4)));
      if (alive(overlay)) overlay.setBounds(fitBounds({...overlay.getBounds(), height: Math.max(210, 110 + fontSize * 2.5)}, workAreas()));
      save(); publish(); return state();
    }
    if (['previous', 'toggle-play', 'next'].includes(command)) {
      if (alive(mainWindow)) mainWindow.webContents.send(LYRICS_TRANSPORT, command);
      return state();
    }
    throw new Error('LYRICS_COMMAND_INVALID');
  };
  const update = (event, value) => {
    if (disposed || !trustedSender(event, mainWindow, origin)) return;
    const clean = sanitizeLyricFrame(value);
    if (!clean) return;
    frame = clean;
    if (alive(overlay)) overlay.webContents.send(LYRICS_SNAPSHOT, snapshot());
  };
  const reset = () => {frame = emptyFrame(); if (alive(overlay)) overlay.webContents.send(LYRICS_SNAPSHOT, snapshot());};
  const navigation = (_event, _url, _inPlace, mainFrame) => {if (mainFrame) reset();};
  const displayChanged = () => {if (alive(overlay)) overlay.setBounds(fitBounds(overlay.getBounds(), workAreas()));};
  const dispose = () => {
    if (disposed) return;
    disposed = true; save(); hide(); clearTimeout(saveTimer);
    ipcMain.removeHandler(LYRICS_COMMAND); ipcMain.removeListener(LYRICS_UPDATE, update);
    mainWindow.webContents.removeListener('render-process-gone', reset);
    mainWindow.webContents.removeListener('did-start-navigation', navigation);
    mainWindow.removeListener('closed', dispose);
    mainWindow.removeListener('close', dispose);
    for (const name of ['display-removed', 'display-metrics-changed']) screen.removeListener(name, displayChanged);
  };
  ipcMain.handle(LYRICS_COMMAND, handle);
  ipcMain.on(LYRICS_UPDATE, update);
  mainWindow.webContents.on('render-process-gone', reset);
  mainWindow.webContents.on('did-start-navigation', navigation);
  mainWindow.once('closed', dispose);
  mainWindow.once('close', dispose);
  for (const name of ['display-removed', 'display-metrics-changed']) screen.on(name, displayChanged);
  return {dispose, state};
}

module.exports = {createDesktopLyrics, sanitizeLyricFrame, fitBounds, LYRICS_COMMAND, LYRICS_UPDATE,
  LYRICS_STATE, LYRICS_SNAPSHOT, LYRICS_TRANSPORT, LOCK_SHORTCUT};
