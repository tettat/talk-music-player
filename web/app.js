import {ListeningTracker} from '/listening.mjs';
import {installWindowControls} from '/window-controls.mjs';
installWindowControls();
// Bootstrap compatibility: an older running server knows the original static
// modules only. Load the new module after its feature handshake, never before it.
let sourceName = source => ({qq: 'QQ', netease: '网易'}[source] || '—');
let trackSource = track => track?.source || (track?.key?.startsWith('netease:') ? 'netease' : 'qq');
let groupTracks = records => records.map(track => ({id: track.key, selected: track, variants: [track], allVariants: [track]}));
let enqueueVersion = (queue, key, tracks, {next = false, current = null} = {}) => {
  const result = queue.filter(k => k !== key);
  if (next && current && current !== key) result.splice(Math.max(0, result.indexOf(current)+1), 0, key);
  else result.push(key);
  return {queue: result, blocked: false};
};
let mergeSearchPage, interleaveResults;
const $ = id => document.getElementById(id);
const audio = $('audio');
const csrf = document.querySelector('meta[name="talk-csrf"]').content;
const fallback = '/assets/cover-default.svg';
const tracks = new Map();
let library = [], favorites = new Set(), playlists = [];
let state = {queue: [], current: null, position: 0, volume: .8, mode: 'repeat'};
let view = 'library', results = [], query = '', hasMore = false;
let account = {}, loading = null, playGeneration = 0, heroKey = null;
let sourceKey = null, resumePosition = 0, pendingTrack = null, editingPlaylist = null;
let authTimer, toastTimer, searchGeneration = 0, stateTimer, saving = Promise.resolve();
let loadingSearch = false;
let sourceFilter = 'all', searchSources = {}, displayQuery = '', preservingResults = false;
let aggregationReady = false, legacyPage = 1;
let providerResults = {qq: [], netease: []};
const searchSelections = new Map();
let statsDays = 30, statsGeneration = 0, statsTimer, historyOffset = 0, historyItems = [];
let statsSource = 'all';
let detailKey = null, detailTab = 'lyrics', detailGeneration = 0, lyricsData = null, lyricCurrent = -1;
let lyricsFollow = true, analysisData = null;
let listenWarningAt = 0;
const lyricCache = new Map(), analysisCache = new Map();
let desktopLyrics = {update() {}, lyricsChanged() {}, dispose() {}};
let scoreModule = {render() {}, openTrack() {}, pause() {}};
let scoreReady = false;

const messages = {
  LOGIN_REQUIRED: '请连接账号', SOURCE_UNAVAILABLE: '这首歌暂不可播放',
  TRACK_IDENTITY_MISMATCH: '歌曲信息已更新，请重新搜索',
  NETWORK_TIMEOUT: '连接超时，请重试', PROVIDER_UNAVAILABLE: '音乐服务暂不可用',
  BUSY: '正在连接，请稍后', PLAYLIST_LIMIT: '列表已满',
  MEDIA_EXPIRED: '请重新播放', PLAYER_OFFLINE: '播放器已离线，请重新打开',
  LYRICS_UNAVAILABLE: '歌词暂不可用', LYRICS_INVALID: '歌词格式不可用', LYRICS_LIMIT: '歌词过长',
  ANALYSIS_UNAVAILABLE: '分析暂不可用', ANALYSIS_BUSY: '正在分析，请稍后',
  ANALYSIS_FAILED: '分析未完成，请重试', ANALYSIS_TIMEOUT: '分析超时，请重试',
  NETEASE_UNAVAILABLE: '网易云连接失败，请重试', NETEASE_SOURCE_UNAVAILABLE: '网易云暂未提供这首歌的完整音源',
  NETEASE_FREE_ONLY: '网易云暂只支持免费歌曲', CAPABILITY_UNAVAILABLE: '暂不支持此操作',
  SCORE_DISABLED: '请先启用谱室', SCORE_RUNTIME_MISSING: '请选择谱室能力包',
  SCORE_BUSY: '请等待当前任务结束', SCORE_FAILED: '识谱未完成，请重试',
  SCORE_UNAVAILABLE: '谱室暂不可用', SCORE_INPUT_MISSING: '请重新导入音频',
  SCORE_NOT_RETRYABLE: '当前任务不可重试', SCORE_AUDIO_LIMIT: '音频须在 64 MB 以内',
};

async function api(path, data) {
  let response;
  try {
    response = await fetch(path, data === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Talk-CSRF': csrf},
      body: JSON.stringify(data), cache: 'no-store',
    });
  } catch { throw Object.assign(new Error(), {code: 'PLAYER_OFFLINE'}); }
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(), {code: result.code || 'PROVIDER_UNAVAILABLE'});
  return result;
}
const listening = new ListeningTracker(audio, {
  send: async (packet, {keepalive}) => {
    const response = await fetch('/api/listen', {method: 'POST', keepalive,
      headers: {'Content-Type': 'application/json', 'X-Talk-CSRF': csrf}, body: JSON.stringify(packet)});
    if (!response.ok) throw Object.assign(new Error(), {code: 'LISTEN_SAVE_FAILED'});
  },
  onSaved: () => {
    if (view === 'insights') {clearTimeout(statsTimer); statsTimer = setTimeout(() => loadStats(false), 350);}
  },
  onError: () => {if (Date.now()-listenWarningAt > 60000) {listenWarningAt = Date.now(); toast('听歌记录暂未保存');}},
});
setInterval(() => listening.tick(), 1000);

