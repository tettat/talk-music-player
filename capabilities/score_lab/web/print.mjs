import {printRequest, printableABC, paginateSystems} from './print-layout.mjs';

const base = location.pathname.startsWith('/extensions/score-lab/') ? '/extensions/score-lab' : '';
const query = new URLSearchParams(location.search);
const $ = id => document.getElementById(id);

async function render() {
  const request = printRequest(query.get('sample'), query.get('revision'));
  const suffix = request.revisionId === null ? '' : `?revision=${request.revisionId}`;
  const response = await fetch(`${base}/api/samples/${request.sampleId}/notation${suffix}`);
  if (!response.ok) throw new Error('此乐谱暂不可导出');
  const snapshot = await response.json();
  if (snapshot.id !== request.sampleId || snapshot.revisionId !== request.revisionId) throw new Error('乐谱版本不一致');
  $('title').textContent = snapshot.title;
  $('artist').textContent = snapshot.artist;
  document.title = `${snapshot.title} · 乐谱`;
  if (!window.ABCJS) throw new Error('谱面组件未加载');
  const tunes = window.ABCJS.renderAbc('notation', printableABC(snapshot.abc), {
    staffwidth: (186 * 96 / 25.4 - 16) / .9, scale: .9, responsive: 'resize',
    oneSvgPerLine: true, add_classes: true, selectTypes: false,
    paddingtop: 10, paddingbottom: 12, paddingleft: 6, paddingright: 10,
    wrap: {minSpacing: 1.3, maxSpacing: 2.6, preferredMeasuresPerLine: 4},
  });
  const systems = [...$('notation').querySelectorAll('svg')];
  if (!tunes.length || !systems.length || !$('notation').querySelector('.abcjs-note, .abcjs-rest')) {
    throw new Error('谱面排版失败');
  }
  for (const svg of systems) {
    svg.style.display = 'block';
    svg.parentElement.classList.add('score-system');
    svg.parentElement.style.overflow = 'visible';
  }
  await document.fonts.ready;
  // Two animation frames settle responsive SVG dimensions before Chromium prints.
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const width = $('notation').getBoundingClientRect().width;
  if (systems.some(svg => {const box = svg.getBoundingClientRect(); return box.width > width + 2 || box.height > 900;})) {
    throw new Error('谱面超出纸张，请缩短片段后重试');
  }
  $('status').hidden = true;
  const header = document.querySelector('main > header');
  const groups = paginateSystems(systems.map(svg => svg.parentElement.getBoundingClientRect().height),
    header.getBoundingClientRect().height, 269 * 96 / 25.4, 4 * 96 / 25.4);
  const pages = groups.map((group, index) => {
    const page = document.createElement('section'); page.className = 'sheet-page';
    if (!index) page.append(header);
    page.append(...group.map(i => systems[i].parentElement));
    return page;
  });
  document.querySelector('main').replaceChildren(...pages);
  $('print-button').disabled = false;
  if (query.get('print') === '1') setTimeout(() => window.print(), 100);
  return {ready: true, title: snapshot.title, systems: systems.length, pages: pages.length, revisionId: request.revisionId};
}

$('print-button').addEventListener('click', () => window.print());
window.usicScorePrintReady = render().catch(() => {
  if ($('status')) $('status').textContent = '此乐谱暂不可导出，请返回重试';
  return {ready: false};
});
