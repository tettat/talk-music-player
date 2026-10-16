'use strict';
(() => {
  const bridge = window.usicLyricOverlay;
  if (!bridge) return;
  const $ = id => document.getElementById(id);
  let previousLine = '';
  function render(snapshot) {
    const {frame, fontSize, locked, shortcutAvailable} = snapshot;
    document.documentElement.style.setProperty('--lyric-size', fontSize + 'px');
    document.body.classList.toggle('is-locked', locked);
    document.body.dataset.status = frame.status;
    $('song').textContent = [frame.title, frame.artist].filter(Boolean).join(' · ');
    $('song').title = $('song').textContent;
    $('current').textContent = frame.current;
    $('next-line').textContent = frame.next;
    if (frame.current !== previousLine) {
      $('current').classList.remove('is-changing');
      void $('current').offsetWidth;
      $('current').classList.add('is-changing');
      previousLine = frame.current;
    }
    $('play-icon').setAttribute('href', frame.playing ? '#pause' : '#play');
    $('play-button').setAttribute('aria-label', frame.playing ? '暂停' : '播放');
    $('play-button').title = frame.playing ? '暂停' : '播放';
    $('play-button').disabled = !frame.key;
    $('previous-button').disabled = $('next-button').disabled = !frame.canSkip;
    $('font-down').disabled = fontSize <= 24;
    $('font-up').disabled = fontSize >= 48;
    $('lock-button').title = shortcutAvailable ? '锁定 · Ctrl+Alt+L 或播放器右键解锁' : '锁定 · 播放器右键解锁';
  }
  for (const [id, action] of [['previous-button', 'previous'], ['play-button', 'togglePlay'],
    ['next-button', 'next'], ['font-down', 'fontDown'], ['font-up', 'fontUp'],
    ['lock-button', 'toggleLock'], ['close-button', 'close']]) {
    $(id).addEventListener('click', () => Promise.resolve(bridge[action]()).catch(() => {}));
  }
  const unsubscribe = bridge.onSnapshot(render);
  bridge.getSnapshot().then(render).catch(() => {});
  document.body.classList.add('is-peeking');
  setTimeout(() => document.body.classList.remove('is-peeking'), 2200);
  window.addEventListener('pagehide', unsubscribe, {once: true});
})();
