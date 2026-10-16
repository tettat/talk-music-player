"""Owned entrypoint for isolated/embeddable Python; never rely on cwd/PYTHONPATH."""
from pathlib import Path
import runpy
import sys

if len(sys.argv) < 2 or sys.argv[1] not in ('worker', 'editing'):
    raise SystemExit(2)
module = sys.argv.pop(1)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
runpy.run_module('capabilities.score_lab.' + module, run_name='__main__')
