'use strict';
// Independent capability launcher. Does not start/stop Electron or player backends.
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const read = file => JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
const configPath = path.resolve(process.env.USIC_SCORE_LAB_CONFIG || path.join(root, '.private', 'score-lab', 'config.json'));
const config = {...read(path.join(root, 'capabilities', 'score_lab', 'config.example.json')),
  ...(fs.existsSync(configPath) ? read(configPath) : {})};
const runtime = path.resolve(root, config.python);
const basePython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = fs.existsSync(runtime) ? runtime : fs.existsSync(basePython) ? basePython : process.platform === 'win32' ? 'python' : 'python3';

async function main() {
  try {
    const receipt = read(path.resolve(root, config.dataDir, 'server.json'));
    if (/^http:\/\/127\.0\.0\.1:\d+\/$/.test(receipt.url)) {
      const health = await fetch(receipt.url + 'api/health', {signal: AbortSignal.timeout(1200)}).then(r => r.json());
      if (health.service === 'usic-score-lab' && health.instance === receipt.instance) {
        console.log(`小黄花 · 谱室  ${receipt.url}`);
        const opener = spawn(python, ['-c', 'import sys,webbrowser; webbrowser.open(sys.argv[1])', receipt.url],
          {cwd: root, stdio: 'inherit', windowsHide: true});
        opener.on('error', e => {console.error(e.message); process.exitCode = 1;});
        return;
      }
    }
  } catch { /* no live instance owned by this demo */ }
  const child = spawn(python, ['-m', 'capabilities.score_lab.server', '--config', configPath, '--open', ...process.argv.slice(2)],
    {cwd: root, stdio: 'inherit', windowsHide: true});
  child.on('error', () => {console.error('未找到 Python，请先配置谱室运行环境。'); process.exitCode = 1;});
  child.on('exit', code => {process.exitCode = code || 0;});
  process.on('SIGINT', () => {if (process.platform !== 'win32') child.kill('SIGINT');});
  process.on('SIGTERM', () => child.kill('SIGTERM'));
}

main().catch(error => {console.error(error.message); process.exitCode = 1;});
