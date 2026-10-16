// Optional host UI. No provider/account/model code; the host passes track identities.
export const scorePhases = {queued: '等待识谱', preparing: '准备音频', loading: '加载模型',
  recognizing: '识别中', notation: '生成乐谱', rendering: '生成回放', ready: '已完成',
  error: '未完成', interrupted: '已中断', cancelled: '已取消', editing: '保存编辑'};
export function taskLabel(item) {
  const status = item.status || {};
  return (scorePhases[status.phase] || '未完成') +
    (status.windows > 1 ? ` · ${status.window || 0}/${status.windows}` : '');
}
export function clipLabel(item) {
  return item.full ? '整首' : `${Math.round(item.duration || 30)} 秒`;
}

export function installScoreExtension({api, showView, getTrack, getCurrent, onError, toast, pausePlayer}) {
  const $ = id => document.getElementById(id);
  const root = document.createElement('section'); root.id = 'score-extension'; root.hidden = true;
  root.setAttribute('aria-label', '谱室');
  root.innerHTML = `
    <div class="scores-overview" id="scores-overview">
      <header class="scores-heading"><div><h1>谱室 <span id="scores-count"></span></h1></div>
        <div class="scores-heading-actions"><button class="icon-btn" id="scores-import" title="导入音频" aria-label="导入音频"><svg class="icon"><use href="#i-plus"/></svg></button><button class="score-switch" id="scores-enabled" role="switch" aria-checked="false" title="启用谱室" aria-label="启用谱室"><i></i></button></div>
      </header>
      <div class="scores-setup" id="scores-setup"><svg class="icon"><use href="#i-score"/></svg><h2 id="scores-setup-title">谱室未启用</h2><button class="score-primary" id="scores-setup-action">启用谱室</button></div>
      <div id="scores-collections" hidden><section class="scores-section" id="scores-pending-section"><h2>任务 <span id="scores-pending-count"></span></h2><div id="scores-pending"></div></section><section class="scores-section" id="scores-done-section"><h2>乐谱 <span id="scores-done-count"></span></h2><div class="scores-grid" id="scores-done"></div></section><div class="scores-empty" id="scores-empty" hidden><svg class="icon"><use href="#i-score"/></svg><span>暂无乐谱</span></div></div>
      <div class="scores-foot"><span><i></i>本机处理</span><button id="scores-bundle" class="text-btn">能力包</button></div>
    </div><div class="scores-workbench" id="scores-workbench" hidden><iframe id="scores-frame" title="乐谱工作台" referrerpolicy="no-referrer"></iframe></div>`;
  $('content').append(root);
  const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = '/score-extension.css'; document.head.append(css);
  const nav = document.createElement('button'); nav.type = 'button'; nav.className = 'nav-btn';
  nav.dataset.view = 'score'; nav.title = '谱室'; nav.setAttribute('aria-label', '谱室');
  nav.innerHTML = '<svg class="icon"><use href="#i-score"/></svg><span class="nav-label">谱室</span><i class="score-nav-dot" hidden></i>';
  document.querySelector('.nav').append(nav); nav.addEventListener('click', () => {home(); showView('score');});
  const dialog = $('score-submit-dialog') || document.createElement('dialog'); dialog.className = 'score-dialog'; dialog.id = 'score-submit-dialog';
  dialog.innerHTML = `<form><header><h2>识谱</h2><button type="button" class="icon-btn" aria-label="关闭" title="关闭"><svg class="icon"><use href="#i-close"/></svg></button></header><strong id="score-submit-title"></strong><span id="score-submit-artist"></span><div class="score-clip-choice" role="group" aria-label="识谱长度"><button type="button" data-seconds="30" aria-pressed="true">30 秒</button><button type="button" data-seconds="90" aria-pressed="false">90 秒</button><button type="button" data-seconds="full" aria-pressed="false">整首</button></div><label class="score-offset" for="score-submit-offset">起点 <input id="score-submit-offset" type="number" min="0" max="86400" step="1" value="0" required> 秒</label><div class="score-submit-note">本机处理 · 后台运行</div><footer><button type="button" class="score-cancel">取消</button><button type="submit" class="score-primary" id="score-submit-start">开始识谱</button></footer></form>`;
  document.body.append(dialog);
  let description = null, samples = [], timer, selectedTrack, seconds = 30, opened = false, fetching = false;
  const frame = $('scores-frame');
  const completed = new Set();
  let initialized = false;
  const create = (tag, cls, text) => {const el = document.createElement(tag); el.className = cls; if (text !== undefined) el.textContent = text; return el;};
  const action = (name, label, callback, cls = 'icon-btn') => {
    const el = create('button', cls); el.type = 'button'; el.title = label; el.setAttribute('aria-label', label);
    el.innerHTML = `<svg class="icon"><use href="#i-${name}"/></svg>`; el.addEventListener('click', callback); return el;
  };
  const currentButton = action('score', '识谱当前歌曲', () => openTrack(getCurrent()?.key));
  currentButton.id = 'player-score'; currentButton.disabled = !getCurrent()?.key;
  $('player-like').insertAdjacentElement('afterend', currentButton);
  async function refresh() {
    if (fetching) return;
    fetching = true;
    try {
      description = await api('/api/extensions/score-lab');
      if (description.enabled) {
        const catalog = await api('/extensions/score-lab/api/catalog'); samples = catalog.samples;
        const newlyDone = samples.filter(item => item.status.state === 'completed' && !completed.has(item.id));
        if (initialized && newlyDone.length) toast('乐谱已完成');
        samples.filter(s => s.status.state === 'completed').forEach(s => completed.add(s.id));
        initialized = true;
      }
      paint();
    } catch (error) {if (!root.hidden) onError(error);}
    finally {fetching = false; clearTimeout(timer); timer = setTimeout(refresh, description?.enabled ? 1800 : 5000);}
  }
  function paint() {
    const focused = root.contains(document.activeElement) ? document.activeElement?.getAttribute('aria-label') : null;
    const enabled = description?.enabled === true, ready = description?.runtimeReady === true;
    $('scores-enabled').setAttribute('aria-checked', String(enabled));
    $('scores-enabled').title = enabled ? '停用谱室' : '启用谱室';
    $('scores-enabled').setAttribute('aria-label', $('scores-enabled').title);
    $('scores-setup').hidden = enabled && ready;
    $('scores-collections').hidden = !enabled || (!ready && !samples.length);
    $('scores-setup-title').textContent = !enabled ? '谱室未启用' : '识谱模块未就绪';
    $('scores-setup-action').textContent = !enabled ? '启用谱室' : '选择能力包';
    $('scores-import').disabled = !enabled || !ready;
    const pending = samples.filter(s => s.status.state !== 'completed').reverse();
    const done = samples.filter(s => s.status.state === 'completed').reverse();
    $('scores-count').textContent = done.length || '';
    $('scores-pending-count').textContent = pending.length; $('scores-done-count').textContent = done.length;
    $('scores-pending-section').hidden = !pending.length; $('scores-done-section').hidden = !done.length;
    $('scores-empty').hidden = Boolean(samples.length);
    nav.querySelector('.score-nav-dot').hidden = !enabled || !pending.some(s => ['queued', 'running'].includes(s.status.state));
    $('scores-pending').replaceChildren(...pending.map(item => {
      const row = create('div', 'score-task'); const art = create('span', 'score-task-art');
      art.innerHTML = '<svg class="icon"><use href="#i-score"/></svg>';
      const text = create('div', 'score-task-text'); text.append(create('strong', '', item.title), create('small', '', `${clipLabel(item)} · ${taskLabel(item)}`));
      row.append(art, text);
      if (['queued', 'running'].includes(item.status.state)) {
        row.classList.add('is-working'); row.append(action('close', '取消识谱 ' + item.title, () => mutate(item, 'cancel')));
      } else {row.append(action('repeat', '重新识谱 ' + item.title, () => mutate(item, 'retry')));}
      return row;
    }));
    $('scores-done').replaceChildren(...done.map((item, index) => {
      const card = create('button', 'score-card'); card.type = 'button'; card.title = '打开乐谱 ' + item.title;
      card.setAttribute('aria-label', '打开乐谱 ' + item.title);
      const art = create('span', 'score-card-art'); art.dataset.palette = index % 3;
      art.innerHTML = '<svg class="icon"><use href="#i-score"/></svg><span class="score-staves" aria-hidden="true"></span>';
      const info = create('span', 'score-card-info'); info.append(create('strong', '', item.title), create('small', '', `${item.artist || '本机音频'} · ${clipLabel(item)}`));
      card.append(art, info); card.addEventListener('click', () => open(item.id)); return card;
    }));
    if (focused && !root.contains(document.activeElement)) [...root.querySelectorAll('button')].find(el => el.getAttribute('aria-label') === focused)?.focus({preventScroll: true});
  }
  async function mutate(item, operation) {
    try {await api(`/extensions/score-lab/api/samples/${item.id}/${operation}`, {}); await refresh();}
    catch (error) {onError(error);}
  }
  async function enable(value) {
    try {description = await api('/api/extensions/score-lab', {enabled: value}); if (!value) home(); await refresh();}
    catch (error) {onError(error);}
  }
  async function chooseBundle() {
    if (!window.usicCapabilities?.chooseScoreBundle) {toast('请在小黄花客户端中选择能力包'); return;}
    const el = $('scores-setup-action'); el.disabled = true;
    try {
      if (samples.some(s => ['queued', 'running'].includes(s.status.state))) {toast('请先完成或取消识谱'); return;}
      el.textContent = '校验中…';
      const result = await window.usicCapabilities.chooseScoreBundle();
      if (!result.cancelled) {await api('/api/extensions/score-lab/reload', {}); await enable(true); toast('谱室已就绪');}
    } catch {toast('能力包未通过校验');}
    finally {el.disabled = false; paint();}
  }
  function home() {opened = false; frame.removeAttribute('src'); $('scores-workbench').hidden = true; $('scores-overview').hidden = false;}
  function open(id) {opened = true; $('scores-overview').hidden = true; $('scores-workbench').hidden = false;
    frame.src = `/extensions/score-lab/?embedded=1&sample=${encodeURIComponent(id)}`;}
  async function openTrack(key) {
    selectedTrack = getTrack(key); if (!selectedTrack) return;
    if (!description?.enabled) {await enable(true);}
    if (!description?.runtimeReady) {home(); showView('score'); toast('请选择谱室能力包'); return;}
    const current = getCurrent();
    seconds = 30; $('score-submit-offset').value = Math.floor(Math.max(0, current?.key === key ? current.position || 0 : 0));
    $('score-submit-title').textContent = selectedTrack.title;
    $('score-submit-artist').textContent = selectedTrack.artist;
    chooseSeconds(30); dialog.showModal();
  }
  function chooseSeconds(value) {
    seconds = value; dialog.querySelectorAll('[data-seconds]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.seconds === String(value))));
    $('score-submit-offset').disabled = value === 'full';
  }
  dialog.querySelector('header button').addEventListener('click', () => dialog.close());
  dialog.querySelector('.score-cancel').addEventListener('click', () => dialog.close());
  dialog.querySelectorAll('[data-seconds]').forEach(el => el.addEventListener('click', () => chooseSeconds(el.dataset.seconds === 'full' ? 'full' : Number(el.dataset.seconds))));
  dialog.querySelector('form').addEventListener('submit', async event => {
    event.preventDefault(); $('score-submit-start').disabled = true;
    try {
      await api('/api/scores/track', {key: selectedTrack.key, seconds, offset: Number($('score-submit-offset').value)});
      dialog.close(); toast('已加入谱室'); await refresh();
    } catch (error) {onError(error);} finally {$('score-submit-start').disabled = false;}
  });
  $('scores-enabled').addEventListener('click', () => enable(!description?.enabled));
  $('scores-setup-action').addEventListener('click', () => description?.enabled ? chooseBundle() : enable(true));
  $('scores-bundle').addEventListener('click', chooseBundle);
  $('scores-import').addEventListener('click', () => {open(''); frame.addEventListener('load', () => frame.contentWindow.postMessage({type: 'usic-score:import'}, location.origin), {once: true});});
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
    if (event.data?.type === 'usic-score:home') {home(); refresh();}
    if (event.data?.type === 'usic-score:play') pausePlayer();
    if (event.data?.type === 'usic-score:queued') {home(); refresh(); toast('已加入谱室');}
  });
  window.addEventListener('pagehide', () => clearTimeout(timer));
  refresh();
  return {openTrack, pause: () => frame.contentWindow?.postMessage({type: 'usic-score:pause'}, location.origin),
    render(active) {
      root.hidden = !active; $('content').classList.toggle('has-score-extension', active);
      if (!active && opened) home();
    }};
}
