import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {taskLabel, clipLabel} from '../web/score-extension.mjs';

test('task display uses actual model phases/windows and never guessed percentages or ETA', () => {
  assert.equal(taskLabel({status: {state: 'running', phase: 'loading'}}), '加载模型');
  assert.equal(taskLabel({status: {phase: 'recognizing', window: 2, windows: 3}}), '识别中 · 2/3');
  assert.equal(taskLabel({status: {phase: 'cancelled'}}), '已取消');
  assert.equal(taskLabel({status: {phase: 'future-phase'}}), '未完成');
  assert.equal(clipLabel({full: true, duration: 300}), '整首');
  assert.equal(clipLabel({duration: 90}), '90 秒');
});
test('host/capability path and lifecycle contracts remain explicitly independent', () => {
  const host = fs.readFileSync(new URL('../web/score-extension.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(host, /credential|DPAPI|torch|\/api\/play['"`]/i);
  assert.match(host, /\/api\/scores\/track/);
  assert.match(host, /\/extensions\/score-lab\//);
  const backend = fs.readFileSync(new URL('../src/score_extension.py', import.meta.url), 'utf8');
  assert.doesNotMatch(backend, /^import torch|^from torch/m);
  assert.doesNotMatch(backend, /taskkill|killall|Stop-Process/i);
  assert.match(backend, /OwnedJob\(\)/);
  const html = fs.readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  assert.match(html, /<dialog[^>]+id="score-submit-dialog"/);
});