function node(tag, className = '', value) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (value !== undefined) el.textContent = value;
  return el;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(svg.namespaceURI, 'use');
  use.setAttribute('href', '#i-' + name); svg.append(use); return svg;
}
function setIcon(el, name) { el.replaceChildren(icon(name)); }
function button(name, label, action, className = 'icon-btn') {
  const el = node('button', className);
  el.type = 'button'; el.title = label; el.setAttribute('aria-label', label);
  el.append(icon(name)); el.addEventListener('click', event => {event.stopPropagation(); action(event);});
  return el;
}
function cover(track, className = '') {
  const el = node('img', className); el.src = track?.cover || fallback;
  el.alt = ''; el.loading = 'lazy'; el.addEventListener('error', () => {el.src = fallback;}, {once: true});
  return el;
}
function time(seconds) {
  seconds = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden = true;}, 2600);
}
function fail(error) {
  toast(messages[error.code] || '操作未完成，请重试');
  if (error.code === 'LOGIN_REQUIRED') openAuth(false);
}
function ingest(data, preserveState = false) {
  data.tracks?.forEach(t => tracks.set(t.key, t));
  library = data.library ?? library;
  favorites = new Set(data.favorites ?? favorites);
  playlists = data.playlists ?? playlists;
  if (data.state && !preserveState) state = data.state;
  if (data.account) account = data.account;
}
function visibleKeys() {
  if (view === 'search') return searchGroups().map(group => group.selected.key);
  if (view === 'favorites') return [...favorites];
  if (view.startsWith('playlist:')) return playlists.find(p => p.id === view.slice(9))?.keys || [];
  return library;
}
function searchGroups() {
  return groupTracks(results.map(key => tracks.get(key)).filter(Boolean),
    {selections: searchSelections, source: sourceFilter, current: state.current});
}
function sourceTag(track) {
  const tag = node('span', 'source-tag', sourceName(trackSource(track)));
  tag.title = trackSource(track) === 'netease' ? '网易云音乐' : 'QQ 音乐';
  return tag;
}
function artistWithSource(element, value, track) {
  element.replaceChildren(node('span', 'artist-text', value));
  element.classList.add('has-source');
  if (track) element.append(sourceTag(track));
}
function sourceCell(group) {
  const cell = node('div', 'row-source');
  const selected = group.selected;
  if (group.variants.length > 1) {
    const control = node('div', 'source-switch'); control.setAttribute('role', 'group');
    control.setAttribute('aria-label', '选择来源：' + selected.title);
    control.title = '歌名、歌手、专辑和时长匹配';
    for (const track of [...group.variants].sort((a, b) => trackSource(a) === 'qq' ? -1 : 1)) {
      const choice = node('button', 'source-choice', sourceName(trackSource(track))); choice.type = 'button';
      choice.dataset.key = track.key;
      const label = '使用' + sourceName(trackSource(track)) + '版本';
      choice.title = label; choice.setAttribute('aria-label', label);
      choice.setAttribute('aria-pressed', track.key === selected.key); choice.tabIndex = track.key === selected.key ? 0 : -1;
      choice.classList.toggle('is-restricted', ['requires_login', 'unavailable'].includes(track.availability));
      choice.classList.toggle('is-playing-source', track.key === sourceKey && !audio.paused);
      const choose = () => {
        searchSelections.set(group.id, track.key); renderList();
        const row = [...$('track-list').children].find(row => row.dataset.group === group.id);
        [...row?.querySelectorAll('.source-choice') || []].find(button => button.dataset.key === track.key)?.focus();
      };
      choice.addEventListener('click', choose);
      choice.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        searchSelections.set(group.id, group.variants.find(t => t.key !== track.key).key);
        renderList();
        const row = [...$('track-list').children].find(row => row.dataset.group === group.id);
        row?.querySelector('.source-choice[aria-pressed="true"]')?.focus();
      });
      control.append(choice);
    }
    cell.append(control);
  } else cell.append(sourceTag(selected));
  if (selected.availability === 'requires_login' || selected.availability === 'unavailable') {
    cell.append(node('span', 'availability-tag', selected.availability === 'requires_login' ? '需授权' : '不可播'));
  }
  return cell;
}
function renderSearchStatus() {
  $('search-source-filter').hidden = view !== 'search' || !aggregationReady;
  const status = $('search-source-status'); status.replaceChildren(); status.hidden = view !== 'search' || !aggregationReady;
  document.querySelectorAll('[data-search-source]').forEach(button => {
    const active = button.dataset.searchSource === sourceFilter;
    button.setAttribute('aria-pressed', active); button.tabIndex = active ? 0 : -1;
  });
  if (view !== 'search' || !aggregationReady) return;
  for (const source of ['qq', 'netease']) {
    if (sourceFilter !== 'all' && sourceFilter !== source) continue;
    const info = searchSources[source]; if (!info) continue;
    const item = node('span', 'source-state');
    item.append(node('span', 'source-state-name', sourceName(source)));
    if (info.status === 'loading') item.append(node('span', 'source-state-text', '搜索中…'));
    else if (info.status === 'error') {
      item.classList.add('has-error');
      const needsLogin = source === 'qq' && info.code === 'LOGIN_REQUIRED';
      item.append(node('span', 'source-state-text', needsLogin ? '未连接' : info.code === 'BUSY' ? '忙碌中' : '连接失败'));
      const retry = node('button', 'source-retry', needsLogin ? '连接' : '重试'); retry.type = 'button';
      retry.setAttribute('aria-label', needsLogin ? '连接 QQ 账号' : '重试' + sourceName(source) + '搜索');
      retry.disabled = loadingSearch && !needsLogin;
      retry.addEventListener('click', () => needsLogin ? openAuth() : search(true, source)); item.append(retry);
    } else {
      const count = results.filter(key => trackSource(tracks.get(key)) === source).length;
      item.append(node('span', 'source-state-text', count ? String(count) : '无匹配'));
    }
    status.append(item);
  }
  const groups = searchGroups(), duplicates = groups.filter(group => group.variants.length > 1).length;
  if (duplicates) status.append(node('span', 'source-merged', '已合并 ' + duplicates + ' 组'));
}
function persist(immediate = false) {
  clearTimeout(stateTimer);
  const save = () => {
    const payload = structuredClone(state);
    saving = saving.catch(() => {}).then(() => api('/api/state', payload)).catch(error => {
      if (error.code !== 'PLAYER_OFFLINE') fail(error);
    });
  };
  if (immediate) save(); else stateTimer = setTimeout(save, 300);
}
function showView(next) {
  closeMenu(); view = next;
  if (next !== 'search') $('search-input').value = '';
  $('content').scrollTop = 0; render();
  if (next === 'insights') loadStats(true);
}
function render() {
  renderNavigation(); renderList(); renderPlayer(); renderQueue(); renderAccount();
}
function renderNavigation() {
  document.querySelectorAll('[data-view]').forEach(el => {
    el.classList.toggle('is-active', el.dataset.view === view);
    el.setAttribute('aria-current', el.dataset.view === view ? 'page' : 'false');
  });
  const list = $('playlist-list'); list.replaceChildren();
  for (const p of playlists) {
    const li = node('li');
    const row = node('div', 'playlist-nav-row');
    const open = button('music', p.name, () => showView('playlist:' + p.id), 'playlist-item');
    open.append(node('span', 'playlist-name', p.name));
    open.classList.toggle('is-active', view === 'playlist:' + p.id);
    row.append(open, button('more', '管理' + p.name, event => playlistMenu(p, event), 'icon-btn playlist-more'));
    li.append(row); list.append(li);
  }
}
function renderList() {
  renderSearchStatus();
  const insights = view === 'insights';
  const scores = view === 'score';
  scoreModule.render(scores);
  $('insights-view').hidden = !insights;
  for (const el of [$('hero'), document.querySelector('.list-head'), $('track-list'), $('empty-state'), $('load-more')]) el.hidden = insights || scores;
  if (insights || scores) {heroKey = null; $('view-actions').hidden = true; $('search-source-status').hidden = true; return;}
  const groups = view === 'search' ? searchGroups() : visibleKeys().filter(key => tracks.has(key))
    .map(key => ({id: key, selected: tracks.get(key), variants: [tracks.get(key)], allVariants: [tracks.get(key)]}));
  const keys = groups.map(group => group.selected.key);
  const playlist = view.startsWith('playlist:') && playlists.find(p => p.id === view.slice(9));
  $('view-actions').hidden = !playlist;
  $('page-title').textContent = view === 'search' ? displayQuery || query : view === 'favorites' ? '收藏' : playlist ? playlist.name : '音乐库';
  $('track-count').textContent = loadingSearch ? '' : `${keys.length}`;
  $('track-list').classList.toggle('is-loading', preservingResults);
  $('track-list').setAttribute('aria-busy', view === 'search' && loadingSearch);
  $('empty-state').hidden = Boolean(keys.length) || loadingSearch;
  const failed = Object.entries(searchSources).filter(([name]) => sourceFilter === 'all' || sourceFilter === name);
  $('empty-state').querySelector('.empty-label').textContent = view === 'search'
    ? failed.length && failed.every(([, info]) => info.status === 'error') ? '搜索未完成' : '没有找到匹配歌曲' : '暂无';
  const list = $('track-list'); list.replaceChildren();
  for (const [index, group] of groups.entries()) {
    const track = group.selected, key = track.key, label = track.title + '（' + sourceName(trackSource(track)) + '）';
    const row = node('li', 'track-row'); row.dataset.key = key; row.dataset.group = group.id;
    row.classList.toggle('is-active', group.allVariants.some(t => t.key === state.current));
    row.classList.toggle('is-playing', group.allVariants.some(t => t.key === sourceKey) && !audio.paused);
    row.classList.toggle('is-blocked', track.availability === 'unavailable');
    const start = () => key === state.current && (sourceKey || loading) ? toggle() : playTrack(key, keys);
    const play = button('play', '播放 ' + label, start, 'row-index');
    play.disabled = track.availability === 'unavailable';
    const number = node('span', 'row-number', String(index + 1).padStart(2, '0'));
    play.prepend(number);
    const art = cover(track, 'row-cover');
    const info = node('div', 'row-info');
    const title = node('button', 'row-title', track.title); title.type = 'button';
    title.title = label; title.addEventListener('click', start);
    info.append(title, node('span', 'row-artist', track.artist));
    const album = node('span', 'row-album', track.album); album.title = track.album;
    const like = button('heart', (favorites.has(key) ? '取消收藏 ' : '收藏 ') + label,
      () => favorite(key), 'icon-btn row-like');
    like.classList.toggle('is-favorite', favorites.has(key)); like.setAttribute('aria-pressed', favorites.has(key));
    row.append(play, art, info, album, sourceCell(group), node('span', 'row-duration', time(track.duration)), like,
      button('more', '更多 ' + label, event => trackMenu(key, event), 'icon-btn row-more'));
    row.addEventListener('dblclick', event => {
      if (!event.target.closest('button')) start();
    });
    list.append(row);
  }
  if (aggregationReady) hasMore = Object.entries(searchSources).some(([name, info]) => (sourceFilter === 'all' || sourceFilter === name) && info.hasMore);
  $('load-more').hidden = view !== 'search' || !hasMore || loadingSearch;
  heroKey = keys.includes(state.current) ? state.current : keys[0] || null;
  $('hero').hidden = !heroKey;
  if (heroKey) {
    const track = tracks.get(heroKey);
    $('hero-title').textContent = track.title;
    artistWithSource($('hero-artist'), track.artist + (track.album !== '—' ? ' · ' + track.album : ''), track);
    $('hero-cover').src = track.cover || fallback;
    $('hero-like').classList.toggle('is-favorite', favorites.has(heroKey));
    $('hero-like').setAttribute('aria-pressed', favorites.has(heroKey));
    setIcon($('hero-play'), loading === heroKey ? 'loader' : heroKey === state.current && !audio.paused ? 'pause' : 'play');
    $('hero-play').setAttribute('aria-label', loading === heroKey ? '取消加载' : heroKey === state.current && !audio.paused ? '暂停' : '播放');
  }
}
function renderPlayer() {
  const track = tracks.get(state.current);
  $('player-title').textContent = track?.title || '—';
  artistWithSource($('player-artist'), track?.artist || '', track);
  $('player-cover').src = track?.cover || fallback;
  $('player-like').disabled = !track;
  if ($('player-score')) $('player-score').disabled = !track;
  $('detail-toggle').disabled = !track && !heroKey;
  $('mobile-detail-toggle').disabled = !track && !heroKey;
  $('player-like').classList.toggle('is-favorite', favorites.has(state.current));
  $('player-like').setAttribute('aria-pressed', favorites.has(state.current));
  setIcon($('play-toggle'), loading ? 'loader' : audio.paused ? 'play' : 'pause');
  $('play-toggle').setAttribute('aria-label', loading ? '取消加载' : audio.paused ? '播放' : '暂停');
  $('play-toggle').title = $('play-toggle').getAttribute('aria-label');
  $('previous').disabled = !state.queue.length;
  $('next').disabled = !state.queue.length;
  $('seek').disabled = !sourceKey || !Number.isFinite(audio.duration);
  const modeLabel = {repeat: '列表循环', single: '单曲循环', shuffle: '随机播放'}[state.mode];
  setIcon($('play-mode'), state.mode === 'shuffle' ? 'shuffle' : 'repeat');
  $('play-mode').classList.toggle('is-active', state.mode === 'single');
  $('play-mode').setAttribute('aria-label', modeLabel); $('play-mode').title = modeLabel;
  $('volume').value = state.volume;
  $('volume').style.setProperty('--p', state.volume * 100 + '%');
  updateProgress();
  updateLyricPosition();
  desktopLyrics.update();
}
function updateProgress() {
  const duration = Number.isFinite(audio.duration) ? audio.duration : tracks.get(state.current)?.duration || 0;
  const position = sourceKey ? audio.currentTime : state.position;
  $('elapsed').textContent = time(position); $('duration').textContent = time(duration);
  if (document.activeElement !== $('seek')) {
    $('seek').value = duration ? position / duration * 100 : 0;
    $('seek').style.setProperty('--p', $('seek').value + '%');
  }
}
function renderQueue() {
  if (!$('queue-panel').hidden && !$('detail-panel').hidden) closeDetail(false);
  $('queue-toggle').classList.toggle('is-active', !$('queue-panel').hidden);
  $('queue-toggle').setAttribute('aria-expanded', !$('queue-panel').hidden);
  $('mobile-queue-toggle').classList.toggle('is-active', !$('queue-panel').hidden);
  $('mobile-queue-toggle').setAttribute('aria-expanded', !$('queue-panel').hidden);
  $('queue-list').replaceChildren();
  for (const key of state.queue) {
    const track = tracks.get(key); if (!track) continue;
    const row = node('li', 'queue-item'); row.dataset.key = key;
    row.classList.toggle('is-active', state.current === key);
    const main = node('button', 'queue-song'); main.type = 'button';
    const info = node('span', 'queue-info');
    const artist = node('span', 'queue-artist'); artistWithSource(artist, track.artist, track);
    info.append(node('span', 'queue-name', track.title), artist);
    main.append(cover(track, 'queue-cover'), info);
    main.addEventListener('click', () => playTrack(key));
    row.append(main, button('close', '移出队列 ' + track.title, () => {
      state.queue = state.queue.filter(k => k !== key); persist(); renderQueue();
    }, 'icon-btn queue-remove'));
    $('queue-list').append(row);
  }
  $('queue-clear').disabled = !state.queue.length;
  $('previous').disabled = !state.queue.length;
  $('next').disabled = !state.queue.length;
}
async function favorite(key) {
  if (!key) return;
  try { ingest(await api('/api/favorite', {key, enabled: !favorites.has(key)}), true); render(); }
  catch (error) {fail(error);}
}
async function search(append = false, retrySource = null) {
  const value = $('search-input').value.trim(); if (!value) {showView('library'); return;}
  if (!aggregationReady) {
    // A currently running pre-upgrade backend can still serve new static files.
    // Keep its QQ workflow usable until the user deliberately restarts Electron.
    const generation = ++searchGeneration; query = value; displayQuery = value; view = 'search'; loadingSearch = true;
    if (!append) {results = []; legacyPage = 1; hasMore = false;}
    $('search-form').classList.add('is-loading'); render();
    try {
      const data = await api('/api/search', {query: value, page: append ? legacyPage+1 : 1});
      if (generation !== searchGeneration) return;
      data.tracks.forEach(track => tracks.set(track.key, track));
      results = [...new Set([...results, ...data.tracks.map(track => track.key)])];
      legacyPage = append ? legacyPage+1 : 1; hasMore = data.hasMore;
    } catch (error) {if (generation === searchGeneration) fail(error);}
    finally {if (generation === searchGeneration) {loadingSearch = false; $('search-form').classList.remove('is-loading'); render();}}
    return;
  }
  if (append && value !== query) {append = false; retrySource = null;}
  const names = retrySource ? [retrySource] : append
    ? ['qq', 'netease'].filter(name => (sourceFilter === 'all' || sourceFilter === name) && searchSources[name]?.hasMore)
    : ['qq', 'netease'];
  if (!names.length) return;
  const generation = ++searchGeneration;
  query = value; view = 'search'; loadingSearch = true;
  let replaced = append;
  if (!append) {sourceFilter = 'all'; searchSelections.clear(); searchSources = {}; hasMore = false;}
  preservingResults = !append && Boolean(results.length);
  const pages = Object.fromEntries(names.map(name => [name, append ? (searchSources[name]?.page || 0)+1 : 1]));
  for (const name of names) searchSources[name] = {...searchSources[name], status: 'loading'};
  $('search-form').classList.add('is-loading'); render();
  try {
    await Promise.all(names.map(async name => {
      try {
        let data;
        // Initial account verification or a discarded older search can still
        // own that source's lock. Retry only BUSY, never switch platforms.
        for (let attempt = 0; attempt < 8; attempt++) {
          if (generation !== searchGeneration) return;
          data = await api('/api/search', {query: value, source: 'all', pages: {[name]: pages[name]}});
          if (data.sources?.[name]?.code !== 'BUSY' || attempt === 7) break;
          await new Promise(resolve => setTimeout(resolve, 350));
        }
        if (generation !== searchGeneration) return;
        if (data.sources?.[name]?.status === 'ready') {
          if (!replaced) {results = []; providerResults = {qq: [], netease: []}; displayQuery = value; replaced = true; preservingResults = false;}
          data.tracks.forEach(track => tracks.set(track.key, track));
          providerResults[name] = [...new Set([...providerResults[name], ...data.tracks.map(track => track.key)])];
          results = interleaveResults(providerResults);
        }
        searchSources = mergeSearchPage(searchSources, data.sources);
        if (data.sources?.qq?.code === 'LOGIN_REQUIRED') account.session = 'required';
      } catch (error) {
        if (generation !== searchGeneration) return;
        searchSources = mergeSearchPage(searchSources, {[name]: {status: 'error', code: error.code || 'PROVIDER_UNAVAILABLE'}});
      } finally {
        if (generation === searchGeneration) {
          loadingSearch = Object.values(searchSources).some(info => info.status === 'loading'); render();
        }
      }
    }));
  } finally {
    if (generation === searchGeneration) {
      loadingSearch = false; preservingResults = false; $('search-form').classList.remove('is-loading'); render();
    }
  }
}
async function playTrack(key, queue, restore = false) {
  if (!tracks.has(key)) return;
  if (loading === key) return;
  const selected = tracks.get(key);
  if (selected.availability === 'unavailable') {toast('这首歌暂不可播放'); return;}
  if (trackSource(selected) === 'netease' && selected.availability === 'requires_login') {toast(messages.NETEASE_FREE_ONLY); return;}
  const group = searchGroups().find(group => group.allVariants.some(track => track.key === key));
  if (group) searchSelections.set(group.id, key);
  const generation = ++playGeneration;
  listening.setTrack(key, tracks.get(key).duration);
  audio.pause(); audio.removeAttribute('src'); audio.load(); sourceKey = null;
  loading = key; resumePosition = restore ? state.position : 0;
  state.current = key; state.position = resumePosition;
  if (queue?.length) state.queue = [...new Set(queue)].filter(key => {
    const track = tracks.get(key);
    return track && track.availability !== 'unavailable' && !(trackSource(track) === 'netease' && track.availability === 'requires_login');
  });
  else state.queue = enqueueVersion(state.queue, key, tracks, {current: state.current}).queue;
  persist(); render();
  if (!$('detail-panel').hidden) openDetail(key, detailTab);
  try {
    let started;
    // A discarded previous source request may still be finishing its bounded fetch.
    // Keep the latest user intent cancellable instead of playing a stale song.
    for (let attempt = 0; attempt < 220; attempt++) {
      if (generation !== playGeneration) return;
      try {started = await api('/api/play', {key}); break;}
      catch (error) {
        if (error.code !== 'BUSY') throw error;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    }
    if (!started) throw Object.assign(new Error(), {code: 'NETWORK_TIMEOUT'});
    const {job} = started;
    let ready;
    for (let n = 0; n < 250; n++) {
      if (generation !== playGeneration) return;
      ready = await api('/api/jobs/' + job);
      if (ready.status === 'ready') break;
      if (ready.status === 'error') throw Object.assign(new Error(), {code: ready.code});
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    if (generation !== playGeneration) return;
    if (ready.status !== 'ready') throw Object.assign(new Error(), {code: 'NETWORK_TIMEOUT'});
    tracks.set(key, ready.track);
    library = [key, ...library.filter(k => k !== key)];
    sourceKey = key; audio.src = ready.mediaUrl; audio.volume = state.volume; audio.load();
    try {await audio.play();} catch {if (generation === playGeneration) toast('点按播放');}
    if (generation !== playGeneration) return;
    if ('mediaSession' in navigator && window.MediaMetadata) {
      navigator.mediaSession.metadata = new MediaMetadata({title: ready.track.title, artist: ready.track.artist,
        album: ready.track.album, artwork: [{src: ready.track.cover, sizes: '300x300'}]});
    }
  } catch (error) {if (generation === playGeneration) {listening.setTrack(null); fail(error);}}
  finally {if (generation === playGeneration) {loading = null; render();}}
}
async function toggle() {
  if (loading) {
    ++playGeneration; loading = null; sourceKey = null;
    listening.setTrack(null);
    audio.pause(); audio.removeAttribute('src'); audio.load(); render(); return;
  }
  if (!sourceKey) {
    const key = state.current || visibleKeys()[0];
    if (key) await playTrack(key, state.queue.length ? null : visibleKeys(), Boolean(state.current));
    return;
  }
  if (audio.paused) {try {await audio.play();} catch {toast('请重新播放');}}
  else audio.pause();
}
function advance(direction = 1, ended = false) {
  const queue = state.queue.filter(key => tracks.has(key)); if (!queue.length) return;
  if (ended && state.mode === 'single' && sourceKey) {audio.currentTime = 0; audio.play().catch(fail); return;}
  if (direction < 0 && audio.currentTime > 3) {audio.currentTime = 0; return;}
  const index = queue.indexOf(state.current);
  let next = index < 0 ? (direction > 0 ? 0 : queue.length - 1) : (index + direction + queue.length) % queue.length;
  if (state.mode === 'shuffle' && queue.length > 1) {
    const others = queue.filter(key => key !== state.current); playTrack(others[Math.floor(Math.random() * others.length)]); return;
  }
  playTrack(queue[next]);
}

// Small contextual menus, no prose or always-visible management panels.
const menu = node('div', 'context-menu'); menu.hidden = true; menu.setAttribute('role', 'menu'); document.body.append(menu);
function closeMenu() {menu.hidden = true; menu.replaceChildren();}
function showMenu(event, actions) {
  closeMenu();
  for (const [symbol, label, action] of actions) {
    const el = button(symbol, label, () => {closeMenu(); action();}, 'menu-action');
    el.setAttribute('role', 'menuitem'); el.append(node('span', '', label)); menu.append(el);
  }
  const rect = event.currentTarget.getBoundingClientRect();
  menu.hidden = false;
  menu.style.left = Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 12) + 'px';
  menu.style.top = Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - menu.offsetHeight - 12)) + 'px';
  if (parseFloat(menu.style.left) < 12) menu.style.left = '12px';
  menu.querySelector('button')?.focus();
}
function enqueue(key, next) {
  const track = tracks.get(key);
  if (trackSource(track) === 'netease' && track.availability === 'requires_login') {toast(messages.NETEASE_FREE_ONLY); return;}
  const result = enqueueVersion(state.queue, key, tracks, {next, current: state.current, playing: !audio.paused || Boolean(loading)});
  if (result.blocked) {toast('正在播放另一来源版本'); return;}
  state.queue = result.queue;
  persist(); renderQueue(); toast(next ? '已设为下一首' : '已加入队列');
}
function trackMenu(key, event) {
  const actions = [['next', '下一首播放', () => enqueue(key, true)],
    ['queue', '加入队列', () => enqueue(key, false)], ['plus', '加至列表', () => openAdd(key)]];
  if (scoreReady) actions.push(['score', '识谱', () => scoreModule.openTrack(key)]);
  actions.push(['detail', '歌词', () => openDetail(key, 'lyrics')], ['chart', '分析', () => openDetail(key, 'analysis')]);
  if (view.startsWith('playlist:')) actions.push(['close', '从列表移除', () => playlistAction(view.slice(9), 'remove', key)]);
  showMenu(event, actions);
}
function playlistMenu(p, event) {
  showMenu(event, [['music', '打开', () => showView('playlist:' + p.id)],
    ['more', '重命名', () => openPlaylist(p)], ['close', '删除列表', () => {
      pendingDelete = p; $('delete-name').textContent = p.name; $('delete-dialog').showModal();
    }]]);
}
let pendingDelete;
function openPlaylist(p = null) {
  editingPlaylist = p?.id || null;
  $('playlist-dialog-title').textContent = p ? '重命名' : '新建列表';
  $('playlist-name').value = p?.name || '';
  $('playlist-dialog').showModal(); $('playlist-name').focus();
}
async function playlistAction(id, action, value) {
  try {
    ingest(await api('/api/playlist', {id, action, value}), true);
    if (view === 'playlist:' + id && action === 'delete') view = 'library';
    render(); return true;
  } catch (error) {fail(error); return false;}
}
function openAdd(key) {
  $('add-dialog-title').textContent = '加至列表';
  pendingTrack = key; const list = $('add-list'); list.replaceChildren();
  const create = button('plus', '新建列表', () => {$('add-dialog').close(); openPlaylist();}, 'add-item');
  create.append(node('span', '', '新建列表')); list.append(node('li')); list.lastChild.append(create);
  for (const p of playlists) {
    const exists = p.keys.includes(key);
    const el = button(exists ? 'check' : 'music', p.name, async () => {
      if (await playlistAction(p.id, 'add', key)) {$('add-dialog').close(); toast('已加入列表');}
    }, 'add-item');
    el.append(node('span', '', p.name)); el.disabled = exists;
    const li = node('li'); li.append(el); list.append(li);
  }
  $('add-dialog').showModal();
}

