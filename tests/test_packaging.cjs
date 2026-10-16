'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {getRuntimeLaunch} = require('../desktop/packaging.cjs');
const {loadConfig} = require('../desktop/config.cjs');
const manifest = require('../packaging/files.json');
const build = require('../packaging/electron-builder.cjs');
const root = path.resolve(__dirname, '..');
const python = process.env.USIC_BUILD_PYTHON || path.join(root, '.venv/Scripts/python.exe');

function fixture(run) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'Usic 中文 安装 '));
  try {return run(temp);} finally {fs.rmSync(temp, {recursive: true, force: true});}
}
function packagedFixture(temp) {
  const resourcesPath = path.join(temp, '自选目录 with spaces/resources');
  for (const name of ['runtime/base/python/Scripts/python.exe', 'app/src/server.py']) {
    const file = path.join(resourcesPath, name); fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, '');
  }
  const userData = path.join(temp, '独立 profile with spaces');
  const app = {isPackaged: true, getPath: name => {assert.equal(name, 'userData'); return userData;}};
  return {app, resourcesPath, userData};
}

test('NSIS is fixed-version, x64, assisted per-user, no elevation, directory selection and real icon', () => {
  const pkg = require('../package.json'), lock = require('../package-lock.json');
  assert.equal(pkg.devDependencies.electron, '44.7.0');
  assert.equal(pkg.devDependencies['electron-builder'], '26.15.3');
  assert.equal(lock.packages['node_modules/electron-builder'].version, '26.15.3');
  assert.equal(pkg.main, 'desktop/main.cjs');
  assert.equal(build.asar, false); assert.equal(build.publish, null);
  assert.deepEqual(build.win.target, [{target: 'nsis', arch: ['x64']}]);
  assert.equal(build.win.requestedExecutionLevel, 'asInvoker');
  assert.equal(build.nsis.oneClick, false); assert.equal(build.nsis.perMachine, false);
  assert.equal(build.nsis.allowElevation, false); assert.equal(build.nsis.packElevateHelper, false);
  assert.equal(build.nsis.allowToChangeInstallationDirectory, true);
  assert.equal(build.nsis.deleteAppDataOnUninstall, false);
  assert.equal(fs.readFileSync(build.win.icon).readUInt16LE(2), 1);
  assert.match(fs.readFileSync(build.nsis.include, 'utf8'), /StrCpy \$isForceCurrentInstall "1"/);
  assert.match(fs.readFileSync(path.join(root, 'scripts/package_windows.cjs'), 'utf8'), /'--publish', 'never'/);
});

test('application resources are explicit positive filenames, not broad repo globs', () => {
  assert.equal(new Set(manifest.files).size, manifest.files.length);
  for (const name of manifest.files) {
    assert.doesNotMatch(name, /[*?]|(^|\/)(?:\.private|\.venv|node_modules|output|dist|cache)(\/|$)/i);
    assert.doesNotMatch(name, /config\.local|\.(?:db|sqlite3?|dpapi|log|mp3|wav|onnx|pt|pth|safetensors|lnk)$/i);
    assert.equal(fs.statSync(path.join(root, name)).isFile(), true, name);
  }
  for (const resource of build.extraResources) assert.ok(resource.filter.every(name => !/[*?]/.test(name)));
  for (const name of ['LICENSE', 'COPYING-QQMusicApi', 'THIRD_PARTY.txt', 'desktop/packaging.cjs',
    'capabilities/score_lab/server.py', 'capabilities/score_lab/notices/piano-attribution.txt']) {
    assert.equal(manifest.files.includes(name), true);
  }
  assert.match(fs.readFileSync(path.join(root, 'packaging/INSTALLER.txt'), 'utf8'), /私人测试，公开发布前复核/);
});

test('development helper does not change roots, env, config or create files', () => fixture(temp => {
  const env = {TALK_DATA_DIR: 'custom data', PYTHONPATH: 'developer-only'};
  const launch = getRuntimeLaunch({app: {isPackaged: false}, root: temp, env});
  assert.equal(launch.root, temp); assert.equal(launch.packaged, false); assert.deepEqual(launch.env, env);
  assert.deepEqual(fs.readdirSync(temp), []);
  assert.equal(loadConfig(temp, {}).data_dir, path.join(temp, '.private'));
}));

test('Chinese/space install roots resolve relative owned runtime and isolated userData, stripping dev env',
  {skip: process.platform !== 'win32'}, () => fixture(temp => {
    const options = packagedFixture(temp);
    const scoreConfig = path.join(temp, '识谱 能力/config.json');
    const input = {SystemRoot: process.env.SystemRoot, Path: 'untrusted-global-python',
      TALK_PYTHON: 'untrusted', TALK_CONFIG_FILE: 'config.local.json', TALK_DATA_DIR: 'untrusted',
      VIRTUAL_ENV: 'untrusted', PYTHONPATH: 'untrusted', NODE_OPTIONS: '--inspect',
      USiC_SCORE_LAB_CONFIG: scoreConfig};
    const launch = getRuntimeLaunch({...options, env: input});
    assert.equal(launch.root, path.join(options.resourcesPath, 'app'));
    assert.equal(launch.runtimeRoot, path.join(options.resourcesPath, 'runtime'));
    assert.equal(launch.env.TALK_USER_DATA, options.userData);
    for (const key of ['TALK_CONFIG_FILE', 'TALK_PYTHON', 'TALK_DATA_DIR', 'VIRTUAL_ENV', 'PYTHONPATH', 'NODE_OPTIONS', 'Path']) {
      assert.equal(key in launch.env, false, key);
    }
    assert.equal(launch.env.USIC_SCORE_LAB_CONFIG, scoreConfig);
    assert.equal(launch.env.USiC_SCORE_LAB_CONFIG, scoreConfig);
    const config = loadConfig(launch.root, launch.env);
    assert.equal(config.python, path.join(launch.runtimeRoot, 'base/python/Scripts/python.exe'));
    assert.equal(config.data_dir, options.userData);
    assert.equal(config.qq_session_dir, path.join(options.userData, 'qq'));
    assert.equal(fs.existsSync(options.userData), false); // helper has no side effects.
  }));

