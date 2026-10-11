'use strict';
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '..');
if (!fs.existsSync(path.join(root, 'node_modules/electron/dist/electron.exe'))) {
  console.error('RUN_NPM_CI_AND_NPM_RUN_SETUP_DESKTOP_FIRST'); process.exit(1);
}
const env = {...process.env};
delete env.ELECTRON_RUN_AS_NODE;
const child = spawn(require('electron'), [root],
  {env, shell: false, windowsHide: true, stdio: 'inherit'});
child.on('error', () => {console.error('DESKTOP_START_FAILED'); process.exitCode = 1;});
child.on('exit', code => {process.exitCode = code ?? 1;});
