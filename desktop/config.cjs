'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

function loadConfig(root = path.resolve(__dirname, '..'), env = process.env) {
  root = path.resolve(root);
  const values = {
    schema_version: 1,
    python: process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
    data_dir: '.private', qq_session_dir: '.private/qq',
    analysis_python: process.platform === 'win32' ? '.private/runtime-analysis/Scripts/python.exe' : '.private/runtime-analysis/bin/python',
    basic_pitch_model: '.private/models/basic_pitch/nmp.onnx', port: 43180, ttl_seconds: 86400,
  };
  const file = path.resolve(root, env.TALK_CONFIG_FILE ?? 'config.local.json');
  if (fs.existsSync(file)) {
    if (fs.statSync(file).size > 32768) throw new Error('CONFIG_SIZE_REJECTED');
    let data;
    try { data = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); }
    catch { throw new Error('CONFIG_JSON_INVALID'); }
    if (!data || typeof data !== 'object' || Array.isArray(data) ||
        Object.keys(data).some(key => !Object.hasOwn(values, key))) throw new Error('CONFIG_KEYS_INVALID');
    Object.assign(values, data);
  } else if (Object.hasOwn(env, 'TALK_CONFIG_FILE')) throw new Error('CONFIG_FILE_NOT_FOUND');
  if (values.schema_version !== 1) throw new Error('CONFIG_VERSION_INVALID');
  for (const key of ['python', 'data_dir', 'qq_session_dir', 'analysis_python', 'basic_pitch_model']) {
    let value = env[`TALK_${key.toUpperCase()}`] ?? values[key];
    if (typeof value !== 'string' || !value.trim() || value.includes('\0')) throw new Error('CONFIG_PATH_INVALID');
    if (/^~[/\\]/.test(value)) value = path.join(os.homedir(), value.slice(2));
    values[key] = path.resolve(root, value);
  }
  if (!Number.isInteger(values.port) || values.port < 1024 || values.port > 65535 ||
      !Number.isInteger(values.ttl_seconds) || values.ttl_seconds < 60 || values.ttl_seconds > 172800) {
    throw new Error('CONFIG_NUMBER_INVALID');
  }
  return Object.freeze(values);
}

module.exports = {loadConfig};
