'use strict';
// Isolated real Electron + real server/UI regression. Never touches production data or plays audible music.
const path = require('node:path');
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '..');
if (!process.versions.electron) {
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(require('electron'), [__filename, ...process.argv.slice(2)], {cwd: root, env,
    windowsHide: true, stdio: 'inherit'});
  child.once('error', () => {console.error('LYRICS_QA_LAUNCH_FAILED'); process.exitCode = 1;});
  child.once('exit', code => {process.exitCode = code ?? 1;});
} else {
  const {app, BrowserWindow, ipcMain, screen, globalShortcut} = require('electron');
  const assert = require('node:assert/strict');
  const {randomUUID} = require('node:crypto');
  const {loadConfig} = require('../desktop/config.cjs');
  const {windowPreferences, allowedURL} = require('../desktop/security.cjs');
  const {bindWindowControls} = require('../desktop/window-controls.cjs');
  const {createDesktopLyrics} = require('../desktop/lyrics-window.cjs');
  const privateDir = path.join(root, '.private', 'desktop-lyrics-qa-' + randomUUID());
  const output = path.join(root, 'output');
  fs.mkdirSync(privateDir, {recursive: true}); fs.mkdirSync(output, {recursive: true});
  fs.mkdirSync(path.join(output, 'playwright'), {recursive: true});
  app.setPath('userData', path.join(privateDir, 'profile'));
  app.setPath('sessionData', path.join(privateDir, 'profile'));
  app.setAppUserModelId('com.usic.lyrics-qa');
  app.on('window-all-closed', () => {}); // Finish assertions/owner-EOF checks before exiting QA.
  let backend, main, lyrics, overlay;
  const report = {checkedAt: new Date().toISOString(), electron: process.versions.electron,
    synthetic: true, audiblePlayback: false, productionTouched: false, checks: []};
  const trace = text => fs.appendFileSync(path.join(output, 'desktop-lyrics-qa.trace.log'), new Date().toISOString() + ' ' + text + '\n');
  trace('start');
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const js = code => main.webContents.executeJavaScript(code, true);
  const overlayJS = code => overlay.webContents.executeJavaScript(code, true);
  async function until(check, label) {
    for (let i = 0; i < 120; i++) {if (await check()) return; await sleep(100);}
    throw new Error('QA_TIMEOUT_' + label);
  }
  function createOverlay(options) {
    overlay = new BrowserWindow(options);
    // Exercise native creation, IPC and painting without showing an extra desktop window.
    overlay.showInactive = () => {};
    return overlay;
  }
  async function stopBackend() {
    if (!backend || backend.exitCode !== null) return;
    const ended = new Promise(resolve => backend.once('exit', resolve));
    backend.stdin.end('shutdown\n');
    await Promise.race([ended, sleep(8000).then(() => {if (backend.exitCode === null) backend.kill();})]);
  }
  const deadline = setTimeout(() => {
    report.error = 'QA_TIMEOUT';
    fs.writeFileSync(path.join(output, 'desktop-lyrics-qa.json'), JSON.stringify(report, null, 2));
    if (backend) backend.stdin.destroy();
    app.exit(1);
  }, process.argv.includes('--browser-hold') ? 180000 : 60000);
  app.whenReady().then(async () => {
    const config = loadConfig(root);
    backend = spawn(config.python, ['-I', '-B', path.join(root, 'tests/lyrics_fixture.py'), path.join(privateDir, 'library')],
      {cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'ignore']});
    const ready = await new Promise((resolve, reject) => {
      let buffer = '';
      backend.once('error', reject); backend.once('exit', () => reject(new Error('QA_BACKEND_EXIT')));
      backend.stdout.on('data', chunk => {
        buffer += chunk;
        if (buffer.includes('\n')) {try {resolve(JSON.parse(buffer.split('\n')[0]));} catch (error) {reject(error);}}
      });
    });
    report.backendPid = ready.pid; report.url = ready.url;
    const origin = new URL(ready.url).origin;
    main = new BrowserWindow({show: false, width: 1380, height: 900, frame: false,
      webPreferences: {...windowPreferences(), preload: path.join(root, 'desktop/preload.cjs')}});
    main.webContents.setAudioMuted(true);
    main.on('close', () => trace('main close event'));
    main.on('closed', () => trace('main closed event'));
    main.webContents.session.webRequest.onBeforeRequest((details, cb) => cb({cancel: !/^(data:|blob:|about:)/.test(details.url) && !allowedURL(details.url, origin)}));
    main.webContents.session.setPermissionRequestHandler((_web, _permission, cb) => cb(false));
    bindWindowControls(ipcMain, main, origin);
    lyrics = createDesktopLyrics({BrowserWindow: createOverlay, ipcMain, screen, globalShortcut, mainWindow: main,
      origin, profile: app.getPath('userData')});
    const consoleErrors = [];
    main.webContents.on('console-message', details => {if (details.level === 'error') consoleErrors.push(details.message);});
    await main.loadURL(ready.url);
    trace('main loaded');
    await until(() => js(`!document.getElementById('desktop-lyrics-toggle').hidden`), 'bridge');
    assert.equal(await js(`document.querySelectorAll('audio').length`), 1);
    await js(`document.getElementById('desktop-lyrics-toggle').click()`);
    await until(() => lyrics.state().visible && overlay && !overlay.isDestroyed() &&
      overlay.webContents.getURL().endsWith('/desktop/lyrics/index.html'), 'overlay');
    await until(() => overlayJS(`document.body.dataset.status === 'ready'`), 'qq-lyrics');
    assert.equal(await overlayJS(`document.querySelectorAll('audio').length`), 0);
    assert.equal(await overlayJS(`typeof require`), 'undefined');
    assert.equal(await overlayJS(`typeof process`), 'undefined');
    assert.equal(overlay.isAlwaysOnTop(), true);
    report.checks.push('real-file-preload-and-isolated-transparent-window', 'one-main-audio-zero-overlay-audio', 'qq-lyric-extraction');
    trace('lyrics loaded');
    await js(`document.getElementById('play-toggle').click()`);
    await until(() => js(`!document.getElementById('audio').paused && document.getElementById('audio').readyState >= 2`), 'play');
    await overlayJS(`document.getElementById('play-button').click()`);
    await until(() => js(`document.getElementById('audio').paused`), 'overlay-pause');
    await js(`document.getElementById('audio').currentTime = 9`);
    await until(() => overlayJS(`document.getElementById('current').textContent === '一朵小花正在等风'`), 'seek-forward');
    await js(`document.getElementById('audio').currentTime = 4`);
    await until(() => overlayJS(`document.getElementById('current').textContent === '把遥远的山色带给我'`), 'seek-back');
    await sleep(2300);
    overlay.webContents.sendInputEvent({type: 'mouseLeave', x: -1, y: -1});
    await sleep(250);
    const transparentImage = await overlay.webContents.capturePage();
    const pixels = transparentImage.toBitmap(), size = transparentImage.getSize();
    report.transparentCornerAlpha = pixels[3];
    report.transparentBottomAlpha = pixels[((size.height - 8) * size.width + Math.floor(size.width / 2)) * 4 + 3];
    report.idleToolbarOpacity = await overlayJS(`getComputedStyle(document.querySelector('.toolbar')).opacity`);
    fs.writeFileSync(path.join(output, 'playwright/desktop-lyrics-transparent.png'), transparentImage.toPNG());
    await overlayJS(`document.body.classList.add('is-peeking')`);
    fs.writeFileSync(path.join(output, 'playwright/desktop-lyrics-controls.png'), (await overlay.webContents.capturePage()).toPNG());
    fs.writeFileSync(path.join(output, 'playwright/desktop-lyrics-player.png'), (await main.webContents.capturePage()).toPNG());
    report.checks.push('overlay-pause-original-player', 'seek-forward-and-backward-sync');
    trace('sync and capture passed');
    await overlayJS(`document.getElementById('lock-button').click()`);
    await until(() => lyrics.state().locked, 'lock');
    await js(`document.getElementById('desktop-lyrics-toggle').dispatchEvent(new MouseEvent('contextmenu', {bubbles:true,cancelable:true}))`);
    await until(() => !lyrics.state().locked, 'unlock');
    await overlayJS(`document.getElementById('font-up').click()`);
    await until(() => lyrics.state().fontSize === 36, 'font');
    overlay.setPosition(100, 100);
    await overlayJS(`document.getElementById('next-button').click()`);
    await until(() => js(`document.getElementById('player-title').textContent === '山间来信'`), 'next');
    await until(() => overlayJS(`document.getElementById('song').textContent.includes('山间来信') && document.body.dataset.status === 'ready'`), 'netease-lyrics');
    await js(`document.getElementById('audio').pause(); document.getElementById('audio').currentTime = 9`);
    await until(() => overlayJS(`document.getElementById('current').textContent === '抬头又看见一片晴空'`), 'netease-seek');
    report.checks.push('lock-clickthrough-and-right-click-unlock', 'font-and-native-position', 'netease-switch-and-seek');
    trace('netease and controls passed');
    await overlayJS(`document.getElementById('next-button').click()`);
    await until(() => overlayJS(`document.body.dataset.status === 'unavailable'`), 'no-lyrics');
    await overlayJS(`document.getElementById('next-button').click()`);
    await until(() => overlayJS(`document.body.dataset.status === 'untimed' && document.getElementById('current').textContent === '静态歌词甲'`), 'plain-lyrics');
    await overlayJS(`document.getElementById('close-button').click()`);
    await until(() => !lyrics.state().visible && overlay.isDestroyed(), 'close-overlay');
    assert.equal(main.isDestroyed(), false);
    assert.equal(await js(`document.getElementById('desktop-lyrics-toggle').getAttribute('aria-pressed')`), 'false');
    await js(`document.getElementById('desktop-lyrics-toggle').click()`);
    await until(() => lyrics.state().visible && !overlay.isDestroyed() && overlay.webContents.getURL().endsWith('/desktop/lyrics/index.html'), 'reopen');
    await until(() => overlayJS(`document.body.dataset.status === 'untimed'`), 'reopen-loaded');
    assert.equal(lyrics.state().fontSize, 36); assert.equal(overlay.getBounds().x, 100);
    report.checks.push('no-lyrics-and-static-fallback', 'close-overlay-keeps-owner-alive', 'reopen-restores-local-preferences');
    trace('reopened');
    report.consoleErrors = consoleErrors;
    assert.deepEqual(consoleErrors, []);
    if (process.argv.includes('--browser-hold')) {
      fs.writeFileSync(path.join(output, 'desktop-lyrics-browser-ready.json'), JSON.stringify({url: ready.url, synthetic: true}));
      await sleep(90000);
    }
    trace('requesting owner close'); main.close(); trace('owner close returned');
    await until(() => main.isDestroyed() && overlay.isDestroyed(), 'owner-closed');
    assert.equal(globalShortcut.isRegistered('CommandOrControl+Alt+L'), false);
    report.checks.push('closing-owner-destroys-overlay-and-releases-shortcut');
    trace('stopping backend'); await stopBackend(); trace('backend stopped');
    report.backendStopped = true; report.verified = true;
  }).catch(error => {report.error = error.message; trace('error: ' + error.message);}).finally(async () => {
    clearTimeout(deadline);
    lyrics?.dispose(); if (main && !main.isDestroyed()) main.destroy();
    await stopBackend();
    fs.writeFileSync(path.join(output, 'desktop-lyrics-qa.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({verified: report.verified === true, checks: report.checks, error: report.error || null}));
    app.exit(report.verified ? 0 : 1);
  });
}
