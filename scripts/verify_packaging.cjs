'use strict';
// Verify a relocated real build, with disposable profiles. Never run the installer
// against HKCU, stop a production process, or contact a music provider.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {setTimeout: delay} = require('node:timers/promises');
const {getRuntimeLaunch, isWithin} = require('../desktop/packaging.cjs');
const {BackendController} = require('../desktop/backend.cjs');
const root = path.resolve(__dirname, '..');
// Never recursively remove an earlier QA tree (it may be retained for review).
const qa = path.join(root, 'output', 'packaging-qa-' + Date.now().toString(36));
const relocated = path.join(qa, '中文 安装 with spaces');
const reportFile = path.join(root, 'output/outsource/packaging-verification.json');
const report = {checkedAt: new Date().toISOString(), passed: false, noRealProfileInstall: true,
  productionProcessesUntouched: true, providerRequestsMade: false, backend: [], gui: []};
const windowsPowerShellEnv = {...process.env};
// Node does not perform pwsh's PSModulePath cleanup when launching Windows
// PowerShell 5.1; inherited PS7 Security modules cannot load in the legacy host.
for (const key of Object.keys(windowsPowerShellEnv)) if (key.toUpperCase() === 'PSMODULEPATH') delete windowsPowerShellEnv[key];

function sha(file) {return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');}
function inventory(directory) {
  const result = {};
  function walk(folder) {
    for (const item of fs.readdirSync(folder, {withFileTypes: true})) {
      const file = path.join(folder, item.name);
      assert.equal(item.isSymbolicLink(), false, 'No symlinks in installed resources');
      if (item.isDirectory()) walk(file); else result[path.relative(directory, file).replaceAll('\\', '/')] = sha(file);
    }
  }
  walk(directory); return result;
}
function installedPrivacyScan(directory) {
  let files = 0;
  const home = require('node:os').homedir();
  const needles = [home, home.replaceAll('\\', '/'), home.replaceAll('\\', '\\\\')]
    .flatMap(value => [Buffer.from(value.toLowerCase()), Buffer.from(value.toLowerCase(), 'utf16le')]);
  function walk(folder) {
    for (const item of fs.readdirSync(folder, {withFileTypes: true})) {
      const file = path.join(folder, item.name), relative = path.relative(directory, file).replaceAll('\\', '/');
      assert.doesNotMatch(relative, /(^|\/)(?:\.private|\.venv|__pycache__|cache|logs)(\/|$)|config\.local\.json|pyvenv\.cfg|\.(?:db|sqlite3?|dpapi|log|mp3|wav|onnx|pt|pth|safetensors|lnk)$/i);
      if (item.isDirectory()) walk(file);
      else {
        // Chunk + overlap avoids loading the ~250MB Electron binary several
        // times just to search for the actual build-machine profile string.
        const fd = fs.openSync(file, 'r'), chunk = Buffer.alloc(4 * 1024 * 1024);
        let overlap = Buffer.alloc(0), count;
        try {
          while ((count = fs.readSync(fd, chunk, 0, chunk.length, null)) > 0) {
            const data = Buffer.concat([overlap, chunk.subarray(0, count)]);
            // Actual profile is ASCII on this machine; preserve other binary bytes.
            for (let i = 0; i < data.length; i++) if (data[i] >= 65 && data[i] <= 90) data[i] += 32;
            for (const needle of needles) assert.equal(data.includes(needle), false, 'Build profile leaked in ' + relative);
            overlap = Buffer.from(data.subarray(Math.max(0, data.length - 1024)));
          }
        } finally {fs.closeSync(fd);}
        files++;
      }
    }
  }
  walk(directory); return {passed: true, filesScanned: files, actualBuildProfileStrings: 0, forbiddenPrivateFiles: 0};
}
function processes() {
  const command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); @(Get-CimInstance Win32_Process | Select-Object @{n='pid';e={[int]$_.ProcessId}},@{n='parent';e={[int]$_.ParentProcessId}},@{n='name';e={$_.Name}},@{n='exe';e={$_.ExecutablePath}},@{n='created';e={$_.CreationDate.ToString('o')}}) | ConvertTo-Json -Compress";
  const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command],
    {env: windowsPowerShellEnv, encoding: 'utf8', windowsHide: true, timeout: 20000});
  assert.equal(result.status, 0, 'Process audit failed');
  const value = JSON.parse(result.stdout || '[]'); return Array.isArray(value) ? value : [value];
}
function ownedProcesses() {return processes().filter(p => p.exe && isWithin(relocated, p.exe));}
async function waitNoOwned() {
  for (let attempt = 0; attempt < 35; attempt++) {
    const remaining = ownedProcesses();
    if (!remaining.length) return;
    await delay(300);
  }
  throw new Error('PACKAGED_OWNED_PROCESSES_REMAIN');
}
function launchContract(userData) {
  return getRuntimeLaunch({app: {isPackaged: true, getPath: () => userData},
    resourcesPath: path.join(relocated, 'resources'), userData,
    env: {...process.env, USIC_USER_DATA_DIR: userData, TALK_DESKTOP_QA: '1'}});
}
async function offline(url) {
  for (let attempt = 0; attempt < 30; attempt++) {
    try {await fetch(url, {signal: AbortSignal.timeout(500)});}
    catch {return true;}
    await delay(100);
  }
  return false;
}
async function backendCheck(mode) {
  const launch = launchContract(path.join(qa, 'profiles', mode));
  fs.mkdirSync(launch.env.TEMP, {recursive: true});
  const backend = new BackendController(launch.root, launch.env);
  try {
    const ready = await backend.start({qa: true});
    const health = await (await fetch(ready.url + 'api/health')).json();
    assert.equal(health.ok, true);
    assert.equal((await fetch(ready.url)).status, 200);
    if (mode === 'shutdown') {
      const assets = require('../packaging/files.json').files.filter(name => name.startsWith('web/'));
      for (const asset of assets) {
        const route = asset === 'web/index.html' ? '' : asset.slice(4);
        assert.equal((await fetch(ready.url + route)).status, 200, asset);
      }
      report.staticWebAssetsVerified = assets.length;
    }
    const providers = await (await fetch(ready.url + 'api/providers')).json();
    assert.deepEqual(providers.providers.map(p => p.id), ['qq', 'netease']);
    const account = await (await fetch(ready.url + 'api/account')).json();
    assert.equal(account.session, 'required');
    if (mode === 'owner-pipe-eof') {
      backend.child.stdin.destroy();
      assert.equal(await offline(ready.url + 'api/health'), true);
    } else await backend.stop();
    assert.equal(await offline(ready.url + 'api/health'), true);
    await waitNoOwned();
    report.backend.push({mode, ready: true, guardPassed: true, providers: ['qq', 'netease'],
      emptySession: true, portClosed: true, ownedProcessesRemaining: 0});
  } finally {await backend.stop();}
}

