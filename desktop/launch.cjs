'use strict';
function desktopLaunchOptions(environment) {
  const env = {...environment};
  delete env.ELECTRON_RUN_AS_NODE;
  // This is the interactive GUI, not the hidden Python helper. SW_HIDE can make
  // the native window invisible even when its renderer and backend are healthy.
  return {env, shell: false, windowsHide: false, stdio: 'inherit'};
}
module.exports = {desktopLaunchOptions};
