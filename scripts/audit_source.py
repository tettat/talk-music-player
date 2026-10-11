"""Refuse private state, device paths and token-shaped values in the exact git index."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = {'.private', '.venv', '.cache', 'node_modules', 'artifacts', 'output',
                 'experiments', 'research', 'design', '.playwright-cli', '.playwright', 'dist'}
PRIVATE_SUFFIXES = {'.sqlite3', '.sqlite', '.db', '.dpapi', '.mp3', '.mgg', '.wav', '.mid',
                    '.onnx', '.pt', '.pth', '.safetensors', '.lnk', '.zip', '.log'}
PATTERNS = {
    'DEVICE_HOME_PATH': re.compile(r'(?i)[a-z]:[\\/]+Users[\\/]+[^\\/\s"\']+'),
    'GITHUB_TOKEN_SHAPE': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})'),
    'PRIVATE_KEY': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'CREDENTIAL_VALUE': re.compile(r'(?i)["\'](?:access_token|refresh_token|music_key|musickey|qm_keyst)["\']\s*:\s*["\'][^"\']{12,}["\']'),
    'SIGNED_MUSIC_URL': re.compile(r'https?://[^\s"\']+[?&](?:vkey|authst|uin)=', re.I),
}


def check_path(name):
    path = PurePosixPath(name)
    if (path.parts[0] in PRIVATE_ROOTS or path.name == 'config.local.json' or
            path.name.startswith('.env') and path.name != '.env.example' or
            path.suffix.lower() in PRIVATE_SUFFIXES or path.name in ('device.json', 'login-qr.png')):
        return 'PRIVATE_FILE'
    return None


def check_text(text):
    return [name for name, pattern in PATTERNS.items() if pattern.search(text)]


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def main():
    names = git('ls-files', '-z').decode('utf-8').split('\0')
    failures = []
    count = 0
    for name in filter(None, names):
        count += 1
        problem = check_path(name)
        if problem:
            failures.append((name, problem)); continue
        mode = git('ls-files', '-s', '--', name).decode().split()[0]
        if mode == '120000':
            failures.append((name, 'SYMLINK_REJECTED')); continue
        if int(git('cat-file', '-s', ':' + name)) > 2 * 1024**2:
            failures.append((name, 'SOURCE_SIZE_REJECTED')); continue
        data = git('show', ':' + name)
        try: text = data.decode('utf-8-sig')
        except UnicodeError:
            failures.append((name, 'UNREVIEWED_BINARY')); continue
        failures.extend((name, code) for code in check_text(text))
    if not count:
        raise SystemExit('STAGE_SOURCE_BEFORE_AUDIT')
    if failures:
        for name, code in failures:
            print(f'{code}: {name}')  # Never echo matched secret material.
        raise SystemExit(1)
    print(f'SOURCE_INDEX_AUDIT_OK: {count} files; no private state/device home paths/token shapes')


if __name__ == '__main__': main()
