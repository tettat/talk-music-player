import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {printableABC, printRequest, printURL, paginateSystems} from '../capabilities/score_lab/web/print-layout.mjs';

test('print links pin the exact saved revision and never accept a URL or path', () => {
  assert.equal(printURL('/extensions/score-lab', 'fixture'), '/extensions/score-lab/print.html?sample=fixture');
  assert.equal(printURL('', 'fixture', 'revision-1', true), '/print.html?sample=fixture&revision=revision-1&print=1');
  for (const [sid, rev] of [['../secret', null], ['fixture', '../secret'], ['fixture', ''], [null, null], ['fixture', {}]]) {
    assert.throws(() => printRequest(sid, rev));
  }
});

test('print adaptation keeps all musical voices, chord members, ties and meter unchanged', () => {
  const body = 'V:Vocal name="Vocal Melody"\nV:Ins name="Ins Melody"\nV:Accomp name="伴奏"\nK:C\n[V:Vocal] C2-C2|\n[V:Ins] z4|\n[V:Accomp] "C"[CEG]4|\n';
  const source = 'X:1\nT:Internal title\nM:4/4\nL:1/8\n%%staffwidth 1000\n' + body;
  const result = printableABC(source);
  assert.doesNotMatch(result, /^T:|^%%staffwidth/m);
  assert.match(result, /M:4\/4/); assert.match(result, /人声旋律/); assert.match(result, /器乐旋律/);
  assert.ok(result.endsWith(body.replace('Vocal Melody', '人声旋律').replace('Ins Melody', '器乐旋律')));
  assert.throws(() => printableABC('a'.repeat(512 * 1024 + 1)));
});

test('PDF page uses per-system pagination, fonts-ready and vector SVG instead of a screen capture', () => {
  const script = fs.readFileSync(new URL('../capabilities/score_lab/web/print.mjs', import.meta.url), 'utf8');
  const css = fs.readFileSync(new URL('../capabilities/score_lab/web/print.css', import.meta.url), 'utf8');
  assert.match(script, /oneSvgPerLine: true/); assert.match(script, /document.fonts.ready/);
  assert.match(script, /textContent = snapshot.title/); assert.doesNotMatch(script, /innerHTML|capturePage|html2canvas/);
  assert.match(css, /@page.*A4/); assert.match(css, /break-inside: avoid/);
});

test('pagination never creates an orphan title or splits a staff system between pages', () => {
  assert.deepEqual(paginateSystems([280, 240, 240, 240], 80, 1016, 15), [[0, 1, 2], [3]]);
  assert.deepEqual(paginateSystems([200, 200], 70, 1016, 15), [[0, 1]]);
  assert.throws(() => paginateSystems([970], 80, 1016, 15));
  assert.throws(() => paginateSystems([NaN], 80, 1016, 15));
  assert.throws(() => paginateSystems([], 80, 1016, 15));
});

test('multi-measure rests preserve silent staves without changing total musical time', () => {
  const abc = printableABC('X:1\nM:3/4\nL:1/8\nK:C\n[V:Ins] Z3|\n');
  assert.match(abc, /\[V:Ins\] z6\|z6\|z6\|/);
  assert.throws(() => printableABC('X:1\nK:C\nZ1501|'));
});

test('PDF action shares the dirty-draft guard and parent checks the exact owning iframe', () => {
  const script = fs.readFileSync(new URL('../capabilities/score_lab/web/app.mjs', import.meta.url), 'utf8');
  const host = fs.readFileSync(new URL('../web/score-extension.mjs', import.meta.url), 'utf8');
  assert.match(script, /!state.data \|\| dirty\(\) \|\| editSaving \|\| pdfSaving/);
  assert.match(host, /event.origin !== location.origin \|\| event.source !== frame.contentWindow/);
  assert.match(host, /window.usicScorePdf.export\(input.sampleId, input.revisionId\)/);
});
