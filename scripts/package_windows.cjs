'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const command = process.argv[2] || 'nsis';
const startedAt = new Date().toISOString();
if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('WINDOWS_X64_BUILD_REQUIRED');
const python = process.env.USIC_BUILD_PYTHON || path.join(root, '.venv/Scripts/python.exe');
if (!fs.existsSync(python)) throw new Error('PYTHON312_BUILD_TOOL_REQUIRED: set USIC_BUILD_PYTHON');
function run(executable, args, env = process.env) {
  const result = spawnSync(executable, args, {cwd: root, stdio: 'inherit', windowsHide: true, env});
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
run(python, ['-I', '-B', 'scripts/package_portable.py', ...(command === 'audit' ? ['--audit-only'] : [])]);
if (['stage', 'audit'].includes(command)) process.exit(0);
if (!['nsis', 'dir'].includes(command)) throw new Error('PACKAGING_COMMAND_INVALID');
const env = {...process.env, CSC_IDENTITY_AUTO_DISCOVERY: 'false', ELECTRON_BUILDER_COMPRESSION_LEVEL: '5'};
// Never use/infer a signing credential from the developer's shell.
for (const name of Object.keys(env)) if (/^(CSC_|WIN_CSC_)/.test(name) && name !== 'CSC_IDENTITY_AUTO_DISCOVERY') delete env[name];
run(process.execPath, ['node_modules/electron-builder/out/cli/cli.js',
  '--config', 'packaging/electron-builder.cjs', '--win', ...(command === 'dir' ? ['--dir'] : ['nsis']),
  '--x64', '--publish', 'never'], env);
if (command === 'nsis') {
  const name = `Usic-${require('../package.json').version}-windows-x64-setup.exe`;
  const artifact = path.join(root, 'dist', name);
  const record = {startedAt, completedAt: new Date().toISOString(), exitCode: 0,
    command: 'npm run pack:win', cleanStaging: true, target: 'nsis', arch: 'x64',
    electron: require('../package.json').devDependencies.electron,
    electronBuilder: require('../package.json').devDependencies['electron-builder'],
    publish: 'never', compressionLevel: 5, artifact: {path: 'dist/' + name,
      sizeBytes: fs.statSync(artifact).size,
      sha256: crypto.createHash('sha256').update(fs.readFileSync(artifact)).digest('hex')}};
  fs.writeFileSync(path.join(root, 'output/outsource/packaging-build.json'), JSON.stringify(record, null, 2) + '\n');
}
