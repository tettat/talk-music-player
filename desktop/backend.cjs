'use strict';
const {spawn} = require('node:child_process');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const {loadConfig} = require('./config.cjs');

function validateReady(value) {
  if (!value || value.event !== 'ready' || !Number.isInteger(value.port) ||
      value.port < 1024 || value.port > 65535 || !Number.isInteger(value.pid) || value.pid < 1 ||
      value.url !== `http://127.0.0.1:${value.port}/` ||
      typeof value.instance !== 'string' || !/^[a-f0-9]{32}$/.test(value.instance) ||
      value.automaticOfficialClientLaunch !== false) throw new Error('BACKEND_READY_INVALID');
  return value;
}

class BackendController extends EventEmitter {
  constructor(root = path.resolve(__dirname, '..'), env = process.env) {
    super(); this.root = root; this.env = {...env}; this.child = null; this.stopping = false;
  }

  async start({qa = false} = {}) {
    if (this.child) throw new Error('BACKEND_ALREADY_STARTED');
    const config = loadConfig(this.root, this.env);
    if (!fs.existsSync(config.python)) throw new Error('LOCAL_PYTHON_RUNTIME_REQUIRED');
    const args = ['-I', '-B', path.join(this.root, 'src/server.py'), '--desktop', '--port', '0'];
    if (qa) args.push('--qa');
    this.child = spawn(config.python, args, {
      cwd: this.root, env: this.env, shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'],
    });
    this.child.stdin.on('error', () => {}); // EOF after a child crash is not an uncaught EPIPE.
    this.child.once('exit', () => {
      if (!this.stopping) this.emit('unexpected-exit');
    });
    try {
      const ready = await new Promise((resolve, reject) => {
        let buffer = '';
        const timer = setTimeout(() => finish(new Error('BACKEND_START_TIMEOUT')), 45000);
        const onError = () => finish(new Error('BACKEND_START_FAILED'));
        const onExit = () => finish(new Error('BACKEND_START_FAILED'));
        const onData = chunk => {
          buffer += chunk.toString('utf8');
          if (buffer.length > 16384) return finish(new Error('BACKEND_OUTPUT_INVALID'));
          const newline = buffer.indexOf('\n');
          if (newline < 0) return;
          try { finish(null, validateReady(JSON.parse(buffer.slice(0, newline)))); }
          catch { finish(new Error('BACKEND_START_FAILED')); }
        };
        const finish = (error, value) => {
          clearTimeout(timer); this.child.stdout.off('data', onData);
          this.child.off('error', onError); this.child.off('exit', onExit);
          error ? reject(error) : resolve(value);
        };
        this.child.on('error', onError); this.child.on('exit', onExit);
        this.child.stdout.on('data', onData);
      });
      const response = await fetch(ready.url + 'api/health', {redirect: 'error', signal: AbortSignal.timeout(5000)});
      const health = await response.json();
      if (!response.ok || health.ok !== true || health.instance !== ready.instance) throw new Error('BACKEND_IDENTITY_MISMATCH');
      this.ready = ready;
      return ready;
    } catch (error) {
      await this.stop(); throw error;
    }
  }

  async stop() {
    if (this.stopPromise) return this.stopPromise;
    this.stopping = true;
    this.stopPromise = (async () => {
      const child = this.child;
      if (!child || child.exitCode !== null || child.signalCode !== null) return;
      await new Promise(resolve => {
        let forced = false;
        const timer = setTimeout(() => {
          forced = true;
          // Only this directly owned process; closing its pipe also stops the real
          // venv child, whose Windows job owns any analysis descendants.
          child.stdin.destroy(); child.kill();
          setTimeout(resolve, 1500).unref();
        }, 6000);
        child.once('exit', () => {clearTimeout(timer); resolve();});
        child.stdin.end('shutdown\n');
        if (forced) resolve();
      });
    })();
    return this.stopPromise;
  }
}

module.exports = {BackendController, validateReady};
