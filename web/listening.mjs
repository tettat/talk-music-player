// Browser-observed playback, never a claim that a person heard the speakers.
export class ListeningTracker {
  constructor(audio, {send, clock = () => performance.now() / 1000,
      makeId = () => crypto.randomUUID().replaceAll('-', ''), onSaved = () => {}, onError = () => {}}) {
    this.audio = audio; this.send = send; this.clock = clock; this.makeId = makeId;
    this.onSaved = onSaved; this.onError = onError;
    this.key = null; this.duration = 0; this.session = null; this.anchor = null;
    this.buffering = true; this.pending = Promise.resolve(); this.listeners = [];
    this.listen('playing', () => {this.buffering = false; this.start(); this.reset();});
    this.listen('pause', () => {this.sample(true); this.flush('pause');});
    this.listen('waiting', () => {this.sample(true); this.buffering = true; this.reset(); this.flush('pause');});
    this.listen('stalled', () => {this.sample();});
    this.listen('seeking', () => {this.sample(false, true); this.flush('seek');});
    this.listen('seeked', () => this.reset());
    this.listen('volumechange', () => {this.sample(true); this.reset(); this.flush('pause');});
    this.listen('ratechange', () => {this.sample(false, true); this.flush('seek');});
    this.listen('ended', () => this.finish('ended'));
    this.listen('error', () => this.setTrack(null));
    this.listen('emptied', () => {if (this.session) this.setTrack(null);});
  }
  listen(name, handler) {this.audio.addEventListener(name, handler); this.listeners.push([name, handler]);}
  active() {
    return !!this.key && !this.buffering && !this.audio.paused && !this.audio.ended &&
      !this.audio.seeking && !this.audio.muted && this.audio.volume > 0 && this.audio.readyState >= 2;
  }
  reset() {this.anchor = {wall: this.clock(), position: this.audio.currentTime, active: this.active()};}
  setTrack(key, duration = 0) {
    if (this.session) this.finish(key ? 'skip' : 'close');
    this.key = key; this.duration = duration; this.buffering = true; this.reset();
  }
  start() {
    if (!this.key || this.session) return;
    this.session = {id: this.makeId(), key: this.key, seq: 0, seconds: 0, segments: [], lastFlush: this.clock()};
    // Establish a server timestamp before accumulating heartbeats; zero seconds is not a listen.
    this.flush('playing');
  }
  sample(force = false, discard = false) {
    const previous = this.anchor, now = this.clock(), position = this.audio.currentTime;
    const active = this.active(), rate = this.audio.playbackRate || 1;
    this.anchor = {wall: now, position, active};
    if (!this.session || !previous || !previous.active || (!active && !force) || discard) return;
    const wall = now - previous.wall, movement = position - previous.position;
    // A seek, suspended machine, stalled media clock or invalid clock never earns time.
    if (!Number.isFinite(wall) || !Number.isFinite(movement) || wall <= 0 || wall > 15 || movement <= 0 ||
        rate < .25 || rate > 4 || Math.abs(movement - wall * rate) > Math.max(.2, wall * rate * .3)) return;
    const seconds = Math.min(wall, movement / rate);
    const actualDuration = Number.isFinite(this.audio.duration) && this.audio.duration > 0 &&
      this.audio.duration <= this.duration + 2 ? this.audio.duration : this.duration;
    const start = Math.max(0, Math.min(previous.position, actualDuration));
    const end = Math.max(start, Math.min(position, actualDuration));
    if (!(end > start)) return;
    this.session.seconds += seconds;
    const ranges = [...this.session.segments, [start, end]].sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const range of ranges) {
      const last = merged.at(-1);
      if (last && range[0] <= last[1] + .001) last[1] = Math.max(last[1], range[1]);
      else merged.push([...range]);
    }
    // In the extreme seek-heavy case prefer underestimating coverage to inventing heard gaps.
    this.session.segments = merged.slice(0, 1000);
  }
  tick() {
    this.sample();
    if (this.session && this.active() && this.clock() - this.session.lastFlush >= 10) this.flush('playing');
  }
  flush(reason = 'playing', keepalive = false) {
    const session = this.session; if (!session) return this.pending;
    session.lastFlush = this.clock();
    const packet = {id: session.id, key: session.key, seq: ++session.seq,
      seconds: Number(session.seconds.toFixed(4)),
      segments: session.segments.map(([start, end]) => [Number(start.toFixed(4)), Number(end.toFixed(4))]), reason};
    const request = async () => {
      // Same id/seq on retry: a lost HTTP response must not double count a listen.
      try {await this.send(packet, {keepalive}); this.onSaved();}
      catch (error) {
        if (keepalive) {this.onError(error); return;}
        try {await this.send(packet, {keepalive: false}); this.onSaved();}
        catch (retryError) {this.onError(retryError);}
      }
    };
    if (keepalive) return request(); // Unload cannot wait behind another network request.
    this.pending = this.pending.catch(() => {}).then(request);
    return this.pending;
  }
  finish(reason = 'close', keepalive = false) {
    this.sample(true);
    const pending = this.flush(reason, keepalive);
    this.session = null; this.reset();
    return pending;
  }
  drain() {return this.pending;}
  destroy() {this.finish('close'); for (const [name, handler] of this.listeners) this.audio.removeEventListener(name, handler);}
}
