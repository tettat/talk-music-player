"""Offline source-pin verification; never prints credentials or imports client sessions."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from providers.qq.adapter import guard

problem = guard()
if problem:
    raise SystemExit(problem)
print('QQ_SDK_PIN_VERIFIED')
