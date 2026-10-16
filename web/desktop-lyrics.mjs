// Desktop text display only. Playback, source authorization and listening remain in app.js.
const clip = (text, limit) => String(text || '').replace(/[\u0000-\u001f\u007f]/g, '').slice(0, limit).replace(/[\uD800-\uDBFF]$/, '');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export function buildTimeline(document) {
  const lines = (Array.isArray(document?.lines) ? document.lines : []).slice(0, 1000)
    .filter(line => line && typeof line.text === 'string' && line.text.trim());
  const groups = [];
  for (const line of lines.filter(line => Number.isFinite(line.time) && line.time >= 0 && line.time <= 1805)
    .sort((a, b) => a.time - b.time)) {
    const last = groups.at(-1);
    if (last?.time === line.time) last.text = clip(last.text + ' · ' + line.text, 500);
    else groups.push({time: line.time, text: clip(line.text, 500)});
  }
  return {groups, plain: lines.slice(0, 2).map(line => clip(line.text, 500))};
}

export function lyricFrame(playback, document, timeline = buildTimeline(document)) {
  const {track, playing = false, canSkip = false} = playback;
  if (!track) return {key: null, title: '', artist: '', current: '♪', next: '', playing: false, canSkip, status: 'idle'};
  const frame = {key: track.key, title: clip(track.title, 150), artist: clip(track.artist, 150),
    current: clip(track.title, 150) || '♪', next: '', playing, canSkip, status: playback.status || 'loading'};
  if (!document) {
    frame.next = frame.status === 'error' ? '歌词暂不可用' : frame.artist;
    return frame;
  }
  if (!document.available || !timeline.plain.length) return {...frame, status: 'unavailable', next: '无歌词'};
  if (!timeline.groups.length) return {...frame, status: 'untimed', current: timeline.plain[0], next: timeline.plain[1] || ''};
  const position = Number.isFinite(playback.position) ? Math.max(0, playback.position) : 0;
  let low = 0, high = timeline.groups.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (timeline.groups[mid].time <= position) low = mid + 1; else high = mid;
  }
  const index = low - 1;
  return {...frame, status: 'ready', current: index >= 0 ? timeline.groups[index].text : frame.current,
    next: timeline.groups[index + 1]?.text || ''};
}

export async function fetchLyricDocument(api, key, {isCurrent = () => true, wait = sleep} = {}) {
  let result = await api('/api/lyrics/' + encodeURIComponent(key));
  if (!isCurrent()) return null;
  if (result.status !== 'ready' && result.status !== 'loading') {
    for (let attempt = 0; attempt < 30; attempt++) {
      if (!isCurrent()) return null;
      try {result = await api('/api/lyrics', {key}); break;}
      catch (error) {if (error.code !== 'BUSY' || attempt === 29) throw error; await wait(500);}
    }
  }
  for (let attempt = 0; result.status === 'loading' && attempt < 180; attempt++) {
    if (!isCurrent()) return null;
    if (typeof result.job !== 'string' || !/^[a-f0-9]{32}$/.test(result.job)) throw Object.assign(new Error(), {code: 'LYRICS_INVALID'});
    const job = result.job;
    await wait(500);
    if (!isCurrent()) return null;
    result = await api('/api/jobs/' + job);
    // Job records include id rather than job.
    if (result.status === 'loading') result = {...result, job};
  }
  if (!isCurrent()) return null;
  if (result.status === 'error') throw Object.assign(new Error(), {code: result.code || 'LYRICS_UNAVAILABLE'});
  if (result.status !== 'ready') throw Object.assign(new Error(), {code: 'NETWORK_TIMEOUT'});
  if (!result.lyrics || result.lyrics.key !== key) throw Object.assign(new Error(), {code: 'LYRICS_INVALID'});
  return result.lyrics;
}

