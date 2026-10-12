// Modal dialogs make the rest of the document inert, regardless of z-index.
// Keep this one control group in the active top-layer dialog, at viewport coordinates.
export function keepControlsAboveDialogs(controls, doc) {
  const home = controls.parentElement;
  const Observer = doc.defaultView?.MutationObserver;
  if (!home || !Observer) return () => {};
  const dialogs = [...doc.querySelectorAll('dialog')], open = [];
  const sync = records => {
    for (const {target} of records) {
      const index = open.indexOf(target);
      if (index !== -1) open.splice(index, 1);
      if (target.open) open.push(target);
    }
    const host = open.findLast(dialog => dialog.open) || home;
    if (controls.parentElement !== host) host.appendChild(controls);
  };
  const observer = new Observer(sync);
  for (const dialog of dialogs) observer.observe(dialog, {attributes: true, attributeFilter: ['open']});
  sync(dialogs.map(target => ({target})));
  return () => {observer.disconnect(); if (controls.parentElement !== home) home.appendChild(controls);};
}

// Optional desktop-only controls. Ordinary browser/mobile pages never show them.
export function installWindowControls(bridge = globalThis.talkWindow, doc = globalThis.document) {
  const methods = ['minimize', 'toggleMaximize', 'close', 'getState', 'onState'];
  if (!bridge || !methods.every(name => typeof bridge[name] === 'function')) return () => {};
  const controls = doc.getElementById('window-controls');
  const minimize = doc.getElementById('window-minimize');
  const maximize = doc.getElementById('window-maximize');
  const close = doc.getElementById('window-close');
  if (!controls || !minimize || !maximize || !close) return () => {};
  let disposed = false;
  const update = state => {
    if (disposed || !state || typeof state.maximized !== 'boolean') return;
    controls.classList.toggle('is-maximized', state.maximized);
    const label = state.maximized ? '还原' : '最大化';
    maximize.title = label;
    maximize.setAttribute('aria-label', label);
    doc.documentElement.classList.toggle('is-window-blurred', state.focused === false);
  };
  const bindings = [[minimize, 'minimize'], [maximize, 'toggleMaximize'], [close, 'close']].map(([button, action]) => {
    const listener = async () => {
      if (button.disabled) return;
      button.disabled = true;
      try {update(await bridge[action]());} catch { /* Native Alt+F4 remains available. */ }
      finally {button.disabled = false;}
    };
    button.addEventListener('click', listener);
    return [button, listener];
  });
  const unsubscribe = bridge.onState(update);
  const releaseDialogs = keepControlsAboveDialogs(controls, doc);
  doc.documentElement.classList.add('is-desktop');
  controls.hidden = false;
  Promise.resolve(bridge.getState()).then(update).catch(() => {});
  return () => {
    if (disposed) return;
    disposed = true;
    unsubscribe();
    releaseDialogs();
    for (const [button, listener] of bindings) button.removeEventListener('click', listener);
    controls.hidden = true;
    doc.documentElement.classList.remove('is-desktop', 'is-window-blurred');
  };
}
