'use strict';
const {allowedURL} = require('./security.cjs');

const WINDOW_COMMAND = 'talk:window-command';
const WINDOW_STATE = 'talk:window-state';
const commands = new Set(['state', 'minimize', 'toggle-maximize', 'close']);

function windowState(window) {
  return {maximized: window.isMaximized(), minimized: window.isMinimized(), focused: window.isFocused()};
}

function trustedSender(event, window, origin) {
  if (!window || window.isDestroyed() || window.webContents.isDestroyed()) return false;
  const contents = window.webContents;
  return event.sender === contents && event.senderFrame === contents.mainFrame &&
    event.senderFrame?.origin === origin && allowedURL(event.senderFrame?.url, origin);
}

function bindWindowControls(ipcMain, window, origin) {
  const handler = (event, command) => {
    if (!trustedSender(event, window, origin)) throw new Error('WINDOW_CONTROL_DENIED');
    if (!commands.has(command)) throw new Error('WINDOW_COMMAND_INVALID');
    if (command === 'close') {window.close(); return null;}
    if (command === 'minimize') window.minimize();
    if (command === 'toggle-maximize') {
      if (window.isMaximized()) window.unmaximize(); else window.maximize();
    }
    return windowState(window);
  };
  ipcMain.handle(WINDOW_COMMAND, handler);
  const publish = () => {
    if (!window.isDestroyed() && !window.webContents.isDestroyed()) {
      window.webContents.send(WINDOW_STATE, windowState(window));
    }
  };
  const events = ['maximize', 'unmaximize', 'minimize', 'restore', 'focus', 'blur'];
  for (const event of events) window.on(event, publish);
  let disposed = false;
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    ipcMain.removeHandler(WINDOW_COMMAND);
    for (const event of events) window.removeListener(event, publish);
    window.removeListener('closed', dispose);
  };
  window.once('closed', dispose);
  return dispose;
}

module.exports = {WINDOW_COMMAND, WINDOW_STATE, trustedSender, bindWindowControls};