function renderAccount() {
  $('auth-button').classList.toggle('is-connected', account.session === 'valid');
  $('auth-button').title = account.session === 'valid' ? '已连接 · QQ 音乐' : '连接 QQ 音乐';
  $('auth-button').setAttribute('aria-label', $('auth-button').title);
}
async function pollAuth() {
  try {
    account = await api('/api/account'); renderAccount();
    const labels = {idle: account.session === 'valid' ? '已连接' : account.session === 'saved' ? '已保存授权' : '未连接',
      creating: '正在连接…', scan: '请用' + ({qq: ' QQ', wx: '微信', mobile: ' QQ 音乐'}[account.provider] || '') + '扫码',
      confirm: '请确认授权', done: '已连接', expired: '二维码已过期', refused: '已取消', failed: '连接未完成，请重试'};
    $('auth-status').textContent = labels[account.auth] || labels.idle;
    const busy = ['creating', 'scan', 'confirm'].includes(account.auth);
    $('auth-qq').textContent = busy ? 'QQ' : account.session === 'valid' ? '重新授权' : 'QQ 扫码';
    document.querySelector('.auth-qr-wrap').hidden = !busy && !account.qrReady;
    for (const type of ['qq', 'wx', 'mobile']) {
      $('auth-' + type).disabled = busy;
      $('auth-' + type).classList.toggle('is-active', type === account.provider);
    }
    if (account.qrReady) {
      const src = '/api/auth/qr?g=' + account.generation;
      if ($('auth-qr').getAttribute('src') !== src) $('auth-qr').src = src;
    } else $('auth-qr').removeAttribute('src');
    if (account.auth === 'done') {
      if (trackSource(tracks.get(state.current)) === 'qq') {
        ++playGeneration; loading = null; sourceKey = null;
        listening.setTrack(null);
        audio.pause(); audio.removeAttribute('src'); audio.load(); renderPlayer();
      }
      clearTimeout(authTimer);
      setTimeout(() => {
        $('auth-dialog').close();
        if (view === 'search' && !loadingSearch && searchSources.qq?.code === 'LOGIN_REQUIRED') search(true, 'qq');
      }, 600); return;
    }
  } catch (error) {$('auth-status').textContent = messages[error.code] || '连接未完成';}
  if ($('auth-dialog').open) authTimer = setTimeout(pollAuth, 1200);
}
function openAuth() {
  if (!$('auth-dialog').open) $('auth-dialog').showModal();
  clearTimeout(authTimer); pollAuth();
}
async function startAuth(provider) {
  try {
    if (trackSource(tracks.get(state.current)) === 'qq') audio.pause();
    await api('/api/auth/start', {provider});
    clearTimeout(authTimer); pollAuth();
  } catch (error) {fail(error);}
}

