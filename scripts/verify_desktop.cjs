'use strict';
// Real backend, fresh temporary QA library/session, no provider requests or audio.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {BackendController} = require('../desktop/backend.cjs');

(async () => {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'talk lifecycle '));
  const env = {...process.env, TALK_DATA_DIR: temp, TALK_QQ_SESSION_DIR: path.join(temp, 'qq')};
  const results = [];
  try {
    for (const exitMode of ['shutdown', 'pipe-eof']) {
      const backend = new BackendController(root, env);
      try {
        const ready = await backend.start({qa: true});
        const page = await fetch(ready.url);
        assert.equal(page.status, 200);
        const sources = await (await fetch(ready.url + 'api/providers')).json();
        assert.deepEqual(sources.providers.map(p => p.id), ['qq', 'netease']);
        assert.equal(sources.providers.find(p => p.id === 'netease').capabilities.includes('login'), false);
        const account = await (await fetch(ready.url + 'api/account')).json();
        assert.equal(account.session, 'required');
        if (exitMode === 'pipe-eof') backend.child.stdin.end();
        await backend.stop();
        let offline = false;
        for (let attempt = 0; attempt < 30; attempt++) {
          try {await fetch(ready.url + 'api/health', {signal: AbortSignal.timeout(500)});}
          catch {offline = true; break;}
          await new Promise(resolve => setTimeout(resolve, 100));
        }
        assert.equal(offline, true);
        results.push({exitMode, page: 200, providerIds: ['qq', 'netease'], emptySession: true, portClosed: true});
      } finally {await backend.stop();}
    }
    const output = path.join(root, 'output'); fs.mkdirSync(output, {recursive: true});
    fs.writeFileSync(path.join(output, 'desktop-lifecycle.json'), JSON.stringify({checkedAt: new Date().toISOString(), results}, null, 2));
    console.log('DESKTOP_BACKEND_LIFECYCLE_VERIFIED: graceful stop + owner EOF');
  } finally {fs.rmSync(temp, {recursive: true, force: true});}
})().catch(error => {console.error(error.message); process.exitCode = 1;});
