'use strict';
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {desktopLaunchOptions} = require('../desktop/launch.cjs');
const root = path.resolve(__dirname, '..');
if (!fs.existsSync(path.join(root, 'node_modules/electron/dist/electron.exe'))) {
  console.error('RUN_NPM_CI_AND_NPM_RUN_SETUP_DESKTOP_FIRST'); process.exit(1);
}
const child = spawn(require('electron'), [root],
  desktopLaunchOptions(process.env));
child.on('error', () => {console.error('DESKTOP_START_FAILED'); process.exitCode = 1;});
child.on('exit', code => {process.exitCode = code ?? 1;});
