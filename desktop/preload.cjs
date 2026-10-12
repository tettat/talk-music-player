'use strict';
// Sandboxed preload: window controls only, never generic IPC, Node, files or accounts.
const {contextBridge, ipcRenderer} = require('electron');
const command = 'talk:window-command';
const stateChannel = 'talk:window-state';

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
