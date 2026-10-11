'use strict';
function allowedURL(raw, origin) {
  try { const url = new URL(raw); return url.origin === origin && !url.username && !url.password; }
  catch { return false; }
}
function windowPreferences() {
  return {nodeIntegration: false, contextIsolation: true, sandbox: true,
          webSecurity: true, allowRunningInsecureContent: false, webviewTag: false};
}
module.exports = {allowedURL, windowPreferences};
