'use strict';
// Sandboxed preload: fixed window/lyric APIs only; never generic IPC, Node, files or accounts.
const {contextBridge, ipcRenderer} = require('electron');
const command = 'talk:window-command';
const stateChannel = 'talk:window-state';

contextBridge.exposeInMainWorld('usicCapabilities', Object.freeze({
  chooseScoreBundle: () => ipcRenderer.invoke('usic:score-bundle'),
}));

contextBridge.exposeInMainWorld('talkWindow', Object.freeze({
  minimize: () => ipcRenderer.invoke(command, 'minimize'),
  toggleMaximize: () => ipcRenderer.invoke(command, 'toggle-maximize'),
  close: () => ipcRenderer.invoke(command, 'close'),
  getState: () => ipcRenderer.invoke(command, 'state'),
  onState: callback => {
    if (typeof callback !== 'function') throw new TypeError('WINDOW_CALLBACK_INVALID');
    const listener = (_event, state) => {
      if (state && typeof state.maximized === 'boolean' && typeof state.minimized === 'boolean' &&
          typeof state.focused === 'boolean') {
        callback({maximized: state.maximized, minimized: state.minimized, focused: state.focused});
      }
    };
    ipcRenderer.on(stateChannel, listener);
    return () => ipcRenderer.removeListener(stateChannel, listener);
  },
}));

contextBridge.exposeInMainWorld('usicLyrics', Object.freeze({
  toggle: () => ipcRenderer.invoke('usic:lyrics-command', 'toggle'),
  unlock: () => ipcRenderer.invoke('usic:lyrics-command', 'unlock'),
  getState: () => ipcRenderer.invoke('usic:lyrics-command', 'state'),
  publish: frame => ipcRenderer.send('usic:lyrics-update', frame),
  onState: callback => {
    if (typeof callback !== 'function') throw new TypeError('LYRICS_CALLBACK_INVALID');
    const listener = (_event, value) => {
      if (value && typeof value.visible === 'boolean' && typeof value.locked === 'boolean') {
        callback({visible: value.visible, locked: value.locked, fontSize: value.fontSize,
          shortcutAvailable: value.shortcutAvailable === true});
      }
    };
    ipcRenderer.on('usic:lyrics-state', listener);
    return () => ipcRenderer.removeListener('usic:lyrics-state', listener);
  },
  onCommand: callback => {
    if (typeof callback !== 'function') throw new TypeError('LYRICS_CALLBACK_INVALID');
    const listener = (_event, value) => {if (['previous', 'toggle-play', 'next'].includes(value)) callback(value);};
    ipcRenderer.on('usic:lyrics-transport', listener);
    return () => ipcRenderer.removeListener('usic:lyrics-transport', listener);
  },
}));
