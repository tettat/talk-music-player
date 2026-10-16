'use strict';
// Native, user-selected capability directory only. No arbitrary renderer path API.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {trustedSender} = require('./window-controls.cjs');
const channel = 'usic:score-bundle';

function contained(root, relative) {
  if (typeof relative !== 'string' || !relative || relative.includes('\\') || relative.includes('\0') ||
      path.posix.isAbsolute(relative) || relative.split('/').some(p => !p || p === '.' || p === '..' || p.includes(':'))) {
    throw new Error('SCORE_BUNDLE_PATH_INVALID');
  }
  const file = path.resolve(root, relative);
  if (!fs.realpathSync(file).startsWith(fs.realpathSync(root) + path.sep)) throw new Error('SCORE_BUNDLE_PATH_INVALID');
  return file;
}
async function hashFile(file) {
  const hash = crypto.createHash('sha256');
  for await (const block of fs.createReadStream(file)) hash.update(block);
  return hash.digest('hex');
}
async function verifyBundle(folder, appRoot) {
  const manifestFile = path.join(folder, 'score-pack.json');
  if (fs.statSync(manifestFile).size > 8 * 1024 * 1024) throw new Error('SCORE_BUNDLE_INVALID');
  const manifest = JSON.parse(await fs.promises.readFile(manifestFile, 'utf8'));
  if (manifest.schema !== 1 || !manifest.files || typeof manifest.files !== 'object' ||
      Array.isArray(manifest.files) || Object.keys(manifest.files).length > 60000) throw new Error('SCORE_BUNDLE_INVALID');
  const values = {};
  for (const key of ['python', 'sheetRepo', 'baseRepo', 'assetsDir']) values[key] = contained(folder, manifest[key]);
  if (!fs.statSync(values.python).isFile()) throw new Error('SCORE_BUNDLE_INVALID');
  // All runtime and model files are verified, not just a marker file.
  for (const [relative, expected] of Object.entries(manifest.files)) {
    if (!/^[a-f0-9]{64}$/.test(expected)) throw new Error('SCORE_BUNDLE_INVALID');
    const file = contained(folder, relative);
    if (!fs.statSync(file).isFile() || await hashFile(file) !== expected) throw new Error('SCORE_BUNDLE_HASH_MISMATCH');
  }
  if (!(manifest.python in manifest.files)) throw new Error('SCORE_BUNDLE_INVALID');
  for (const [key, files] of Object.entries(JSON.parse(fs.readFileSync(path.join(appRoot,
      'capabilities/score_lab/model.lock.json'), 'utf8')).repositories)) {
    for (const [relative, expected] of Object.entries(files)) {
      if (manifest.files[manifest[key] + '/' + relative] !== expected) throw new Error('SCORE_BUNDLE_MODEL_MISMATCH');
    }
  }
  const assets = JSON.parse(fs.readFileSync(path.join(appRoot, 'capabilities/score_lab/render-assets.lock.json'), 'utf8')).files;
  for (const [relative, expected] of Object.entries(assets)) {
    if (manifest.files[manifest.assetsDir + '/' + relative] !== expected) throw new Error('SCORE_BUNDLE_ASSETS_MISMATCH');
  }
  return {schema_version: 1, ...values};
}

function bindScoreBundle({ipcMain, dialog, window, origin, root, dataDir}) {
  let busy = false;
  ipcMain.handle(channel, async event => {
    if (!trustedSender(event, window, origin) || busy) throw new Error('SCORE_BUNDLE_DENIED');
    busy = true;
    try {
      const chosen = await dialog.showOpenDialog(window, {title: '选择谱室能力包文件夹',
        properties: ['openDirectory'], buttonLabel: '使用此能力包'});
      if (chosen.canceled || chosen.filePaths.length !== 1) return {cancelled: true};
      const bundle = await verifyBundle(chosen.filePaths[0], root);
      const folder = path.join(dataDir, 'extensions/score-lab');
      await fs.promises.mkdir(folder, {recursive: true});
      const config = {python: bundle.python, sheetRepo: bundle.sheetRepo, baseRepo: bundle.baseRepo,
        assetsDir: bundle.assetsDir, dataDir: path.join(folder, 'data'), device: 'cpu', threads: 2,
        port: 0, jobTimeoutSeconds: 3600};
      const pending = path.join(folder, 'bundle-config.json.' + crypto.randomBytes(8).toString('hex') + '.tmp');
      await fs.promises.writeFile(pending, JSON.stringify(config, null, 2));
      await fs.promises.rename(pending, path.join(folder, 'bundle-config.json'));
      return {ready: true};
    } finally {busy = false;}
  });
  window.once('closed', () => ipcMain.removeHandler(channel));
}
module.exports = {contained, verifyBundle, bindScoreBundle};
