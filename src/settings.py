"""Device-independent defaults plus explicit, git-ignored machine configuration."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'schema_version': 1,
    'python': '.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python',
    'data_dir': '.private',
    'qq_session_dir': '.private/qq',
    'analysis_python': '.private/runtime-analysis/Scripts/python.exe' if os.name == 'nt'
                       else '.private/runtime-analysis/bin/python',
    'basic_pitch_model': '.private/models/basic_pitch/nmp.onnx',
    'port': 43180,
    'ttl_seconds': 86400,
}
PATH_KEYS = ('python', 'data_dir', 'qq_session_dir', 'analysis_python', 'basic_pitch_model')


@dataclass(frozen=True)
class Settings:
    python: Path
    data_dir: Path
    qq_session_dir: Path
    analysis_python: Path
    basic_pitch_model: Path
    port: int
    ttl_seconds: int


def load_settings(root=ROOT, environ=None):
    env = os.environ if environ is None else environ
    root = Path(root).resolve()
    config = Path(env.get('TALK_CONFIG_FILE', str(root / 'config.local.json')))
    if not config.is_absolute():
        config = root / config
    values = dict(DEFAULTS)
    if config.is_file():
        if config.stat().st_size > 32768:
            raise ValueError('CONFIG_SIZE_REJECTED')
        try:
            data = json.loads(config.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError):
            raise ValueError('CONFIG_JSON_INVALID') from None
        if not isinstance(data, dict) or set(data) - set(DEFAULTS):
            raise ValueError('CONFIG_KEYS_INVALID')
        values.update(data)
    elif 'TALK_CONFIG_FILE' in env:
        raise ValueError('CONFIG_FILE_NOT_FOUND')
    if type(values['schema_version']) is not int or values['schema_version'] != 1:
        raise ValueError('CONFIG_VERSION_INVALID')
    for name in PATH_KEYS:
        value = env.get('TALK_' + name.upper(), values[name])
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ValueError('CONFIG_PATH_INVALID')
        path = Path(value).expanduser()
        values[name] = (path if path.is_absolute() else root / path).resolve()
    for name, minimum, maximum in (('port', 1024, 65535), ('ttl_seconds', 60, 172800)):
        value = values[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError('CONFIG_NUMBER_INVALID')
    return Settings(**{name: values[name] for name in Settings.__dataclass_fields__})


SETTINGS = load_settings()
