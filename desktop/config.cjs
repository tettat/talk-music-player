'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

function resolveConfigPath(base, value) {
  const absolute = path.resolve(base, value), tail = [];
  let existing = absolute;
  while (true) {
    try {return path.resolve(fs.realpathSync.native(existing), ...tail.reverse());}
    catch (error) {
      if (!['ENOENT', 'ENOTDIR'].includes(error.code)) throw error;
      const parent = path.dirname(existing);
      if (parent === existing) return absolute;
      tail.push(path.basename(existing)); existing = parent;
    }
  }
}

function loadConfig(root = path.resolve(__dirname, '..'), env = process.env) {
  root = resolveConfigPath(root, '.');
  const values = {
    schema_version: 1,
    python: process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
    data_dir: '.private', qq_session_dir: '.private/qq',
    analysis_python: process.platform === 'win32' ? '.private/runtime-analysis/Scripts/python.exe' : '.private/runtime-analysis/bin/python',
    basic_pitch_model: '.private/models/basic_pitch/nmp.onnx', port: 43180, ttl_seconds: 86400,
  };
  const packaged = env.TALK_PACKAGED === '1';
  if (packaged) {
    for (const name of ['TALK_RUNTIME_ROOT', 'TALK_USER_DATA']) {
      if (typeof env[name] !== 'string' || !env[name].trim() || env[name].includes('\0') ||
          !path.isAbsolute(env[name]) || (process.platform === 'win32' && path.parse(env[name]).root === path.sep)) {
        throw new Error('CONFIG_PACKAGED_PATH_INVALID');
      }
    }
    const runtime = resolveConfigPath(root, env.TALK_RUNTIME_ROOT), data = resolveConfigPath(root, env.TALK_USER_DATA);
    Object.assign(values, {
      python: path.join(runtime, 'base/python/Scripts/python.exe'), data_dir: data,
      qq_session_dir: path.join(data, 'qq'),
      analysis_python: path.join(data, 'capabilities/basic-pitch/python/python.exe'),
      basic_pitch_model: path.join(data, 'capabilities/basic-pitch/models/nmp.onnx'),
    });
  }
  // Never discover a checkout's machine configuration in an installed app.
  const file = path.resolve(root, env.TALK_CONFIG_FILE ??
    (packaged ? path.join(env.TALK_USER_DATA, 'config.json') : 'config.local.json'));
  if (fs.statSync(file, {throwIfNoEntry: false})?.isFile()) {
    if (fs.statSync(file).size > 32768) throw new Error('CONFIG_SIZE_REJECTED');
    let data;
    try {
      data = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''), (key, value, context) => {
        // Preserve Python's rejection of float/exponent spellings, even 1.0,
        // without changing validation priority for unknown/nested keys.
        if (typeof value === 'number' && context?.source && !/^-?(?:0|[1-9]\d*)$/.test(context.source)) {
          return NaN;
        }
        return value;
      });
    } catch (error) {
      if (/^CONFIG_(?:VERSION|NUMBER)_INVALID$/.test(error.message)) throw error;
      throw new Error('CONFIG_JSON_INVALID');
    }
    if (!data || typeof data !== 'object' || Array.isArray(data) ||
        Object.keys(data).some(key => !Object.hasOwn(values, key))) throw new Error('CONFIG_KEYS_INVALID');
    Object.assign(values, data);
  } else if (Object.hasOwn(env, 'TALK_CONFIG_FILE')) throw new Error('CONFIG_FILE_NOT_FOUND');
  if (values.schema_version !== 1) throw new Error('CONFIG_VERSION_INVALID');
  for (const key of ['python', 'data_dir', 'qq_session_dir', 'analysis_python', 'basic_pitch_model']) {
    let value = env[`TALK_${key.toUpperCase()}`] ?? values[key];
    if (typeof value !== 'string' || !value.trim() || value.includes('\0')) throw new Error('CONFIG_PATH_INVALID');
    if (/^~[/\\]/.test(value)) value = path.join(os.homedir(), value.slice(2));
    values[key] = resolveConfigPath(packaged ? env.TALK_USER_DATA : root, value);
  }
  if (packaged) {
    const resources = resolveConfigPath(env.TALK_RUNTIME_ROOT, '..');
    for (const key of ['data_dir', 'qq_session_dir']) {
      const relative = path.relative(resources, values[key]);
      if (relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative))) {
        throw new Error('CONFIG_DATA_IN_RESOURCES');
      }
    }
  }
  if (!Number.isInteger(values.port) || values.port < 1024 || values.port > 65535 ||
      !Number.isInteger(values.ttl_seconds) || values.ttl_seconds < 60 || values.ttl_seconds > 172800) {
    throw new Error('CONFIG_NUMBER_INVALID');
  }
  return Object.freeze(values);
}

module.exports = {loadConfig, resolveConfigPath};