// Complete the static Kimi skeleton with terse functional controls only.
const more = node('button', 'btn-ghost load-more', '更多'); more.type = 'button'; more.id = 'load-more'; more.hidden = true;
more.addEventListener('click', () => search(true)); $('content').append(more);
const deleteDialog = node('dialog', 'dialog'); deleteDialog.id = 'delete-dialog';
const deleteHeading = node('h2', 'dialog-title', '删除列表');
deleteHeading.id = 'delete-heading'; deleteDialog.setAttribute('aria-labelledby', 'delete-heading');
const deleteName = node('p', 'delete-name'); deleteName.id = 'delete-name';
const deleteActions = node('div', 'dialog-actions');
const cancelDelete = node('button', 'btn-ghost', '取消'); cancelDelete.type = 'button'; cancelDelete.onclick = () => deleteDialog.close();
const confirmDelete = node('button', 'btn-accent btn-text', '删除'); confirmDelete.type = 'button';
confirmDelete.addEventListener('click', async () => {
  if (pendingDelete && await playlistAction(pendingDelete.id, 'delete')) deleteDialog.close();
});
deleteActions.append(cancelDelete, confirmDelete); deleteDialog.append(deleteHeading, deleteName, deleteActions); document.body.append(deleteDialog);
const browsePlaylists = button('queue', '播放列表', () => {
  pendingTrack = null; $('add-dialog-title').textContent = '播放列表'; $('add-list').replaceChildren();
  for (const p of playlists) {
    const el = button('music', p.name, () => {$('add-dialog').close(); showView('playlist:' + p.id);}, 'add-item');
    el.append(node('span', '', p.name)); const li = node('li'); li.append(el); $('add-list').append(li);
  }
  const add = button('plus', '新建列表', () => {$('add-dialog').close(); openPlaylist();}, 'add-item');
  add.append(node('span', '', '新建列表')); const li = node('li'); li.append(add); $('add-list').append(li);
  $('add-dialog').showModal();
});
browsePlaylists.id = 'browse-playlists'; document.querySelector('.rail-section-head').prepend(browsePlaylists);
const viewActions = button('more', '管理列表', event => {
  const p = playlists.find(p => view === 'playlist:' + p.id); if (p) playlistMenu(p, event);
});
viewActions.id = 'view-actions'; viewActions.hidden = true; document.querySelector('.list-head').append(viewActions);
const mobileQueue = button('queue', '队列', () => {$('queue-panel').hidden = !$('queue-panel').hidden; renderQueue();});
mobileQueue.id = 'mobile-queue-toggle'; document.querySelector('.topbar').append(mobileQueue);
const mobileDetail = button('detail', '详情', () => openDetail(state.current || heroKey));
mobileDetail.id = 'mobile-detail-toggle'; document.querySelector('.topbar').append(mobileDetail);

function shortDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)} 分`;
  return `${seconds} 秒`;
}
function svgNode(tag, attributes = {}) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, value);
  return el;
}
function dailyChart(points) {
  const svg = svgNode('svg', {viewBox: '0 0 640 176', role: 'presentation'});
  const maximum = Math.max(1, ...points.map(p => p.seconds));
  svg.append(svgNode('line', {x1: 28, y1: 140, x2: 620, y2: 140, class: 'chart-axis'}));
  const width = 584 / Math.max(1, points.length);
  points.forEach((point, index) => {
    const height = 108 * point.seconds / maximum;
    const bar = svgNode('rect', {x: 32 + index * width, y: 140-height, width: Math.max(2, width-5),
      height, rx: 2, class: 'chart-bar' + (index === points.length-1 ? ' is-today' : '')});
    const title = svgNode('title'); title.textContent = `${point.date} · ${shortDuration(point.seconds)}`;
    bar.append(title); svg.append(bar);
    if (index === 0 || index === points.length-1 || (points.length > 10 && index === Math.floor(points.length/2))) {
      const label = svgNode('text', {x: 32 + index * width, y: 159, class: 'chart-label'});
      label.textContent = point.date.slice(5).replace('-', '/'); svg.append(label);
    }
  });
  $('stats-chart').replaceChildren(svg);
}
async function loadStats(reset = true) {
  const generation = ++statsGeneration;
  try {
    const [summary, history] = await Promise.all([api('/api/stats?days=' + statsDays + '&source=' + statsSource),
      api('/api/history?source=' + statsSource)]);
    if (generation !== statsGeneration || view !== 'insights') return;
    $('stat-time').textContent = shortDuration(summary.totalSeconds);
    $('stat-plays').textContent = summary.playCount; $('stat-songs').textContent = summary.songCount;
    document.querySelectorAll('[data-days]').forEach(el => el.setAttribute('aria-pressed', Number(el.dataset.days) === statsDays));
    document.querySelectorAll('[data-stats-source]').forEach(el => {
      const active = el.dataset.statsSource === statsSource;
      el.setAttribute('aria-pressed', active); el.tabIndex = active ? 0 : -1;
    });
    $('stat-sources').hidden = !summary.totalSeconds || !aggregationReady;
    const shares = summary.bySource || [], total = shares.reduce((sum, row) => sum + row.seconds, 0);
    const qqShare = total ? Math.round((shares.find(row => row.source === 'qq')?.seconds || 0) / total * 100) : 0;
    $('source-share').style.setProperty('--qq-share', qqShare + '%');
    const shareLabel = 'QQ ' + qqShare + '% · 网易 ' + (100-qqShare) + '%';
    $('source-share-label').textContent = shareLabel;
    $('source-share').setAttribute('aria-label', '按听歌时长：' + shareLabel);
    $('stats-chart').hidden = !summary.totalSeconds;
    if (summary.totalSeconds) dailyChart(summary.daily);
    const topTracks = $('stats-top-tracks'); topTracks.replaceChildren();
    for (const [index, item] of summary.topTracks.entries()) {
      tracks.set(item.track.key, item.track);
      const row = node('button', 'top-row'); row.type = 'button'; row.title = '播放 ' + item.track.title;
      const info = node('span', 'top-info'), artist = node('span', 'top-sub'); artistWithSource(artist, item.track.artist, item.track);
      info.append(node('span', 'top-name', item.track.title), artist);
      row.append(node('span', 'top-rank', String(index+1).padStart(2, '0')), cover(item.track, 'top-cover'), info,
        node('span', 'top-count', shortDuration(item.seconds)));
      row.addEventListener('click', () => playTrack(item.track.key));
      const li = node('li'); li.append(row); topTracks.append(li);
    }
    topTracks.closest('.top-block').hidden = !summary.topTracks.length;
    const topArtists = $('stats-top-artists'); topArtists.replaceChildren();
    for (const [index, item] of summary.topArtists.entries()) {
      const row = node('button', 'top-row artist-row'); row.type = 'button'; row.title = '搜索 ' + item.artist;
      const info = node('span', 'top-info'); info.append(node('span', 'top-name', item.artist), node('span', 'top-sub', `${item.plays} 次`));
      row.append(node('span', 'top-rank', String(index+1).padStart(2, '0')), info, node('span', 'top-count', shortDuration(item.seconds)));
      row.addEventListener('click', () => {$('search-input').value = item.artist; search();});
      const li = node('li'); li.append(row); topArtists.append(li);
    }
    topArtists.closest('.top-block').hidden = !summary.topArtists.length;
    historyItems = history.items; historyOffset = historyItems.length;
    renderHistory(history.hasMore);
  } catch (error) {if (generation === statsGeneration && view === 'insights') fail(error);}
}
function renderHistory(hasMore) {
  const list = $('history-list'); list.replaceChildren();
  const formatter = new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai'});
  for (const item of historyItems) {
    tracks.set(item.track.key, item.track);
    const row = node('button', 'history-row'); row.type = 'button';
    row.title = `${item.track.title} · ${Math.round(item.coverage * 100)}%`;
    const info = node('span', 'row-info'), artist = node('span', 'row-artist'); artistWithSource(artist, item.track.artist, item.track);
    info.append(node('span', 'row-title', item.track.title), artist);
    row.append(cover(item.track, 'row-cover'), info, node('span', 'history-time', formatter.format(item.startedAt*1000)),
      node('span', 'row-duration', time(item.seconds)));
    row.addEventListener('click', () => playTrack(item.track.key));
    const li = node('li'); li.append(row); list.append(li);
  }
  if (!historyItems.length) {
    const empty = node('li', 'insights-empty'); empty.append(icon('chart'), node('span', '', '暂无')); list.append(empty);
  }
  $('history-more').hidden = !hasMore;
}
function closeDetail(focus = true) {
  $('detail-panel').hidden = true; ++detailGeneration;
  for (const id of ['detail-toggle', 'mobile-detail-toggle']) {$(id).classList.remove('is-active'); $(id).setAttribute('aria-expanded', 'false');}
  if (focus) (window.innerWidth <= 720 ? $('mobile-detail-toggle') : $('detail-toggle')).focus();
}
function openDetail(key, tab = 'lyrics') {
  const track = tracks.get(key); if (!track) return;
  detailKey = key; $('detail-panel').hidden = false; $('queue-panel').hidden = true; renderQueue();
  $('detail-cover').src = track.cover || fallback; $('detail-title').textContent = track.title;
  artistWithSource($('detail-artist'), track.artist, track);
  for (const id of ['detail-toggle', 'mobile-detail-toggle']) {$(id).classList.add('is-active'); $(id).setAttribute('aria-expanded', 'true');}
  setDetailTab(tab);
}
function setDetailTab(tab) {
  detailTab = tab; const generation = ++detailGeneration;
  for (const el of document.querySelectorAll('[data-detail]')) {
    const active = el.dataset.detail === tab; el.classList.toggle('is-active', active);
    el.setAttribute('aria-selected', active); el.tabIndex = active ? 0 : -1;
  }
  $('lyrics-view').hidden = tab !== 'lyrics'; $('analysis-view').hidden = tab !== 'analysis';
  if (tab === 'lyrics') {lyricsData = lyricCache.get(detailKey) || null; renderLyrics(); if (!lyricsData) loadLyrics(detailKey, generation);}
  else {analysisData = analysisCache.get(detailKey) || null; renderAnalysis(); loadAnalysis(detailKey, generation);}
}
async function taskResult(path, payload, kind, generation, onProgress = () => {}) {
  let result;
  for (let attempt = 0; attempt < 30; attempt++) {
    if (generation !== detailGeneration) return null;
    try {result = await api(path, payload); break;}
    catch (error) {if (error.code !== 'BUSY') throw error; await new Promise(resolve => setTimeout(resolve, 750));}
  }
  if (!result) throw Object.assign(new Error(), {code: 'NETWORK_TIMEOUT'});
  if (result.status === 'ready') return result[kind];
  return pollTask(result, kind, generation, onProgress);
}
async function pollTask(result, kind, generation, onProgress = () => {}) {
  if (result.status === 'error') throw Object.assign(new Error(), {code: result.code});
  for (let attempt = 0; attempt < 600; attempt++) {
    if (generation !== detailGeneration) return null;
    const job = await api('/api/jobs/' + result.job);
    if (job.status === 'ready') return job[kind];
    if (job.status === 'error') throw Object.assign(new Error(), {code: job.code});
    onProgress(job);
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw Object.assign(new Error(), {code: 'NETWORK_TIMEOUT'});
}
async function loadLyrics(key, generation, refresh = false) {
  $('lyrics-status').textContent = '加载中';
  $('lyrics-refresh').disabled = true;
  try {
    const result = await taskResult('/api/lyrics', {key, refresh}, 'lyrics', generation);
    if (!result) return;
    lyricCache.set(key, result);
    desktopLyrics.lyricsChanged(key, result);
    if (generation === detailGeneration && detailKey === key) {lyricsData = result; renderLyrics();}
  } catch (error) {
    if (generation === detailGeneration) {$('lyrics-status').textContent = messages[error.code] || '暂不可用'; if (error.code === 'LOGIN_REQUIRED') openAuth(false);}
  } finally {if (generation === detailGeneration) $('lyrics-refresh').disabled = false;}
}
function renderLyrics() {
  const list = $('lyrics-list'); list.replaceChildren(); lyricCurrent = -1;
  $('lyrics-export').disabled = !lyricsData?.available;
  $('lyrics-refresh').disabled = false;
  $('lyrics-status').textContent = lyricsData ? (lyricsData.available ? '' : '无歌词') : '';
  for (const [index, line] of (lyricsData?.lines || []).entries()) {
    const li = node('li');
    const text = node(line.time === null ? 'span' : 'button', 'lyric-line', line.text); text.dataset.index = index;
    if (line.time !== null) {
      text.type = 'button'; text.title = time(line.time);
      text.addEventListener('click', async () => {
        const key = detailKey; if (sourceKey !== key) await playTrack(key);
        if (sourceKey === key && Number.isFinite(audio.duration)) {audio.currentTime = Math.min(line.time, audio.duration); updateProgress();}
      });
    } else text.classList.add('is-readable');
    li.append(text); list.append(li);
  }
  updateLyricPosition();
}
function updateLyricPosition() {
  if (!lyricsData || $('detail-panel').hidden || detailTab !== 'lyrics') return;
  let index = -1;
  if (sourceKey === detailKey) {
    for (const [i, line] of lyricsData.lines.entries()) if (line.time !== null && line.time <= audio.currentTime) index = i;
  }
  if (index === lyricCurrent) return;
  $('lyrics-list').querySelector('.is-current')?.classList.remove('is-current'); lyricCurrent = index;
  const current = $('lyrics-list').querySelector(`[data-index="${index}"]`);
  if (current) {
    current.classList.add('is-current');
    if (lyricsFollow) current.scrollIntoView({block: 'center', behavior: 'smooth'});
  }
}
async function loadAnalysis(key, generation) {
  try {
    const result = await api('/api/analysis/' + key);
    if (generation !== detailGeneration) return;
    if (result.analysis) {analysisCache.set(key, result.analysis); analysisData = result.analysis; renderAnalysis();}
    if (result.status === 'loading') {
      analysisProgress(result, generation);
      const analysis = await pollTask(result, 'analysis', generation, job => analysisProgress(job, generation));
      if (analysis) analysisCache.set(key, analysis);
      if (analysis && generation === detailGeneration) {analysisData = analysis; renderAnalysis();}
    } else if (result.status === 'error') throw Object.assign(new Error(), {code: result.code});
  } catch (error) {if (generation === detailGeneration) {
    $('analysis-status').textContent = messages[error.code] || '暂不可用';
    $('analysis-run').textContent = '重试';
    $('analysis-run').disabled = false; $('analysis-run').classList.remove('is-loading');
  }}
}
function analysisProgress(job, generation) {
  if (generation !== detailGeneration) return;
  $('analysis-run').disabled = true; $('analysis-run').classList.add('is-loading');
  $('analysis-status').textContent = job.phase === 'audio' ? '准备音频' : '分析中';
}
function renderAnalysis() {
  const result = $('analysis-result'); result.replaceChildren();
  $('analysis-run').disabled = false; $('analysis-run').classList.remove('is-loading');
  $('analysis-run').textContent = analysisData ? '重算' : '分析';
  $('analysis-model').hidden = !analysisData;
  $('analysis-status').textContent = analysisData ? `片段 ${time(analysisData.audio.start)}–${time(analysisData.audio.start+analysisData.audio.seconds)} · 估计` : '未分析';
  if (!analysisData) return;
  $('analysis-model').textContent = 'Basic Pitch'; $('analysis-model').title = '本机 CPU · 音符候选';
  const metrics = node('div', 'analysis-metrics');
  for (const metric of analysisData.metrics) {
    const item = node('div', 'metric'); const value = node('span', 'metric-num', String(metric.value));
    if (metric.unit) value.append(node('small', 'metric-unit', ' ' + metric.unit));
    item.append(value, node('span', 'metric-label', metric.label)); metrics.append(item);
  }
  result.append(metrics);
  if (analysisData.notes.length) {
    const chart = node('div', 'analysis-chart'); chart.setAttribute('role', 'img'); chart.setAttribute('aria-label', '音符候选');
    const svg = svgNode('svg', {viewBox: '0 0 300 120', role: 'presentation'});
    const low = Math.min(...analysisData.notes.map(n => n.midi))-1, high = Math.max(...analysisData.notes.map(n => n.midi))+1;
    for (const note of analysisData.notes) svg.append(svgNode('rect', {
      x: 12 + (note.start-analysisData.audio.start) / analysisData.audio.seconds * 276,
      y: 100 - (note.midi-low) / (high-low) * 80,
      width: Math.max(.6, (note.end-note.start) / analysisData.audio.seconds * 276), height: 2,
      class: 'pitch-note', opacity: .25 + note.confidence * .7}));
    chart.append(svg); result.append(chart);
  }
}

$('history-more').addEventListener('click', async () => {
  const generation = statsGeneration; $('history-more').disabled = true;
  try {
    const data = await api('/api/history?offset=' + historyOffset + '&source=' + statsSource);
    if (generation !== statsGeneration || view !== 'insights') return;
    historyItems.push(...data.items); historyOffset += data.items.length; renderHistory(data.hasMore);
  } catch (error) {fail(error);} finally {$('history-more').disabled = false;}
});
document.querySelectorAll('[data-days]').forEach(el => el.addEventListener('click', () => {statsDays = Number(el.dataset.days); loadStats(true);}));
function installSourceFilter(attribute, choose) {
  const buttons = [...document.querySelectorAll('[' + attribute + ']')];
  for (const [index, button] of buttons.entries()) {
    button.addEventListener('click', () => choose(button.getAttribute(attribute)));
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].click(); buttons[next].focus();
    });
  }
}
installSourceFilter('data-search-source', source => {sourceFilter = source; closeMenu(); renderList();});
installSourceFilter('data-stats-source', source => {statsSource = source; loadStats(true);});
$('detail-toggle').addEventListener('click', () => {if (!$('detail-panel').hidden) closeDetail(); else openDetail(state.current || heroKey);});
$('detail-close').addEventListener('click', () => closeDetail());
$('detail-cover').addEventListener('error', () => {$('detail-cover').src = fallback;});
document.querySelectorAll('[data-detail]').forEach(el => {
  el.addEventListener('click', () => setDetailTab(el.dataset.detail));
  el.addEventListener('keydown', event => {
    if (['ArrowLeft', 'ArrowRight'].includes(event.key)) {event.preventDefault(); const tab = detailTab === 'lyrics' ? 'analysis' : 'lyrics'; setDetailTab(tab); document.querySelector(`[data-detail="${tab}"]`).focus();}
  });
});
$('lyrics-follow').addEventListener('click', () => {lyricsFollow = !lyricsFollow; $('lyrics-follow').setAttribute('aria-pressed', lyricsFollow); lyricCurrent = -1; updateLyricPosition();});
for (const event of ['wheel', 'touchmove']) $('lyrics-list').addEventListener(event, () => {lyricsFollow = false; $('lyrics-follow').setAttribute('aria-pressed', 'false');}, {passive: true});
$('lyrics-export').addEventListener('click', () => {
  if (!lyricsData?.available) return;
  const a = node('a'); a.href = '/api/lyrics/' + detailKey + '/export'; a.download = ''; document.body.append(a); a.click(); a.remove();
});
const lyricRefresh = button('repeat', '重新提取歌词', () => loadLyrics(detailKey, ++detailGeneration, true));
lyricRefresh.id = 'lyrics-refresh'; document.querySelector('.lyrics-actions').prepend(lyricRefresh);
async function runAnalysis(refresh = Boolean(analysisData)) {
  const key = detailKey, generation = detailGeneration;
  $('analysis-run').disabled = true; $('analysis-run').classList.add('is-loading'); $('analysis-status').textContent = '分析中';
  try {
    const result = await taskResult('/api/analyze', {key, refresh}, 'analysis', generation, job => analysisProgress(job, generation));
    if (!result) return;
    analysisCache.set(key, result);
    if (generation === detailGeneration) {analysisData = result; renderAnalysis();}
  } catch (error) {
    if (generation === detailGeneration) {$('analysis-status').textContent = messages[error.code] || '分析未完成'; $('analysis-run').textContent = '重试'; if (error.code === 'LOGIN_REQUIRED') openAuth(false);}
  } finally {if (generation === detailGeneration) {$('analysis-run').disabled = false; $('analysis-run').classList.remove('is-loading');}}
}
$('analysis-run').addEventListener('click', () => runAnalysis());

$('search-form').addEventListener('submit', event => {event.preventDefault(); search();});
document.querySelectorAll('[data-view]').forEach(el => el.addEventListener('click', () => showView(el.dataset.view)));
document.querySelector('.brand').addEventListener('click', event => {event.preventDefault(); showView('library');});
$('create-playlist').addEventListener('click', () => {pendingTrack = null; openPlaylist();});
$('playlist-cancel').addEventListener('click', () => {$('playlist-dialog').close(); pendingTrack = null;});
$('playlist-form').addEventListener('submit', async event => {
  event.preventDefault(); const name = $('playlist-name').value.trim(); if (!name) return;
  $('playlist-save').disabled = true;
  try {
    if (editingPlaylist) {if (!await playlistAction(editingPlaylist, 'rename', name)) return;}
    else {
      const data = await api('/api/playlists', {name}); ingest(data, true);
      if (pendingTrack) {await playlistAction(data.id, 'add', pendingTrack); pendingTrack = null;}
      view = 'playlist:' + data.id;
    }
    $('playlist-dialog').close(); render();
    if (view !== 'search') $('search-input').value = '';
  } catch (error) {fail(error);}
  finally {$('playlist-save').disabled = false;}
});
$('add-close').addEventListener('click', () => {$('add-dialog').close(); pendingTrack = null;});
$('hero-play').addEventListener('click', () => {
  if (heroKey === state.current && (sourceKey || loading)) toggle(); else if (heroKey) playTrack(heroKey, visibleKeys());
});
$('hero-like').addEventListener('click', () => favorite(heroKey));
$('player-like').addEventListener('click', () => favorite(state.current));
$('play-toggle').addEventListener('click', toggle);
$('previous').addEventListener('click', () => advance(-1));
$('next').addEventListener('click', () => advance(1));
$('play-mode').addEventListener('click', () => {
  const modes = ['repeat', 'shuffle', 'single']; state.mode = modes[(modes.indexOf(state.mode) + 1) % 3]; persist(); renderPlayer();
});
$('seek').addEventListener('input', () => {$('seek').style.setProperty('--p', $('seek').value + '%');});
$('seek').addEventListener('change', () => {
  if (sourceKey && Number.isFinite(audio.duration)) {audio.currentTime = audio.duration * Number($('seek').value) / 100; state.position = audio.currentTime; persist();}
});
$('volume').addEventListener('input', () => {state.volume = Number($('volume').value); audio.volume = state.volume;
  $('volume').style.setProperty('--p', state.volume * 100 + '%'); persist();});
$('queue-toggle').addEventListener('click', () => {$('queue-panel').hidden = !$('queue-panel').hidden; renderQueue();});
$('queue-close').addEventListener('click', () => {$('queue-panel').hidden = true; renderQueue();});
$('queue-clear').addEventListener('click', () => {state.queue = []; persist(); renderQueue();});
$('auth-button').addEventListener('click', openAuth);
for (const provider of ['qq', 'wx', 'mobile']) $('auth-' + provider).addEventListener('click', () => startAuth(provider));
$('auth-close').addEventListener('click', () => $('auth-dialog').close());
$('auth-dialog').addEventListener('close', () => {clearTimeout(authTimer); api('/api/auth/cancel', {}).catch(() => {});});
for (const id of ['hero-cover', 'player-cover']) $(id).addEventListener('error', () => {$(id).src = fallback;});
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click', event => {
  const rect = dialog.getBoundingClientRect();
  if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
});
document.addEventListener('click', event => {if (!menu.contains(event.target) && !event.target.closest('.row-more,.playlist-more')) closeMenu();});
window.addEventListener('resize', closeMenu);
$('content').addEventListener('scroll', closeMenu);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {closeMenu(); if (!$('detail-panel').hidden) closeDetail();}
  if (!menu.hidden && menu.contains(event.target) && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const items = [...menu.querySelectorAll('button')], index = items.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 :
      (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  }
  if (event.code === 'Space' && !event.repeat && !event.target.closest('input,textarea,button,dialog,[contenteditable]')) {event.preventDefault(); toggle();}
  if (event.key === '/' && !event.target.closest('input,textarea,dialog')) {event.preventDefault(); $('search-input').focus();}
});
audio.addEventListener('loadedmetadata', () => {
  if (resumePosition && resumePosition < audio.duration - 1) audio.currentTime = resumePosition;
  resumePosition = 0; renderPlayer();
});
let lastSaved = 0;
audio.addEventListener('timeupdate', () => {
  updateProgress();
  listening.tick(); updateLyricPosition(); desktopLyrics.update();
  if (sourceKey && Date.now() - lastSaved > 5000) {state.position = audio.currentTime; persist(); lastSaved = Date.now();}
});
audio.addEventListener('play', () => {renderPlayer(); renderList();
  scoreModule.pause();
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';});
audio.addEventListener('pause', () => {
  if (sourceKey) {state.position = audio.currentTime; persist(true);}
  renderPlayer(); renderList(); if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
});
audio.addEventListener('ended', () => advance(1, true));
for (const event of ['seeking', 'seeked', 'playing', 'waiting']) audio.addEventListener(event, () => desktopLyrics.update());
audio.addEventListener('error', () => {
  if (!sourceKey) return;
  sourceKey = null; loading = null; renderPlayer(); toast('播放中断，请重试');
});
if ('mediaSession' in navigator) {
  for (const [action, handler] of Object.entries({play: () => toggle(), pause: () => audio.pause(),
    previoustrack: () => advance(-1), nexttrack: () => advance(1),
    seekto: details => {if (sourceKey && Number.isFinite(details.seekTime)) audio.currentTime = details.seekTime;}})) {
    try {navigator.mediaSession.setActionHandler(action, handler);} catch {}
  }
}
window.addEventListener('pagehide', () => {
  desktopLyrics.dispose();
  listening.finish('close', true);
  if (sourceKey) state.position = audio.currentTime;
  fetch('/api/state', {method: 'POST', keepalive: true,
    headers: {'Content-Type': 'application/json', 'X-Talk-CSRF': csrf}, body: JSON.stringify(state)}).catch(() => {});
});
window.addEventListener('pageshow', event => {if (event.persisted && sourceKey && !audio.paused) {listening.start(); listening.reset();}});

try {
  const [snapshot, features] = await Promise.all([api('/api/library'), api('/api/features')]);
  aggregationReady = features.aggregation === true;
  if (aggregationReady) ({groupTracks, enqueueVersion, mergeSearchPage, interleaveResults, sourceName, trackSource} = await import('/aggregation.mjs'));
  $('stats-source-filter').hidden = !aggregationReady;
  ingest(snapshot); audio.volume = state.volume; render();
  if (features.scoreLab) {
    try {
      const {installScoreExtension} = await import('/score-extension.mjs');
      scoreModule = installScoreExtension({api, showView, getTrack: key => tracks.get(key),
        getCurrent: () => ({key: state.current, position: sourceKey ? audio.currentTime : state.position}),
        onError: fail, toast, pausePlayer: () => audio.pause()});
      scoreReady = true; $('hero-score').hidden = false;
      $('hero-score').addEventListener('click', () => scoreModule.openTrack(heroKey));
    } catch { /* Optional capability must never prevent music playback. */ }
  }
  if (features.desktopLyrics && window.usicLyrics) {
    try {
      const {installDesktopLyrics} = await import('/desktop-lyrics.mjs');
      desktopLyrics = installDesktopLyrics({
        getPlayback: () => ({track: tracks.get(state.current),
          position: sourceKey === state.current ? audio.currentTime : state.position,
          loading: Boolean(loading),
          playing: Boolean(sourceKey === state.current && !audio.paused && !audio.ended), canSkip: Boolean(state.queue.length)}),
        api, cache: lyricCache, actions: {previous: () => advance(-1), 'toggle-play': toggle, next: () => advance(1)}, onError: fail,
      });
    } catch { /* Optional native feature must never prevent normal playback. */ }
  }
  // No auto-play, no auto-QR, no official-client launch, and no misleading online badge from disk alone.
  api('/api/account/verify', {}).then(data => {account = data; renderAccount();}).catch(error => {
    if (error.code === 'LOGIN_REQUIRED') {account.session = 'required'; renderAccount();}
  });
} catch (error) {fail(error);}
