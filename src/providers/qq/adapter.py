"""Standalone SDK guard and protected session storage, extracted from the prototype."""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys

from settings import SETTINGS

ROOT = Path(__file__).resolve().parent
PIN = '0.8.2'


def source_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def guard():
    if sys.version_info[:2] != (3, 12):
        return 'PYTHON_3_12_REQUIRED'
    expected_prefix = SETTINGS.python.parent.parent.resolve()
    if Path(sys.prefix).resolve() != expected_prefix:
        return 'CONFIGURED_VENV_REQUIRED'
    try:
        dist = importlib.metadata.distribution('qqmusic-api-python')
        if dist.version != PIN:
            return 'SDK_VERSION_MISMATCH'
        lock = json.loads((ROOT / 'sources.lock.json').read_text(encoding='utf-8'))
        for name in lock['runtimeDependencyNames']:
            importlib.metadata.version(name)
        for relative, expected in lock['pythonSourceSha256'].items():
            path = Path(dist.locate_file(relative))
            if not path.is_file() or source_hash(path) != expected:
                return 'SDK_SOURCE_PIN_MISMATCH'
    except importlib.metadata.PackageNotFoundError:
        return 'SDK_DEPENDENCY_MISSING'
    return None


def private_path(name):
    root = SETTINGS.qq_session_dir.resolve()
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError('PRIVATE_PATH_ESCAPE')
    root.mkdir(parents=True, exist_ok=True)
    return path


def isolate():
    root = SETTINGS.data_dir / 'sdk-sandbox'
    for key, relative in {'HOME': 'home', 'USERPROFILE': 'home',
                          'APPDATA': 'home/appdata', 'LOCALAPPDATA': 'home/localappdata',
                          'XDG_CONFIG_HOME': 'config', 'XDG_CACHE_HOME': 'cache',
                          'PIP_CACHE_DIR': 'cache/pip', 'UV_CACHE_DIR': 'cache/uv',
                          'TEMP': 'tmp', 'TMP': 'tmp'}.items():
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    SETTINGS.qq_session_dir.mkdir(parents=True, exist_ok=True)
    logging.disable(logging.CRITICAL)


def local_module(name):
    if name not in ('windows_secret', 'member_source', 'cookie_transport'):
        raise ValueError('LOCAL_MODULE_REJECTED')
    spec = importlib.util.spec_from_file_location('talk_qq_' + name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save_credential(credential):
    local_module('windows_secret').save(private_path('credential.dpapi'), credential.model_dump_json())
