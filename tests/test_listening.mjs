// Synthetic clocks/events are contracts, not evidence of real speaker output.
import {test} from 'node:test';
import assert from 'node:assert/strict';
const {ListeningTracker} = await import(process.env.TALK_TEST_PREVIEW === '1' ? '../web-next/listening.mjs' : '../web/listening.mjs');

test('paused and zero-volume clocks do not emit periodic playing heartbeats', async () => {
  const t = setup(); t.play(); t.step(3);
  t.audio.paused = true; t.audio.emit('pause'); await t.tracker.drain();
  const count = t.packets.length; t.step(25, 0); await t.tracker.drain();
  assert.equal(t.packets.length, count); assert.equal(t.packets.at(-1).reason, 'pause');
  t.audio.volume = 0; t.audio.paused = false; t.audio.emit('playing');
  t.step(25, 25); await t.tracker.drain(); assert.equal(t.packets.length, count);
});

class Audio extends EventTarget {
  paused = true; ended = false; seeking = false; muted = false; volume = .8;
  readyState = 4; currentTime = 0; duration = 120; playbackRate = 1;
  emit(type) {this.dispatchEvent(new Event(type));}
}
function setup(sendOverride) {
  const audio = new Audio(), packets = []; let now = 0, ids = 0;
  const tracker = new ListeningTracker(audio, {clock: () => now, makeId: () => String(++ids).padStart(32, '0'),
    send: sendOverride || (async packet => {packets.push(structuredClone(packet));})});
  tracker.setTrack('0039MnYb0qxYhV', 120);
  const step = (wall, movement = wall) => {now += wall; audio.currentTime += movement; tracker.tick();};
  const play = () => {audio.paused = false; audio.ended = false; audio.emit('playing');};
  return {audio, tracker, packets, step, play};
}

test('loading is not a listen', async () => {
  const t = setup(); t.step(10, 0); await t.tracker.drain(); assert.equal(t.packets.length, 0);
});
test('continuous playback reports real elapsed time and coverage', async () => {
  const t = setup(); t.play(); for (let i = 0; i < 10; i++) t.step(1);
  await t.tracker.drain(); const p = t.packets.at(-1);
  assert.equal(p.seconds, 10); assert.deepEqual(p.segments, [[0, 10]]); assert.equal(p.seq, 2);
});
test('pause excludes elapsed wall time and resume keeps one session', async () => {
  const t = setup(); t.play(); t.step(3); t.audio.paused = true; t.audio.emit('pause');
  t.step(20, 0); t.play(); t.step(2); t.tracker.flush(); await t.tracker.drain();
  assert.equal(t.packets.at(-1).seconds, 5); assert.equal(new Set(t.packets.map(p => p.id)).size, 1);
});
test('waiting and stalled media clock do not earn time', async () => {
  const t = setup(); t.play(); t.step(2); t.audio.emit('waiting'); t.step(5, 0);
  t.play(); t.step(2); t.step(3, 0); t.tracker.flush(); await t.tracker.drain();
  assert.equal(t.packets.at(-1).seconds, 4);
});
test('forward seek never credits jumped audio', async () => {
  const t = setup(); t.play(); t.step(2); t.audio.seeking = true; t.audio.currentTime = 100;
  t.audio.emit('seeking'); t.audio.seeking = false; t.audio.emit('seeked'); t.step(2);
  t.tracker.flush(); await t.tracker.drain();
  assert.equal(t.packets.at(-1).seconds, 4); assert.deepEqual(t.packets.at(-1).segments, [[0, 2], [100, 102]]);
});
test('replayed section adds time but does not duplicate coverage', async () => {
  const t = setup(); t.play(); t.step(3); t.audio.seeking = true; t.audio.currentTime = 0; t.audio.emit('seeking');
  t.audio.seeking = false; t.audio.emit('seeked'); t.step(3); t.tracker.flush(); await t.tracker.drain();
  assert.equal(t.packets.at(-1).seconds, 6); assert.deepEqual(t.packets.at(-1).segments, [[0, 3]]);
});
test('muted audio and volume zero are excluded', async () => {
  const t = setup(); t.play(); t.step(2); t.audio.muted = true; t.audio.emit('volumechange'); t.step(3);
  t.audio.muted = false; t.audio.volume = 0; t.audio.emit('volumechange'); t.step(3);
  t.audio.volume = .8; t.audio.emit('volumechange'); t.step(2); t.tracker.flush(); await t.tracker.drain();
  assert.equal(t.packets.at(-1).seconds, 4);
});
test('sleep gaps and unknown position jumps are discarded', async () => {
  const t = setup(); t.play(); t.step(2); t.step(60, 60); t.step(1, 30); t.step(1);
  t.tracker.flush(); await t.tracker.drain(); assert.equal(t.packets.at(-1).seconds, 3);
});
test('track switch closes preceding session and creates new id', async () => {
  const t = setup(); t.play(); t.step(2); t.tracker.setTrack('00123456789012', 120); t.play(); t.step(2);
  t.tracker.flush(); await t.tracker.drain();
  assert.equal(t.packets.find(p => p.reason === 'skip').seconds, 2);
  assert.equal(new Set(t.packets.map(p => p.id)).size, 2);
});
test('single-track loop starts a new listening session', async () => {
  const t = setup(); t.play(); t.step(2); t.audio.ended = true; t.audio.emit('ended');
  t.audio.currentTime = 0; t.play(); t.step(2); t.tracker.flush(); await t.tracker.drain();
  assert.equal(t.packets.filter(p => p.reason === 'ended').length, 1);
  assert.equal(new Set(t.packets.map(p => p.id)).size, 2);
});
test('retry uses identical id/sequence and serialized order', async () => {
  const attempts = []; let fail = true;
  const t = setup(async packet => {attempts.push(structuredClone(packet)); if (fail) {fail = false; throw Error('offline');}});
  t.play(); t.step(2); t.tracker.flush('pause'); await t.tracker.drain();
  assert.equal(attempts.length, 3); assert.deepEqual(attempts[0], attempts[1]); assert.equal(attempts[2].seq, 2);
});
test('close uses keepalive and no later ticks create phantom records', async () => {
  const calls = []; const t = setup(async (packet, options) => calls.push({packet, options}));
  t.play(); await t.tracker.drain(); t.step(2); await t.tracker.finish('close', true); t.step(3);
  assert.equal(calls.at(-1).options.keepalive, true); assert.equal(calls.at(-1).packet.seconds, 2);
  assert.equal(calls.length, 2);
});