async function connectCDP(url) {
  const socket = new WebSocket(url), pending = new Map(); let sequence = 0;
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', reject, {once: true});
    setTimeout(() => reject(new Error('CDP_CONNECT_TIMEOUT')), 10000).unref();
  });
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!pending.has(message.id)) return;
    const {resolve, reject, timer} = pending.get(message.id); pending.delete(message.id); clearTimeout(timer);
    message.error ? reject(new Error(message.error.message)) : resolve(message.result);
  });
  return {close: () => socket.close(), call: (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence, timer = setTimeout(() => {pending.delete(id); reject(new Error('CDP_COMMAND_TIMEOUT'));}, 10000);
    pending.set(id, {resolve, reject, timer}); socket.send(JSON.stringify({id, method, params}));
  })};
}
async function guiCheck(mode) {
  const userData = path.join(qa, 'profiles', 'gui-' + mode);
  fs.mkdirSync(userData, {recursive: true});
  const env = {...process.env, USIC_USER_DATA_DIR: userData, TALK_DESKTOP_QA: '1'};
  delete env.ELECTRON_RUN_AS_NODE; delete env.NODE_OPTIONS;
  const child = spawn(path.join(relocated, 'Usic.exe'), ['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--user-data-dir=' + userData],
    {cwd: relocated, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = '', stdout = '', browserEndpoint;
  child.stderr.on('data', value => {stderr = (stderr + value.toString()).slice(-32000);
    browserEndpoint = /DevTools listening on (ws:\/\/127\.0\.0\.1:\d+\/\S+)/.exec(stderr)?.[1];});
  child.stdout.on('data', value => {stdout = (stdout + value.toString()).slice(-8000);});
  let cdp;
  try {
    for (let attempt = 0; attempt < 200 && !browserEndpoint; attempt++) {
      if (child.exitCode !== null) throw new Error('PACKAGED_GUI_EXITED_EARLY');
      await delay(100);
    }
    if (!browserEndpoint) {
      report.guiDiagnostic = {mode, stderr, stdout, exitCode: child.exitCode, signalCode: child.signalCode};
      throw new Error('Packaged GUI debug endpoint missing (startup failure)');
    }
    const debuggerOrigin = 'http://' + new URL(browserEndpoint).host;
    let page;
    for (let attempt = 0; attempt < 200; attempt++) {
      const targets = await (await fetch(debuggerOrigin + '/json/list')).json();
      page = targets.find(target => target.type === 'page' && /^http:\/\/127\.0\.0\.1:\d+\/$/.test(target.url));
      if (page) break;
      await delay(100);
    }
    assert.ok(page, 'Packaged app did not load its backend');
    cdp = await connectCDP(page.webSocketDebuggerUrl);
    let value;
    for (let attempt = 0; attempt < 50; attempt++) {
      value = await cdp.call('Runtime.evaluate', {expression:
        "({title:document.title, desktop:!!window.talkWindow, lyrics:!!window.usicLyrics, url:location.href})", returnByValue: true});
      if (value.result.value?.desktop) break;
      await delay(100);
    }
    assert.equal(value.result.value.desktop, true); assert.match(value.result.value.title, /Usic/);
    assert.equal((await fetch(page.url + 'api/health')).status, 200);
    const live = ownedProcesses();
    assert.ok(live.some(p => p.name.toLowerCase() === 'python.exe'));
    assert.ok(live.some(p => p.pid === child.pid));
    if (mode === 'window-close') {
      if (value.result.value.lyrics) await cdp.call('Runtime.evaluate', {expression: 'window.usicLyrics.toggle()', awaitPromise: true});
      await cdp.call('Runtime.evaluate', {expression: 'setTimeout(()=>window.talkWindow.close(),200); true', returnByValue: true});
    } else {
      // Abruptly terminate only this freshly spawned QA main PID; its backend
      // owner pipe and Windows job must close without any global process kill.
      child.kill();
    }
    cdp.close(); cdp = null;
    await waitNoOwned();
    assert.equal(await offline(page.url + 'api/health'), true);
    report.gui.push({mode, opened: true, title: value.result.value.title, nativePreload: true,
      ownedProcessCountWhileLive: live.length, ownedProcessesRemaining: 0, backendPortClosed: true});
  } finally {
    cdp?.close();
    if (child.exitCode === null && child.signalCode === null) child.kill();
  }
}

(async () => {
  assert.equal(process.platform, 'win32');
  const installer = path.join(root, `dist/Usic-${require('../package.json').version}-windows-x64-setup.exe`);
  assert.ok(fs.existsSync(installer), 'Build NSIS first');
  const before = processes().filter(p => /^(?:Usic|electron|python|pythonw|QQMusic|cloudmusic)\.exe$/i.test(p.name));
  report.productionPidsObserved = before.map(p => ({pid: p.pid, name: p.name}));
  assert.ok(isWithin(path.join(root, 'output'), qa));
  assert.equal(fs.existsSync(qa), false, 'Fresh QA destination required');
  report.qaDirectory = path.relative(root, qa);
  fs.mkdirSync(qa, {recursive: true});
  // 7-Zip locates the embedded app archive in the NSIS executable. Extract its
  // payload directly; do not execute installation callbacks, touch HKCU, or
  // create shortcuts in the real user's profile. This tests the actual artifact.
  const seven = await require('app-builder-lib/out/toolsets/7zip').getPath7za();
  const extraction = spawnSync(seven, ['x', '-y', installer, '-o' + relocated],
    {cwd: qa, encoding: 'utf8', windowsHide: true, timeout: 120000, maxBuffer: 4 * 1024 * 1024});
  assert.equal(extraction.status, 0, extraction.stderr);
  report.installerPayloadExtracted = true;
  report.installedPayloadPrivacyScan = installedPrivacyScan(relocated);
  const resources = path.join(relocated, 'resources'), resourcesBefore = inventory(resources);
  const stage = path.join(root, 'output/packaging-stage');
  assert.deepEqual(inventory(path.join(resources, 'app')), inventory(path.join(stage, 'app')), 'Only staged application files can ship');
  assert.deepEqual(inventory(path.join(resources, 'runtime')), inventory(path.join(stage, 'runtime')), 'Runtime must match clean staging');
  report.exactStagingMatch = true;
  const runtime = JSON.parse(fs.readFileSync(path.join(resources, 'runtime/runtime.json'), 'utf8'));
  for (const entry of runtime.files) assert.equal(sha(path.join(resources, 'runtime', entry.path)), entry.sha256);
  report.runtimeIntegrityFiles = runtime.files.length;
  const launch = launchContract(path.join(qa, 'profiles', 'probe'));
  fs.mkdirSync(launch.env.TEMP, {recursive: true});
  const probeCode = "import sys,json,pathlib; sys.path.insert(0,sys.argv[1]+'/src'); from providers.qq.adapter import guard; assert guard() is None; assert sys.flags.isolated and sys.flags.no_site; assert pathlib.Path(sys.prefix) == pathlib.Path(sys.executable).parent.parent; assert all('.venv' not in p for p in sys.path); print(json.dumps({'python':sys.version.split()[0],'isolated':True,'noSite':True,'qqGuard':True}))";
  const config = require('../desktop/config.cjs').loadConfig(launch.root, launch.env);
  const probe = spawnSync(config.python, ['-I', '-B', '-c', probeCode, launch.root],
    {cwd: qa, env: launch.env, encoding: 'utf8', windowsHide: true, timeout: 20000});
  assert.equal(probe.status, 0, probe.stderr); report.runtimeProbe = JSON.parse(probe.stdout);
  for (const mode of ['shutdown', 'owner-pipe-eof']) await backendCheck(mode);
  for (const mode of ['window-close', 'abrupt-owned-ui-exit']) await guiCheck(mode);
  assert.deepEqual(inventory(resources), resourcesBefore, 'Installed resources must remain read-only');
  report.resourcesUnchangedAfterLaunch = true;
  // Check installed EXE metadata and signature status without ever executing NSIS.
  const metadataScript = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $files=@($args[0],$args[1],$args[2]); @($files | ForEach-Object { $item=Get-Item -LiteralPath $_; $sig=Get-AuthenticodeSignature -FilePath $_; [PSCustomObject]@{name=$item.Name;product=$item.VersionInfo.ProductName;version=$item.VersionInfo.ProductVersion;signature=[string]$sig.Status;signer=$sig.SignerCertificate.Subject} }) | ConvertTo-Json -Compress";
  // -Command does not reliably delimit arbitrary Windows paths from script
  // arguments: pass the safe paths as process environment instead.
  const metadataEnv = {...windowsPowerShellEnv, USIC_QA_EXE: path.join(relocated, 'Usic.exe'),
    USIC_QA_PYTHON: config.python, USIC_QA_INSTALLER: installer};
  const metadata = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    metadataScript.replace('$files=@($args[0],$args[1],$args[2])', '$files=@($env:USIC_QA_EXE,$env:USIC_QA_PYTHON,$env:USIC_QA_INSTALLER)')],
    {env: metadataEnv, encoding: 'utf8', windowsHide: true, timeout: 20000});
  assert.equal(metadata.status, 0, metadata.stderr);
  report.executableMetadata = JSON.parse(metadata.stdout);
  assert.ok([require('../package.json').version, require('../package.json').version + '.0'].includes(report.executableMetadata[0].version));
  assert.match(report.executableMetadata[0].product, /Usic/);
  assert.equal(report.executableMetadata[1].signature, 'Valid');
  const after = processes();
  report.productionPidsStillAlive = before.filter(original => after.some(now => now.pid === original.pid && now.created === original.created)).map(p => p.pid);
  report.installer = {path: path.relative(root, installer), sizeBytes: fs.statSync(installer).size, sha256: sha(installer)};
  report.installerExecuted = false;
  report.unverified = ['NSIS wizard / uninstall / HKCU / UAC behavior on a separate clean Windows account or VM',
    'Real provider login/playback and audio devices (no production account touched)',
    'Optional model/torch/soundfont capability pack lifecycle with installed app',
    'Clean Windows without build-host dependencies (relocation tested with own PATH, not a separate OS)',
    'Public release license/security/code-signing review'];
  report.passed = true;
})().catch(error => {report.error = error.message; process.exitCode = 1;}).finally(async () => {
  // Failure recovery is limited to verified executable paths inside this QA copy.
  try {
    const leftovers = ownedProcesses();
    if (leftovers.length) {
      report.qaRecoveryPids = leftovers.map(p => p.pid);
      for (const process of leftovers) {try {global.process.kill(process.pid);} catch {}}
      await delay(1500);
      report.passed = false; global.process.exitCode = 1;
    }
  } catch (error) {report.cleanupAuditError = error.message;}
  fs.mkdirSync(path.dirname(reportFile), {recursive: true});
  fs.writeFileSync(reportFile, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
});
