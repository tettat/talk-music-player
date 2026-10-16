export function position(value, duration) {
  return Math.max(0, Math.min(Number.isFinite(value) ? value : 0, duration || 0));
}

export function formatTime(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

export function measureAt(measures, seconds) {
  return measures.find(m => m.start <= seconds && seconds < m.end) ?? null;
}

export function abcMeasureIndex(classes) {
  // abcjs-mN restarts on each system; abcjs-mmN is the global measure.
  const match = String(classes).match(/abcjs-mm(\d+)/);
  return match ? Number(match[1]) : null;
}

export function rollLayout(duration, viewport, scroll = 0) {
  duration = Number.isFinite(duration) ? Math.max(0, duration) : 0;
  const width = Math.max(200, Math.min(3200, Number.isFinite(viewport) ? viewport : 200));
  const total = Math.max(width, duration * 34 + 44);
  const left = Math.max(0, Math.min(Number.isFinite(scroll) ? scroll : 0, total - width));
  const scale = duration > 0 ? (total - 44) / duration : 1;
  return {width, total, left, scale, start: Math.min(duration, Math.max(0, (left - 34) / scale)),
    end: Math.min(duration, (left + width - 34) / scale)};
}

export function notesAt(tracks, seconds, part = 'mix') {
  return tracks.filter(t => part === 'mix' || t.role === part)
    .flatMap(t => t.notes.filter(n => n.start <= seconds && seconds < n.end));
}

export function audioURL(id, mode, part) {
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(id)) throw new Error('Invalid sample');
  if (!['mix', 'vocal', 'instrumental', 'chords'].includes(part)) throw new Error('Invalid part');
  if (!['original', 'piano'].includes(mode)) throw new Error('Invalid mode');
  return `/media/${id}/${mode === 'original' ? 'original' : `piano_${part}`}.wav`;
}

export class AudioTransport {
  constructor(audio) {
    this.audio = audio;
    this.generation = 0;
    this.controller = null;
    this.loading = false;
  }

  async load(url, seconds = 0, resume = false) {
    this.controller?.abort();
    const version = ++this.generation;
    const controller = this.controller = new AbortController();
    this.loading = true;
    this.audio.pause();
    this.audio.src = url;
    try {
      await new Promise((resolve, reject) => {
        let timer;
        const finish = error => {
          clearTimeout(timer);
          this.audio.removeEventListener('loadedmetadata', ready);
          this.audio.removeEventListener('error', failed);
          controller.signal.removeEventListener('abort', aborted);
          error ? reject(error) : resolve();
        };
        const ready = () => finish();
        const failed = () => finish(new Error('音频加载失败'));
        const aborted = () => finish(new Error('cancelled'));
        this.audio.addEventListener('loadedmetadata', ready);
        this.audio.addEventListener('error', failed);
        controller.signal.addEventListener('abort', aborted, {once: true});
        timer = setTimeout(() => finish(new Error('音频加载超时')), 15000);
        this.audio.load();
      });
      if (version !== this.generation) return false;
      this.audio.currentTime = position(seconds, this.audio.duration);
      this.loading = false;
      if (resume) await this.audio.play();
      return version === this.generation;
    } catch (error) {
      if (controller.signal.aborted || version !== this.generation) return false;
      this.loading = false;
      throw error;
    }
  }

  stop() {
    ++this.generation;
    this.controller?.abort();
    this.controller = null;
    this.loading = false;
    this.audio.pause();
    this.audio.removeAttribute('src');
    this.audio.load();
  }

  seek(seconds) {
    if (Number.isFinite(this.audio.duration)) this.audio.currentTime = position(seconds, this.audio.duration);
  }
}
