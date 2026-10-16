import test from 'node:test';
import assert from 'node:assert/strict';
import {AudioTransport, abcMeasureIndex, audioURL, formatTime, measureAt, notesAt, position, rollLayout} from '../capabilities/score_lab/web/timeline.mjs';

test('position clamps invalid and out-of-range times', () => {
  assert.equal(position(-1, 30), 0); assert.equal(position(31, 30), 30);
  assert.equal(position(NaN, 30), 0); assert.equal(position(.125, 30), .125);
});
test('time labels are clip-local and stable', () => {
  assert.equal(formatTime(0), '00:00'); assert.equal(formatTime(61.8), '01:01'); assert.equal(formatTime(-2), '00:00');
});
test('full-song note rolls use a bounded viewport, not a giant canvas', () => {
  const first = rollLayout(900, 900), last = rollLayout(900, 900, 1e9);
  assert.equal(first.width, 900); assert.equal(first.total, 30644);
  assert.equal(first.start, 0); assert.equal(last.end, 900);
  assert.equal(last.left, first.total - last.width);
  assert.ok(last.start > 870 && last.start < 900);
  assert.equal(rollLayout(900, 9000).width, 3200);
  assert.equal(rollLayout(300.288, 300, -50).left, 0);
  for (const args of [[0, 0], [NaN, NaN, NaN], [-1, -1]]) {
    assert.ok(Object.values(rollLayout(...args)).every(Number.isFinite));
  }
});
test('measure boundaries use the original-audio map, not BPM', () => {
  const measures = [{index:0,start:.54,end:2.29},{index:1,start:2.29,end:5.79}];
  assert.equal(measureAt(measures, 0), null); assert.equal(measureAt(measures, 2.29).index, 1);
  assert.equal(measureAt(measures, 5.79), null);
});
test('score clicks and follow use the global measure, never the line-local index', () => {
  assert.equal(abcMeasureIndex(['abcjs-note','abcjs-l1','abcjs-m1','abcjs-mm5']),5);
  assert.equal(abcMeasureIndex('abcjs-note abcjs-l0 abcjs-m0 abcjs-mm0'),0);
  assert.equal(abcMeasureIndex('abcjs-staff abcjs-l0'),null);
});
test('notes stay separated by role and have exclusive note-off boundaries', () => {
  const tracks = [{role:'vocal',notes:[{pitch:67,start:.54,end:1.41}]},{role:'chords',notes:[{pitch:48,start:0,end:3}]}];
  assert.equal(notesAt(tracks, .8).length, 2); assert.equal(notesAt(tracks, .8, 'vocal').length, 1);
  assert.equal(notesAt(tracks, 1.41, 'vocal').length, 0);
});
test('audio URLs cannot traverse or invent a source', () => {
  assert.equal(audioURL('sunny','piano','vocal'), '/media/sunny/piano_vocal.wav');
  assert.equal(audioURL('sunny','original','mix'), '/media/sunny/original.wav');
  for (const args of [['../x','piano','mix'], ['sunny','piano','../../secret'], ['sunny','fake','mix']]) {
    assert.throws(() => audioURL(...args));
  }
});

class FakeAudio extends EventTarget {
  constructor(){super();this.paused=true;this.duration=30;this.currentTime=0;this.src='';this.playCount=0;}
  load(){if(this.src)queueMicrotask(()=>this.dispatchEvent(new Event('loadedmetadata')));}
  pause(){this.paused=true;}
  async play(){this.paused=false;this.playCount++;}
  removeAttribute(name){if(name==='src')this.src='';}
}
test('switching source preserves the requested original-second playhead', async () => {
  const audio = new FakeAudio(), transport = new AudioTransport(audio);
  assert.equal(await transport.load('/original.wav', 12.25, false), true);
  assert.equal(audio.currentTime, 12.25); assert.equal(audio.paused, true);
  await transport.load('/piano_mix.wav', 12.25, true);
  assert.equal(audio.currentTime, 12.25); assert.equal(audio.paused, false); assert.equal(audio.playCount, 1);
});
test('racing selections never autoplay a stale track', async () => {
  const audio = new FakeAudio(), transport = new AudioTransport(audio);
  const first = transport.load('/old.wav', 1, true);
  const second = transport.load('/new.wav', 6, false);
  assert.equal(await first, false); assert.equal(await second, true);
  assert.equal(audio.src, '/new.wav'); assert.equal(audio.currentTime, 6); assert.equal(audio.playCount, 0);
});
test('stop cancels pending loading and releases the audio', async () => {
  const audio = new FakeAudio(), transport = new AudioTransport(audio);
  const pending = transport.load('/old.wav', 8, true); transport.stop();
  assert.equal(await pending, false); assert.equal(audio.paused, true); assert.equal(audio.src, '');
  assert.equal(transport.loading, false); assert.equal(audio.playCount, 0);
});
