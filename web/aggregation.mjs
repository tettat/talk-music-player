// Display groups are not playback identities. Only real provider keys leave here.
export const sourceName = source => ({qq: 'QQ', netease: '网易'}[source] || '—');
export const trackSource = track => track?.source || (track?.key?.startsWith('netease:') ? 'netease' : 'qq');
const normalize = value => String(value || '').normalize('NFKC').toLocaleLowerCase('en')
  .replace(/[\p{P}\p{Z}\s]/gu, '');
const version = /\b(live|remix|cover|demo|acoustic|instrumental|sped|slowed|edit)\b|现场|翻唱|伴奏|重制|纯音乐|加速|慢速|混音/iu;
const unknown = value => !value || value === '—';

export function recordingSignature(track) {
  if (!track || unknown(track.title) || unknown(track.artist) || unknown(track.album)
      || !Number.isFinite(track.duration) || version.test(track.title) || version.test(track.album)) return null;
  const artists = track.artist.split(/\s+\/\s+/).map(normalize).filter(Boolean).sort();
  return JSON.stringify([normalize(track.title), [...new Set(artists)], normalize(track.album)]);
}

export function sameRecording(a, b) {
  if (!a || !b) return false;
  if (a.key === b.key) return true;
  const signature = recordingSignature(a);
  return trackSource(a) !== trackSource(b) && signature !== null
    && signature === recordingSignature(b) && Math.abs(a.duration-b.duration) <= 2;
}

export function groupTracks(records, {selections = new Map(), source = 'all', current = null} = {}) {
  const groups = [], candidates = new Map(), seen = new Set();
  for (const track of records) {
    if (!track?.key || seen.has(track.key)) continue;
    seen.add(track.key);
    const signature = recordingSignature(track);
    const bucket = signature === null ? [] : candidates.get(signature) || [];
    let group = bucket.find(group => !group.allVariants.some(v => trackSource(v) === trackSource(track))
      && group.allVariants.every(v => sameRecording(v, track)));
    if (!group) {
      group = {id: signature === null ? track.key : signature + '#' + bucket.length, allVariants: []};
      groups.push(group);
      if (signature !== null) {bucket.push(group); candidates.set(signature, bucket);}
    }
    group.allVariants.push(track);
  }
  return groups.flatMap(group => {
    const variants = group.allVariants.filter(track => source === 'all' || trackSource(track) === source);
    if (!variants.length) return [];
    const chosen = selections.get(group.id);
    const selected = variants.find(t => t.key === chosen) || variants.find(t => t.key === current)
      || variants.find(t => trackSource(t) === 'qq' && !['requires_login', 'unavailable'].includes(t.availability))
      || variants.find(t => t.availability === 'free') || variants[0];
    return [{...group, variants, selected}];
  });
}

export function enqueueVersion(queue, key, tracks, {next = false, current = null, playing = false} = {}) {
  const selected = tracks.get(key);
  if (!selected) return {queue, blocked: true};
  if (playing && current !== key && sameRecording(selected, tracks.get(current))) return {queue, blocked: true};
  const result = [...new Set(queue)].filter(k => !sameRecording(selected, tracks.get(k)));
  if (next && current && current !== key) result.splice(Math.max(0, result.indexOf(current)+1), 0, key);
  else result.push(key);
  return {queue: result, blocked: false};
}

export function mergeSearchPage(previous, incoming) {
  // Failed pages never advance a cursor or destroy previously loaded results.
  const merged = {...previous};
  for (const [source, row] of Object.entries(incoming || {})) {
    merged[source] = row.status === 'ready' ? {...row} : {...previous[source], status: 'error', code: row.code};
  }
  return merged;
}

export function interleaveResults(bySource) {
  const order = ['qq', 'netease'], result = [], seen = new Set();
  for (let i = 0; i < Math.max(...order.map(source => bySource[source]?.length || 0)); i++) {
    for (const source of order) {
      const key = bySource[source]?.[i];
      if (key && !seen.has(key)) {result.push(key); seen.add(key);}
    }
  }
  return result;
}
