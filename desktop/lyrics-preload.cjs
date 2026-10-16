'use strict';
// This file-only window receives bounded display text, never accounts, audio URLs or generic IPC.
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('usicLyricOverlay', Object.freeze({
  getSnapshot: () => ipcRenderer.invoke('usic:lyrics-command', 'snapshot'),
  close: () => ipcRenderer.invoke('usic:lyrics-command', 'close'),
  toggleLock: () => ipcRenderer.invoke('usic:lyrics-command', 'toggle-lock'),
  fontUp: () => ipcRenderer.invoke('usic:lyrics-command', 'font-up'),
  fontDown: () => ipcRenderer.invoke('usic:lyrics-command', 'font-down'),
  previous: () => ipcRenderer.invoke('usic:lyrics-command', 'previous'),
  togglePlay: () => ipcRenderer.invoke('usic:lyrics-command', 'toggle-play'),
  next: () => ipcRenderer.invoke('usic:lyrics-command', 'next'),
  onSnapshot: callback => {
    if (typeof callback !== 'function') throw new TypeError('LYRICS_CALLBACK_INVALID');
    const listener = (_event, value) => {if (value && value.frame && typeof value.fontSize === 'number') callback(value);};
    ipcRenderer.on('usic:lyrics-snapshot', listener);
    return () => ipcRenderer.removeListener('usic:lyrics-snapshot', listener);
  },
}));
