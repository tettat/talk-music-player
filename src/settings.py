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
    if env.get('TALK_PACKAGED') == '1':
        for name in ('TALK_RUNTIME_ROOT', 'TALK_USER_DATA'):
            value = env.get(name)
            if (not isinstance(value, str) or not value.strip() or '\x00' in value
                    or not Path(value).is_absolute()):
                raise ValueError('CONFIG_PACKAGED_PATH_INVALID')
        runtime, private = Path(env['TALK_RUNTIME_ROOT']).resolve(), Path(env['TALK_USER_DATA']).resolve()
        values.update(python=str(runtime / 'base/python/Scripts/python.exe'), data_dir=str(private),
                      qq_session_dir=str(private / 'qq'),
                      analysis_python=str(private / 'capabilities/basic-pitch/python/python.exe'),
                      basic_pitch_model=str(private / 'capabilities/basic-pitch/models/nmp.onnx'))
        if 'TALK_CONFIG_FILE' not in env:
            config = private / 'config.json'
    if config.is_file():
        if config.stat().st_size > 32768:
            raise ValueError('CONFIG_SIZE_REJECTED')
        try:
            data = json.loads(config.read_text(encoding='utf-8-sig'),
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
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
        # Same expansion contract as desktop/config.cjs (not ~other-user or bare ~).
        if value.startswith(('~/', '~\\')):
            value = str(Path.home() / value[2:])
        path = Path(value)
        base = Path(env['TALK_USER_DATA']) if env.get('TALK_PACKAGED') == '1' else root
        values[name] = (path if path.is_absolute() else base / path).resolve()
    if env.get('TALK_PACKAGED') == '1':
        resources = Path(env['TALK_RUNTIME_ROOT']).resolve().parent
        if any(values[name].is_relative_to(resources) for name in ('data_dir', 'qq_session_dir')):
            raise ValueError('CONFIG_DATA_IN_RESOURCES')
    for name, minimum, maximum in (('port', 1024, 65535), ('ttl_seconds', 60, 172800)):
        value = values[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError('CONFIG_NUMBER_INVALID')
    return Settings(**{name: values[name] for name in Settings.__dataclass_fields__})


SETTINGS = load_settings()
