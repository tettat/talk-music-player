"""Explicit setup only; playback/inference never downloads dependencies or models."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from settings import SETTINGS
BUNDLE = ROOT / 'analysis/basic_pitch'
PRIVATE = SETTINGS.data_dir
RUNTIME = PRIVATE / 'runtime-analysis'
PYTHON = SETTINGS.analysis_python
MODEL = SETTINGS.basic_pitch_model
DOWNLOAD = PRIVATE / 'analysis-setup'
WHEEL = DOWNLOAD / 'basic_pitch-0.4.0-py2.py3-none-any.whl'
BASE = Path(sys.executable)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def command(*args):
    subprocess.run([str(value) for value in args], check=True, cwd=ROOT)


def install(lock):
    # Never install into the existing QQ adapter, another runtime, or a global Python.
    if PYTHON.resolve() != (RUNTIME / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')).resolve():
        raise ValueError('SETUP_REQUIRES_DEFAULT_OWNED_ANALYSIS_RUNTIME')
    for path in (RUNTIME, MODEL, DOWNLOAD):
        if not path.resolve().is_relative_to(PRIVATE.resolve()):
            raise ValueError('OWNED_SETUP_PATH_REQUIRED')
    if not PYTHON.is_file():
        if RUNTIME.exists() or not BASE.is_file():
            raise ValueError('PINNED_BASE_OR_EMPTY_RUNTIME_REQUIRED')
        command(BASE, '-I', '-m', 'venv', RUNTIME)
    command(PYTHON, '-I', '-m', 'pip', 'install', '-r', BUNDLE / 'requirements.lock.txt')
    DOWNLOAD.mkdir(parents=True, exist_ok=True)
    if not WHEEL.is_file():
        command(PYTHON, '-I', '-m', 'pip', 'download', '--no-deps', '--only-binary=:all:',
                '--dest', DOWNLOAD, 'basic-pitch==0.4.0')
    if sha(WHEEL.read_bytes()) != lock['wheelSha256']:
        raise ValueError('MODEL_WHEEL_MISMATCH')
    # Whitelist entries, not extractall; no execution of the wheel or its TF dependency.
    with zipfile.ZipFile(WHEEL) as wheel:
        for name, expected in lock['sources'].items():
            content = wheel.read('basic_pitch/' + name)
            if sha(content) != expected:
                raise ValueError('MODEL_SOURCE_MISMATCH')
            destination = BUNDLE / 'vendor/basic_pitch' / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and sha(destination.read_bytes()) != expected:
                raise ValueError('EXISTING_VENDOR_MISMATCH')
            destination.write_bytes(content)
        content = wheel.read('basic_pitch/saved_models/icassp_2022/nmp.onnx')
        if sha(content) != lock['model']['sha256']:
            raise ValueError('MODEL_HASH_MISMATCH')
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        if MODEL.exists() and sha(MODEL.read_bytes()) != lock['model']['sha256']:
            raise ValueError('EXISTING_MODEL_MISMATCH')
        MODEL.write_bytes(content)


def verify(lock):
    if not PYTHON.is_file() or sha(MODEL.read_bytes()) != lock['model']['sha256']:
        raise ValueError('MODEL_RUNTIME_REQUIRED')
    for name, expected in lock['sources'].items():
        if sha((BUNDLE / 'vendor/basic_pitch' / name).read_bytes()) != expected:
            raise ValueError('MODEL_SOURCE_MISMATCH')
    command(PYTHON, '-I', '-m', 'pip', 'check')
    command(PYTHON, '-I', '-c',
            "import sys,onnxruntime as o,numpy as n,librosa,soundfile; "
            "assert sys.version_info[:2]==(3,12); assert o.__version__=='1.20.1'; "
            "assert n.__version__=='1.26.4'; print('ONNX_CPU_RUNTIME_VERIFIED')")
    print('BASIC_PITCH_SOURCES_AND_MODEL_VERIFIED')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true', help='No downloads or installs')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('PYTHON_3_12_REQUIRED')
    lock = json.loads((BUNDLE / 'sources.lock.json').read_text(encoding='utf-8'))
    if not args.check_only:
        install(lock)
    verify(lock)
