'use strict';
const {app, BrowserWindow, dialog} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const {BackendController} = require('./backend.cjs');
const {loadConfig} = require('./config.cjs');
const {allowedURL, windowPreferences} = require('./security.cjs');

const root = path.resolve(__dirname, '..');
let mainWindow;
let backend;
let quitting = false;
let cleanupComplete = false;
let config;

try {
  config = loadConfig(root);
  const profile = path.join(config.data_dir, process.env.TALK_DESKTOP_QA === '1' ? 'desktop-profile-qa' : 'desktop-profile');
  fs.mkdirSync(profile, {recursive: true});
  app.setPath('userData', profile);
  app.setPath('sessionData', profile);
} catch {
  dialog.showErrorBox('talk', '本机配置无效，请检查 config.local.json。');
  app.exit(1);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus();}
  });
  app.on('window-all-closed', () => app.quit()); // No hidden tray or background playback.
  app.on('before-quit', event => {
    if (cleanupComplete || !backend) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    backend.stop().finally(() => {cleanupComplete = true; app.quit();});
  });
  app.whenReady().then(async () => {
    backend = new BackendController(root);
    backend.on('unexpected-exit', () => {
      if (!quitting && mainWindow) {
        dialog.showErrorBox('talk', '本地服务已退出，请重新打开播放器。');
        app.quit();
      }
    });
    const ready = await backend.start({qa: process.env.TALK_DESKTOP_QA === '1'});
    if (quitting) return;
    const origin = new URL(ready.url).origin;
    mainWindow = new BrowserWindow({width: 1380, height: 900, minWidth: 900, minHeight: 650,
      title: 'talk', backgroundColor: '#161714', show: false, autoHideMenuBar: true,
      webPreferences: windowPreferences()});
    mainWindow.removeMenu();
    const contents = mainWindow.webContents;
    contents.session.setPermissionRequestHandler((_web, _permission, callback) => callback(false));
    contents.session.setPermissionCheckHandler(() => false);
    contents.session.webRequest.onBeforeRequest((details, callback) => {
      const internal = /^(data:|blob:|about:)/.test(details.url);
      callback({cancel: !internal && !allowedURL(details.url, origin)});
    });
    contents.setWindowOpenHandler(() => ({action: 'deny'}));
    contents.on('will-navigate', (event, url) => {if (!allowedURL(url, origin)) event.preventDefault();});
    contents.on('will-attach-webview', event => event.preventDefault());
    mainWindow.once('ready-to-show', () => mainWindow.show());
    mainWindow.on('closed', () => {mainWindow = null;});
    await mainWindow.loadURL(ready.url);
  }).catch(() => {
    dialog.showErrorBox('talk', '启动失败。请检查本机配置与 Python 环境，详见 README。');
    app.quit();
  });
}