export function installDesktopLyrics({bridge = globalThis.usicLyrics, doc = globalThis.document,
  getPlayback, api, cache, actions, onError = () => {}}) {
  const methods = ['toggle', 'unlock', 'getState', 'publish', 'onState', 'onCommand'];
  const button = doc?.getElementById('desktop-lyrics-toggle');
  const noop = {update() {}, lyricsChanged() {}, dispose() {}};
  if (!button || !bridge || !methods.every(name => typeof bridge[name] === 'function')) return noop;
  let nativeState = {visible: false, locked: false}, key = null, lyrics = null, timeline = buildTimeline(null);
  let generation = 0, status = 'loading', lastPacket = '', disposed = false, busy = false, stateSerial = 0, requested = false;
  const publish = () => {
    if (!nativeState.visible || disposed) return;
    const frame = lyricFrame({...getPlayback(), status}, lyrics, timeline);
    const packet = JSON.stringify(frame);
    if (packet !== lastPacket) {lastPacket = packet; bridge.publish(frame);}
  };
  const update = () => {
    if (!nativeState.visible || disposed) return;
    const playback = getPlayback(), nextKey = playback.track?.key || null;
    if (nextKey !== key) {
      key = nextKey; ++generation; requested = false;
      lyrics = cache.get(key) || null; timeline = buildTimeline(lyrics); status = 'loading';
    }
    publish();
    // A new source fetch owns its provider lock. Don't compete with playback or
    // exhaust BUSY retries before the audio download has even completed.
    if (!key || lyrics || requested || playback.loading) return;
    requested = true;
    const request = generation;
    const requestedKey = key;
    const isCurrent = () => !disposed && nativeState.visible && request === generation && getPlayback().track?.key === requestedKey;
    fetchLyricDocument(api, requestedKey, {isCurrent}).then(result => {
      if (!result || !isCurrent()) return;
      cache.set(requestedKey, result); lyrics = result; timeline = buildTimeline(result); publish();
    }).catch(error => {
      if (!isCurrent()) return;
      status = 'error'; publish();
      if (error.code === 'PLAYER_OFFLINE') onError(error);
    });
  };
  const applyState = state => {
    if (disposed || !state || typeof state.visible !== 'boolean') return;
    const wasVisible = nativeState.visible;
    nativeState = state;
    button.classList.toggle('is-active', state.visible);
    button.classList.toggle('is-locked', state.locked);
    button.setAttribute('aria-pressed', String(state.visible));
    button.title = state.locked ? '桌面歌词 · 右键解锁' : '桌面歌词';
    if (!state.visible) {++generation; key = null; requested = false; lastPacket = '';}
    else {if (!wasVisible) lastPacket = ''; update();}
  };
  const unsubscribeState = bridge.onState(state => {++stateSerial; applyState(state);});
  const unsubscribeCommand = bridge.onCommand(command => {
    if (!disposed && typeof actions?.[command] === 'function') Promise.resolve().then(() => actions[command]()).catch(onError);
  });
  const toggle = async () => {
    if (busy || disposed) return;
    busy = true; button.disabled = true;
    try {applyState(await bridge.toggle());} catch (error) {onError(error);}
    finally {busy = false; button.disabled = false;}
  };
  const unlock = event => {
    if (!nativeState.visible || !nativeState.locked) return;
    event.preventDefault(); bridge.unlock().then(applyState).catch(onError);
  };
  button.hidden = false;
  button.addEventListener('click', toggle); button.addEventListener('contextmenu', unlock);
  const initialSerial = stateSerial;
  bridge.getState().then(state => {if (stateSerial === initialSerial) applyState(state);}).catch(onError);
  const dispose = () => {
    if (disposed) return;
    disposed = true; ++generation;
    unsubscribeState(); unsubscribeCommand();
    button.removeEventListener('click', toggle); button.removeEventListener('contextmenu', unlock);
    button.hidden = true;
  };
  return {update, dispose, lyricsChanged(changedKey, document) {
    if (changedKey !== key || !nativeState.visible || disposed) return;
    ++generation; lyrics = document; timeline = buildTimeline(document); status = 'loading'; publish();
  }};
}