test('packaged helper rejects resources data roots, relative capability config and missing runtime',
  {skip: process.platform !== 'win32'}, () => fixture(temp => {
    const options = packagedFixture(temp), env = {SystemRoot: process.env.SystemRoot};
    assert.throws(() => getRuntimeLaunch({...options, userData: path.join(options.resourcesPath, 'private'), env}), /IN_RESOURCES/);
    assert.throws(() => getRuntimeLaunch({...options, env: {...env, USIC_SCORE_LAB_CONFIG: 'relative/config.json'}}), /SCORE_CONFIG_INVALID/);
    const junction = path.join(temp, 'junction-to-resources');
    fs.symlinkSync(options.resourcesPath, junction, 'junction');
    assert.throws(() => getRuntimeLaunch({...options, userData: path.join(junction, 'private'), env}), /IN_RESOURCES/);
    fs.unlinkSync(path.join(options.resourcesPath, 'runtime/base/python/Scripts/python.exe'));
    assert.throws(() => getRuntimeLaunch({...options, env}), /RUNTIME_MISSING/);
  }));

const pythonSettings = String.raw`
import sys, json, os
sys.path.insert(0, sys.argv[1] + '/src')
env = json.loads(sys.argv[3])
os.environ.update(env)
try:
    from settings import load_settings
    from dataclasses import asdict
    value = asdict(load_settings(sys.argv[2], env))
    print(json.dumps({'ok': {k: str(v) if hasattr(v, '__fspath__') else v for k,v in value.items()}}))
except Exception as error:
    print(json.dumps({'error': str(error)}))
`;
function compareConfig(directory, env) {
  let js;
  try {const {schema_version, ...ok} = loadConfig(directory, env); js = {ok};}
  catch (error) {js = {error: error.message};}
  const result = spawnSync(python, ['-I', '-B', '-c', pythonSettings, root, directory, JSON.stringify(env)],
    {encoding: 'utf8', windowsHide: true, timeout: 15000});
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), js);
}

test('Python and JS strictly agree on dev and installed paths, BOM config, overrides, invalid schema',
  {skip: !fs.existsSync(python)}, () => fixture(temp => {
    const appRoot = path.join(temp, '中文 app'); fs.mkdirSync(appRoot);
    compareConfig(appRoot, {});
    fs.writeFileSync(path.join(appRoot, 'config.local.json'), '\uFEFF' + JSON.stringify({port: 43301, data_dir: 'dev data'}));
    compareConfig(appRoot, {TALK_QQ_SESSION_DIR: 'sessions 中文'});
    const env = {TALK_PACKAGED: '1', TALK_RUNTIME_ROOT: path.join(temp, 'resources/runtime'),
      TALK_USER_DATA: path.join(temp, '用户 数据')};
    fs.mkdirSync(env.TALK_USER_DATA);
    compareConfig(appRoot, env); // Ignores config.local.json, even if present.
    const file = path.join(env.TALK_USER_DATA, 'config.json');
    fs.writeFileSync(file, '\uFEFF' + JSON.stringify({analysis_python: '能力包/python.exe', port: 43303}));
    compareConfig(appRoot, env);
    compareConfig(appRoot, {...env, TALK_BASIC_PITCH_MODEL: '能力包/model.onnx'});
    for (const value of [{password: 'synthetic'}, [], null, {port: true}, {port: 0}, {ttl_seconds: '60'},
      {python: ''}, {schema_version: false}, {schema_version: 2}, {qq_session_dir: 'bad\0path'},
      {data_dir: path.join(temp, 'resources/forbidden')}]) {
      fs.writeFileSync(file, JSON.stringify(value)); compareConfig(appRoot, env);
    }
    fs.unlinkSync(file);
    for (const raw of ['{"port":43180.0}', '{"port":4.318e4}', '{"schema_version":1.0}', '{"port":NaN}',
      '{"unknown":1.0}', '{"data_dir":{"port":1.0}}']) {
      fs.writeFileSync(file, raw); compareConfig(appRoot, env);
    }
    fs.unlinkSync(file);
    compareConfig(appRoot, {...env, TALK_CONFIG_FILE: 'does-not-exist.json'});
    compareConfig(appRoot, {...env, TALK_RUNTIME_ROOT: 'relative-runtime'});
  }));

test('staging negative tests reject secrets, unlisted private files, zip traversal and unsafe cleanup',
  {skip: !fs.existsSync(python)}, () => {
    const result = spawnSync(python, ['-I', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_packaging.py'],
      {cwd: root, encoding: 'utf8', windowsHide: true, timeout: 30000});
    assert.equal(result.status, 0, result.stdout + result.stderr);
  });
