'use strict';
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '..');
const stage = path.join(root, 'output/packaging-stage');
const manifest = require('./files.json');
const runtimeManifest = path.join(stage, 'runtime/runtime.json');
const runtimeFiles = fs.existsSync(runtimeManifest) ? JSON.parse(fs.readFileSync(runtimeManifest, 'utf8')).files.map(item => item.path) : [];
for (const name of runtimeFiles) {
  if (path.posix.isAbsolute(name) || name.split('/').includes('..') || /[\\*?]/.test(name)) throw new Error('INVALID_RUNTIME_FILE_WHITELIST');
}
module.exports = {
  appId: 'com.usic.player', productName: '小黄花 · Usic', executableName: 'Usic',
  electronVersion: require('../package.json').devDependencies.electron,
  directories: {app: path.join(stage, 'app'), output: path.join(root, 'dist'), buildResources: __dirname},
  asar: false, // Embedded Python cannot execute server.py inside an ASAR archive.
  files: [...manifest.files, 'package.json', 'notices/INSTALLER-LICENSE.txt', 'notices/Electron-LICENSE.txt',
    'notices/Electron-LICENSES.chromium.html', 'notices/python-dependencies.json'],
  extraResources: [{from: path.join(stage, 'runtime'), to: 'runtime', filter: ['runtime.json', ...runtimeFiles]}],
  npmRebuild: false, nodeGypRebuild: false, buildDependenciesFromSource: false,
  compression: 'normal', publish: null,
  win: {
    target: [{target: 'nsis', arch: ['x64']}], icon: path.join(root, 'desktop/assets/usic.ico'),
    requestedExecutionLevel: 'asInvoker',
  },
  nsis: {
    artifactName: 'Usic-${version}-windows-x64-setup.${ext}',
    oneClick: false, perMachine: false, allowElevation: false, allowToChangeInstallationDirectory: true,
    packElevateHelper: false, include: path.join(__dirname, 'installer.nsh'),
    unicode: true, installerLanguages: ['zh_CN', 'en_US'],
    license: path.join(stage, 'app/notices/INSTALLER-LICENSE.txt'),
    installerIcon: path.join(root, 'desktop/assets/usic.ico'),
    uninstallerIcon: path.join(root, 'desktop/assets/usic.ico'),
    shortcutName: '小黄花 · Usic', createDesktopShortcut: true, createStartMenuShortcut: true,
    runAfterFinish: true, deleteAppDataOnUninstall: false, differentialPackage: false,
  },
};
