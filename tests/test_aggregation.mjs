import test from 'node:test';
import assert from 'node:assert/strict';
import {groupTracks, sameRecording, enqueueVersion, mergeSearchPage, recordingSignature, interleaveResults} from '../web/aggregation.mjs';

const qq = {key: 'qq:0039MnYb0qxYhV', source: 'qq', title: '晴天', artist: '周杰伦', album: '叶惠美', duration: 269};
const ne = {...qq, key: 'netease:186016', source: 'netease', availability: 'free', duration: 270};

test('only display groups merge; selected playback key stays an exact provider recording', () => {
  const groups = groupTracks([ne, qq, qq]);
  assert.equal(groups.length, 1); assert.equal(groups[0].allVariants.length, 2);
  assert.equal(groups[0].selected.key, qq.key); assert.notEqual(groups[0].id, qq.key);
  assert.equal(groupTracks([qq, ne], {selections: new Map([[groups[0].id, ne.key]])})[0].selected.key, ne.key);
});
test('never merge covers, live/remix, unknown/different albums, artist subsets or different durations', () => {
  for (const changes of [{title: '晴天 (Live)'}, {title: '晴天 Remix'}, {artist: '另一位歌手'},
    {artist: '周杰伦 / 另一位歌手'}, {album: '—'}, {album: '另一张专辑'}, {duration: 300}]) {
    assert.equal(groupTracks([qq, {...ne, ...changes}]).length, 2, JSON.stringify(changes));
  }
  assert.equal(groupTracks([{...qq, title: '晴天 (Live)'}, {...ne, title: '晴天 (Live)'}]).length, 2);
  assert.equal(recordingSignature({...qq, album: '—'}), null);
});
test('same-platform catalog entries remain separate and input objects are never mutated', () => {
  const before = JSON.stringify([qq, ne]);
  assert.equal(groupTracks([qq, {...qq, key: 'qq:AnotherQQMid123'}]).length, 2);
  groupTracks([qq, ne]); assert.equal(JSON.stringify([qq, ne]), before);
});
test('source filter cannot expose or play a hidden platform; returning to all preserves explicit selection', () => {
  const group = groupTracks([qq, ne])[0], selections = new Map([[group.id, ne.key]]);
  assert.equal(groupTracks([qq, ne], {selections, source: 'qq'})[0].selected.key, qq.key);
  assert.equal(groupTracks([qq, ne], {selections, source: 'qq'})[0].variants.length, 1);
  assert.equal(groupTracks([qq, ne], {selections})[0].selected.key, ne.key);
  assert.equal(groupTracks([qq], {source: 'netease'}).length, 0);
});
test('explicit selection wins over current playback, but a source click itself has no playback side effect', () => {
  const group = groupTracks([qq, ne])[0];
  assert.equal(groupTracks([qq, ne], {current: ne.key})[0].selected.key, ne.key);
  assert.equal(groupTracks([qq, ne], {current: ne.key, selections: new Map([[group.id, qq.key]])})[0].selected.key, qq.key);
});
test('initial unavailable QQ chooses free variant only before explicit user intent', () => {
  const blocked = {...qq, availability: 'unavailable'}, group = groupTracks([blocked, ne])[0];
  assert.equal(group.selected.key, ne.key);
  assert.equal(groupTracks([blocked, ne], {selections: new Map([[group.id, qq.key]])})[0].selected.key, qq.key);
});
test('normalizing does not discard version annotations; equivalent artist order is conservative', () => {
  assert.equal(sameRecording({...qq, artist: '甲 / 乙'}, {...ne, artist: '乙 / 甲'}), true);
  assert.equal(sameRecording(qq, {...ne, title: '晴天 (重制版)'}), false);
});
test('one selected version enters queue, without disturbing a currently playing alternate version', () => {
  const other = {...qq, key: 'qq:AnotherQQMid123', title: '另一首'}, tracks = new Map([qq, ne, other].map(t => [t.key, t]));
  assert.deepEqual(enqueueVersion([qq.key, other.key], ne.key, tracks).queue, [other.key, ne.key]);
  assert.deepEqual(enqueueVersion([qq.key, other.key], ne.key, tracks, {current: qq.key, playing: true}),
    {queue: [qq.key, other.key], blocked: true});
  assert.deepEqual(enqueueVersion([other.key, qq.key], ne.key, tracks, {next: true, current: other.key}).queue, [other.key, ne.key]);
});
test('partial failures preserve successful cursors, and retry does not skip a failed page', () => {
  const first = {qq: {status: 'ready', page: 2, hasMore: true}, netease: {status: 'ready', page: 1, hasMore: true}};
  const failed = mergeSearchPage(first, {netease: {status: 'error', page: 2, code: 'NETWORK_TIMEOUT', hasMore: false}});
  assert.equal(failed.qq, first.qq); assert.equal(failed.netease.page, 1); assert.equal(failed.netease.hasMore, true);
  assert.equal(mergeSearchPage(failed, {netease: {status: 'ready', page: 2, hasMore: false}}).netease.page, 2);
});
test('rank alternates between sources instead of favoring the fastest response', () => {
  assert.deepEqual(interleaveResults({qq: ['q1', 'q2'], netease: ['n1', 'n2', 'n3']}), ['q1', 'n1', 'q2', 'n2', 'n3']);
  assert.deepEqual(interleaveResults({netease: ['n1']}), ['n1']);
  assert.deepEqual(interleaveResults({}), []);
});
