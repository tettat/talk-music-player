'use strict';
// Packaged-only bootstrap plus a side-effect-free launch contract for main.cjs.
const fs = require('node:fs');
const path = require('node:path');
const {resolveConfigPath} = require('./config.cjs');

function isWithin(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative));
}

function getRuntimeLaunch({app, resourcesPath = process.resourcesPath,
  root = path.resolve(__dirname, '..'), env = process.env, userData} = {}) {
  if (!app?.isPackaged) return Object.freeze({root, runtimeRoot: root, env: {...env}, packaged: false});
  if (process.platform !== 'win32') throw new Error('PACKAGED_WINDOWS_X64_ONLY');
  const resources = resolveConfigPath(resourcesPath, '.');
  const runtimeRoot = path.join(resources, 'runtime');
  const appRoot = path.join(resources, 'app'); // asar:false: Python must read real files.
  userData = userData ?? env.USIC_USER_DATA_DIR ?? app.getPath('userData');
  if (typeof userData !== 'string' || !userData.trim() || userData.includes('\0') || !path.isAbsolute(userData)) {
    throw new Error('PACKAGED_USER_DATA_INVALID');
  }
  userData = resolveConfigPath(root, userData);
  if (isWithin(resources, userData)) throw new Error('PACKAGED_USER_DATA_IN_RESOURCES');
  for (const file of [path.join(runtimeRoot, 'base/python/Scripts/python.exe'), path.join(appRoot, 'src/server.py')]) {
    if (!fs.statSync(file, {throwIfNoEntry: false})?.isFile()) throw new Error('PACKAGED_RUNTIME_MISSING');
  }
  const launchEnv = {...env};
  // Do not inherit developer venvs, device-local TALK_* paths, module injection,
  // or the Electron-as-Node flag into this app's backend/capability children.
  for (const name of Object.keys(launchEnv)) {
    if (/^(TALK_|PYTHON|VIRTUAL_ENV$|CONDA_|NODE_OPTIONS$|NODE_PATH$|ELECTRON_RUN_AS_NODE$)/i.test(name)) {
      delete launchEnv[name];
    }
  }
  const scoreConfig = env.USiC_SCORE_LAB_CONFIG ?? env.USIC_SCORE_LAB_CONFIG ??
    path.join(userData, 'capabilities/score-lab/config.json');
  if (typeof scoreConfig !== 'string' || !scoreConfig.trim() || scoreConfig.includes('\0') || !path.isAbsolute(scoreConfig)) {
    throw new Error('PACKAGED_SCORE_CONFIG_INVALID');
  }
  const systemRoot = env.SystemRoot || env.SYSTEMROOT || env.WINDIR;
  if (!systemRoot || !path.isAbsolute(systemRoot)) throw new Error('PACKAGED_SYSTEM_ROOT_REQUIRED');
  for (const name of Object.keys(launchEnv)) if (name.toUpperCase() === 'PATH') delete launchEnv[name];
  Object.assign(launchEnv, {
    TALK_PACKAGED: '1', TALK_RUNTIME_ROOT: runtimeRoot, TALK_USER_DATA: userData,
    // Match the capability's existing uppercase consumer; accept the requested spelling too.
    USIC_SCORE_LAB_CONFIG: path.resolve(scoreConfig), USiC_SCORE_LAB_CONFIG: path.resolve(scoreConfig),
    PATH: [path.join(runtimeRoot, 'base/python'), path.join(systemRoot, 'System32'), systemRoot].join(path.delimiter),
    PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1', PYTHONUTF8: '1',
    TEMP: path.join(userData, 'tmp'), TMP: path.join(userData, 'tmp'),
  });
  if (env.TALK_DESKTOP_QA === '1') launchEnv.TALK_DESKTOP_QA = '1';
  return Object.freeze({root: appRoot, runtimeRoot, userData, env: launchEnv, packaged: true});
}

function bootstrap(app) {
  app.setName('小黄花 · Usic');
  const launch = getRuntimeLaunch({app});
  if (launch.packaged) {
    fs.mkdirSync(launch.env.TEMP, {recursive: true});
    for (const name of Object.keys(process.env)) {
      if (!(name in launch.env)) delete process.env[name];
    }
    Object.assign(process.env, launch.env);
    // Before main's single-instance lock/BrowserWindow; never open the real QA profile.
    app.setPath('userData', launch.userData);
    app.setPath('sessionData', launch.userData);
  }
  return launch;
}

module.exports = {getRuntimeLaunch, bootstrap, isWithin};
if (require.main === module) {
  const {app, dialog} = require('electron');
  try {bootstrap(app); require('./main.cjs');}
  catch (error) {
    if (process.env.TALK_DESKTOP_QA === '1') console.error('USIC_PACKAGED_BOOTSTRAP_FAILED:', error.message);
    dialog.showErrorBox('小黄花 · Usic', '安装文件或数据目录无效。请重新安装，或检查 USIC_USER_DATA_DIR。');
    app.exit(1);
  }
}
