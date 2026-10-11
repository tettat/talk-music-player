"""Reproduce the pinned Windows Python runtime in this checkout, never globally."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.venv'
PYTHON = RUNTIME / 'Scripts/python.exe'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise SystemExit('WINDOWS_PYTHON_3_12_REQUIRED')
    if not args.check_only:
        if not PYTHON.is_file():
            if RUNTIME.exists():
                raise SystemExit('EMPTY_OWNED_RUNTIME_REQUIRED')
            subprocess.run([sys.executable, '-I', '-m', 'venv', str(RUNTIME)], check=True)
        subprocess.run([str(PYTHON), '-I', '-m', 'pip', 'install', '--require-hashes',
                        '--only-binary=:all:', '-r', str(ROOT / 'requirements/qq-windows-py312.lock')],
                       check=True, cwd=ROOT)
    if not PYTHON.is_file():
        raise SystemExit('LOCAL_RUNTIME_REQUIRED')
    subprocess.run([str(PYTHON), '-I', '-m', 'pip', 'check'], check=True)
    # Check the clean-checkout defaults rather than a user's optional external runtime.
    import os
    env = dict(os.environ, TALK_PYTHON=str(PYTHON))
    subprocess.run([str(PYTHON), '-I', '-B', str(ROOT / 'scripts/verify_runtime.py')],
                   check=True, cwd=ROOT, env=env)


if __name__ == '__main__':
    main()
