"""Fixed local CPU inference subprocess, independent from the pinned QQ runtime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from insights import pitch_document
from library import PlayerError
from settings import SETTINGS

ROOT = Path(__file__).resolve().parents[1]
PYTHON = SETTINGS.analysis_python
SCRIPT = ROOT / 'analysis/basic_pitch/probe.py'
LOCK = ROOT / 'analysis/basic_pitch/sources.lock.json'
MODEL = SETTINGS.basic_pitch_model


def ready():
    return all(path.is_file() for path in (PYTHON, SCRIPT, LOCK, MODEL))


def file_sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def copy_authorized(source, workspace, digest):
    """Caller holds its source-cache lock; freeze a verified input before releasing it."""
    destination = Path(workspace) / 'input.mp3'
    source = Path(source)
    if source.suffix != '.mp3' or not 0 < source.stat().st_size <= 24 * 1024**2:
        raise PlayerError('SOURCE_UNAVAILABLE', 422)
    shutil.copyfile(source, destination)
    if file_sha(destination) != digest:
        destination.unlink(missing_ok=True)
        raise PlayerError('SOURCE_UNAVAILABLE', 422)
    return destination


def work_directory(private):
    root = Path(private).resolve() / 'analysis-work'
    # Both automatic TemporaryDirectory cleanups are confined to this owned private subtree.
    if not root.is_relative_to(SETTINGS.data_dir.resolve()):
        raise PlayerError('ANALYSIS_UNAVAILABLE', 503)
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='job-', dir=root)


def analyze(source, workspace, track, expected_sha):
    if not ready():
        raise PlayerError('ANALYSIS_UNAVAILABLE', 503)
    start = float(min(30, max(0, track['duration']-30)))
    seconds = float(min(30, track['duration']-start))
    output = Path(workspace) / 'result.json'
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    system = Path(os.environ.get('SystemRoot', '/'))
    env = {name: os.environ[name] for name in ('SystemRoot', 'WINDIR', 'COMSPEC') if name in os.environ}
    env.update(PATH=str(system / 'System32') if os.name == 'nt' else os.defpath,
               TEMP=str(workspace), TMP=str(workspace),
               TALK_BASIC_PITCH_MODEL=str(MODEL),
               NUMBA_CACHE_DIR=str(SETTINGS.data_dir / 'model-jit-cache'),
               OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2', MKL_NUM_THREADS='2', NUMBA_NUM_THREADS='2',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    try:
        child = subprocess.run([str(PYTHON), '-I', '-B', str(SCRIPT), '--input', str(source),
                                '--output', str(output), '--start', str(start), '--seconds', str(seconds)],
                               cwd=str(ROOT), env=env, shell=False, capture_output=True, timeout=180,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise PlayerError('ANALYSIS_TIMEOUT', 504) from None
    except OSError:
        raise PlayerError('ANALYSIS_UNAVAILABLE', 503) from None
    if child.returncode or not output.is_file() or output.stat().st_size > 1024**2:
        raise PlayerError('ANALYSIS_FAILED', 502)
    try:
        raw = json.loads(output.read_text(encoding='utf-8'),
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if raw['model']['sha256'] != lock['model']['sha256']:
            raise ValueError()
        return pitch_document(raw, expected_sha=expected_sha, start=start, seconds=seconds)
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PlayerError('ANALYSIS_INVALID', 502) from None
