"""Clean, fail-closed Windows staging: official embedded CPython, never a copied venv."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'output/packaging-stage'
CACHE = ROOT / 'output/packaging-cache'
LOCK = ROOT / 'requirements/qq-windows-py312.lock'
MANIFEST = ROOT / 'packaging/files.json'
GENERATED = {'package.json', 'notices/INSTALLER-LICENSE.txt', 'notices/Electron-LICENSE.txt',
             'notices/Electron-LICENSES.chromium.html', 'notices/python-dependencies.json'}
BLOCKED_PARTS = {'.private', '.venv', '.git', '.cache', 'cache', 'node_modules', '__pycache__',
                 'logs', 'dist', 'output', 'experiments', 'research'}
BLOCKED_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.dpapi', '.log', '.mp3', '.wav', '.mgg', '.mid',
                    '.onnx', '.pt', '.pth', '.safetensors', '.lnk', '.pyc'}
TEXT_SUFFIXES = {'.py', '.cjs', '.mjs', '.js', '.json', '.html', '.css', '.svg', '.txt', '.md', '.lock'}
TEXT_PATTERNS = {
    'DEVICE_HOME_PATH': re.compile(rb'(?i)[a-z]:[\\/]+Users[\\/]+[^\\/\s"\']+'),
    'BUILD_VENV_PATH': re.compile(rb'(?i)[a-z]:[\\/]+[^\r\n"\']*?\.venv[\\/]'),
    'PRIVATE_KEY': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\r\n\s]+[A-Za-z0-9+/=]{40,}'),
    'TOKEN_SHAPE': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})'),
    'CREDENTIAL_VALUE': re.compile(rb'(?i)["\'](?:access_token|refresh_token|music_key|musickey|qm_keyst)["\']\s*:\s*["\'][^"\']{12,}["\']'),
    'SIGNED_MUSIC_URL': re.compile(rb'https?://[^\s"\']+[?&](?:vkey|authst|uin)=', re.I),
}


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def clean_owned(path):
    """Only delete the fixed build staging root, never a caller-computed directory."""
    if path != STAGE or path.is_symlink() or path.resolve() != ROOT / 'output/packaging-stage':
        raise ValueError('UNSAFE_STAGING_DELETE')
    if not path.resolve().is_relative_to(ROOT):
        raise ValueError('UNSAFE_STAGING_DELETE')
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def allowed_name(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name
            and not (set(part.lower() for part in path.parts) & BLOCKED_PARTS)
            and path.suffix.lower() not in BLOCKED_SUFFIXES
            and path.name.lower() not in {'config.local.json', 'device.json', 'login-qr.png'}
            and not path.name.startswith('.env'))


def audit_text(name, data):
    # Known literal in the hash-verified SDK's docstring, never an account value.
    data = data.replace(b'"musickey": "placeholder-musickey"', b'"musickey": ""')
    return [f'{code}:{name}' for code, pattern in TEXT_PATTERNS.items() if pattern.search(data)]


def copy_source(app):
    names = json.loads(MANIFEST.read_text(encoding='utf-8'))['files']
    if len(set(names)) != len(names) or not names:
        raise ValueError('INVALID_PACKAGING_MANIFEST')
    for name in names:
        source = ROOT / name
        if not allowed_name(name) or not source.is_file() or not source.resolve().is_relative_to(ROOT):
            raise ValueError('REJECTED_PACKAGING_SOURCE:' + name)
        if any(parent.is_symlink() for parent in (source, *source.parents)):
            raise ValueError('SYMLINK_PACKAGING_SOURCE:' + name)
        if source.stat().st_size > 2 * 1024**2:
            raise ValueError('SOURCE_TOO_LARGE:' + name)
        issues = audit_text(name, source.read_bytes())
        if issues:
            raise ValueError(';'.join(issues))  # Codes only, never echo secret values.
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    shipped_package = {key: package[key] for key in
                       ('name', 'productName', 'version', 'description', 'license', 'private')}
    shipped_package['main'] = 'desktop/packaging.cjs'
    write_json(app / 'package.json', shipped_package)
    notices = app / 'notices'
    notices.mkdir(exist_ok=True)
    (notices / 'INSTALLER-LICENSE.txt').write_text(
        '小黄花 · Usic: 私人测试，公开发布前复核。\n'
        'No claim of commercial redistribution clearance. See packaging/INSTALLER.txt and THIRD_PARTY.txt.\n\n'
        + (ROOT / 'LICENSE').read_text(encoding='utf-8'), encoding='utf-8')
    electron = ROOT / 'node_modules/electron/dist'
    shutil.copyfile(electron / 'LICENSE', notices / 'Electron-LICENSE.txt')
    shutil.copyfile(electron / 'LICENSES.chromium.html', notices / 'Electron-LICENSES.chromium.html')
    return names, package


def verify_source_snapshot(app, names):
    for name in names:
        if digest(ROOT / name) != digest(app / name):
            raise ValueError('SOURCE_CHANGED_DURING_STAGING:' + name)
    # Fail before building if a parallel integration adds a local JS dependency
    # without explicitly adding that resource to the reviewed positive manifest.
    for path in app.rglob('*'):
        if path.suffix not in {'.cjs', '.mjs', '.js'}:
            continue
        content = path.read_text(encoding='utf-8')
        patterns = (r'require\([\'\"](\.[^\'\"]+)[\'\"]\)',
                    r'(?:from\s*|import\s*\()[\'\"](\.[^\'\"]+)[\'\"]')
        for pattern in patterns:
            for relative in re.findall(pattern, content):
                target = path.parent / relative
                if not target.is_file():
                    raise ValueError('MISSING_LOCAL_JS_DEPENDENCY:' + path.relative_to(app).as_posix() + ':' + relative)


def download(url, target, sha256):
    if not target.is_file() or digest(target) != sha256:
        request = urllib.request.Request(url, headers={'User-Agent': 'Usic-private-packaging/0.1'})
        with urllib.request.urlopen(request, timeout=120) as response, target.open('wb') as output:
            shutil.copyfileobj(response, output)
    if digest(target) != sha256:
        target.unlink(missing_ok=True)
        raise ValueError('DOWNLOAD_HASH_MISMATCH')


def safe_extract(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = PurePosixPath(member.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in member.filename
                    or ':' in member.filename or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('ZIP_TRAVERSAL_REJECTED')
        bundle.extractall(destination)


def run(command, **kwargs):
    subprocess.run([str(item) for item in command], check=True, cwd=ROOT,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)


def runtime_dependencies(python):
    wheels = CACHE / 'wheels'
    wheels.mkdir(exist_ok=True)
    # The host venv is a build tool only. Every wheel is fetched/checked against
    # the existing hashlock and installed into a fresh target, with dependency resolution.
    run([sys.executable, '-I', '-m', 'pip', 'download', '--disable-pip-version-check',
         '--require-hashes', '--only-binary=:all:', '--no-deps', '-r', LOCK, '--dest', wheels])
    site = python / 'Lib/site-packages'
    run([sys.executable, '-I', '-m', 'pip', 'install', '--disable-pip-version-check',
         '--no-index', '--find-links', wheels, '--require-hashes', '--only-binary=:all:',
         '--no-compile', '--no-warn-script-location', '--target', site, '-r', LOCK])
    # Generated console launchers may embed the build interpreter. They are unused
    # by server.py and must not leak host paths or create an accidental public pip.
    for name in ('bin', 'Scripts'):
        if (site / name).exists():
            shutil.rmtree(site / name)
    for path in sorted(site.rglob('__pycache__'), reverse=True):
        shutil.rmtree(path)
    for path in site.rglob('*.pyc'):
        path.unlink()
    # This pinned urllib3-future wheel contains a site-time package merge hook.
    # Both namespaces are already vendored in the wheel; do not execute that
    # mutating hook in a read-only installation. All other .pth files fail closed.
    for path in site.rglob('*.pth'):
        if path.relative_to(site).as_posix() != 'urllib3_future.pth':
            raise ValueError('UNREVIEWED_DEPENDENCY_PTH')
        path.unlink()
    (python / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n', encoding='ascii')
    # Keep the existing SDK guard's executable.parent.parent == sys.prefix
    # contract without a venv: DLL + DLL-named _pth stay in python/, the official
    # launcher lives one level below and finds the DLL via our owned PATH.
    (python / 'Scripts').mkdir()
    for name in ('python.exe', 'pythonw.exe'):
        (python / name).rename(python / 'Scripts' / name)
    executable = python / 'Scripts/python.exe'
    runtime_env = dict(os.environ, PATH=os.pathsep.join(
        [str(python), str(Path(os.environ['SystemRoot']) / 'System32'), os.environ['SystemRoot']]))
    probe = (
        "import sys, json, ssl, sqlite3, ctypes, importlib.metadata as m; "
        "import qqmusic_api, cryptography, miniaudio, orjson, pydantic, niquests; "
        "assert sys.version_info[:2] == (3,12); assert sys.flags.isolated; "
        "assert sys.prefix == str(__import__('pathlib').Path(sys.executable).parent.parent); "
        "print(json.dumps({'version':sys.version.split()[0], 'isolated':bool(sys.flags.isolated), "
        "'dependencies':sorted([{'name':d.metadata['Name'],'version':d.version, "
        "'license':d.metadata.get('License-Expression') or d.metadata.get('License'), "
        "'requires':d.requires or []} for d in m.distributions()],key=lambda x:x['name'].lower())}))"
    )
    result = subprocess.check_output([str(executable), '-I', '-B', '-c', probe], cwd=ROOT, env=runtime_env)
    metadata = json.loads(result)
    expected = {re.split(r'[= ]', line)[0].lower().replace('_', '-'): line.split('==')[1].split()[0]
                for line in LOCK.read_text().splitlines() if line and not line.startswith('#')}
    actual = {entry['name'].lower().replace('_', '-'): entry['version'] for entry in metadata['dependencies']}
    if actual != expected:
        raise ValueError('INSTALLED_HASHLOCK_SET_MISMATCH')
    # pip check with the embedded runtime's working set, never the host's packages.
    pip_check = ("import sys, importlib.util; "
                 "s=importlib.util.spec_from_file_location('pip',sys.argv[1]+'/__init__.py',submodule_search_locations=[sys.argv[1]]); "
                 "m=importlib.util.module_from_spec(s); sys.modules['pip']=m; s.loader.exec_module(m); "
                 "from pip._internal.commands import create_command; sys.exit(create_command('check').main([]))")
    pip_root = subprocess.check_output([sys.executable, '-I', '-c',
                                       'import pip, pathlib; print(pathlib.Path(pip.__file__).parent)'], text=True).strip()
    run([executable, '-I', '-B', '-c', pip_check, pip_root], env=runtime_env)
    # Check the SDK's retained source pins too (not just its wheel).
    source_lock = json.loads((ROOT / 'src/providers/qq/sources.lock.json').read_text(encoding='utf-8'))
    for relative, expected_sha in source_lock['pythonSourceSha256'].items():
        content = (site / relative).read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(content).hexdigest() != expected_sha:
            raise ValueError('QQ_SOURCE_PIN_MISMATCH:' + relative)
    metadata['qqSourceFilesVerified'] = len(source_lock['pythonSourceSha256'])
    metadata['vendoringChanges'] = ['Removed unused generated console launchers and bytecode',
                                   'Removed inactive urllib3_future.pth merge hook; site startup disabled']
    metadata['hashlockSha256'] = digest(LOCK)
    metadata['qqSourceLockSha256'] = digest(ROOT / 'src/providers/qq/sources.lock.json')
    metadata['wheelArtifacts'] = [{'filename': path.name, 'sha256': digest(path)}
                                  for path in sorted(wheels.glob('*.whl'))]
    return metadata


def scan_tree(stage=STAGE):
    app = stage / 'app'
    names = set(json.loads(MANIFEST.read_text(encoding='utf-8'))['files']) | GENERATED
    errors, checked = [], 0
    for path in app.rglob('*'):
        if not path.is_file():
            if path.is_symlink():
                errors.append('SYMLINK_REJECTED')
            continue
        name = path.relative_to(app).as_posix()
        if name not in names or not allowed_name(name):
            errors.append('UNLISTED_APP_FILE:' + name)
        errors.extend(audit_text(name, path.read_bytes()))
        checked += 1
    missing = names - {path.relative_to(app).as_posix() for path in app.rglob('*') if path.is_file()}
    errors.extend('MISSING_APP_FILE:' + name for name in sorted(missing))
    runtime = stage / 'runtime'
    for path in runtime.rglob('*'):
        if path.is_symlink():
            errors.append('SYMLINK_RUNTIME_REJECTED')
        if not path.is_file():
            continue
        name = path.relative_to(runtime).as_posix()
        parts = {part.lower() for part in Path(name).parts}
        if (parts & BLOCKED_PARTS or path.suffix.lower() in BLOCKED_SUFFIXES or
                path.name.lower() in {'config.local.json', 'pyvenv.cfg'}):
            errors.append('PRIVATE_RUNTIME_FILE:' + name)
        data = path.read_bytes()
        # Scan source/metadata and binary strings for this build's actual profile.
        if path.suffix.lower() in TEXT_SUFFIXES or path.suffix == '':
            errors.extend(audit_text(name, data))
        home = str(Path.home())
        for representation in (home, home.replace('\\', '/'), home.replace('\\', '\\\\')):
            if representation.encode('utf-8').lower() in data.lower() or representation.encode('utf-16-le').lower() in data.lower():
                errors.append('BUILD_PROFILE_IN_RUNTIME:' + name)
                break
        checked += 1
    if errors:
        raise ValueError('\n'.join(errors))
    return {'passed': True, 'filesScanned': checked, 'appFiles': len(names),
            'checks': ['exact app allowlist', 'private filenames', 'device paths', 'credential shapes',
                       'binary host-profile strings', 'no venv', 'no heavy models or personal media']}


def build():
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise ValueError('WINDOWS_PYTHON_3_12_BUILD_TOOL_REQUIRED')
    if shutil.disk_usage(ROOT).free < 2 * 1024**3:
        raise ValueError('AT_LEAST_2GB_FREE_REQUIRED')
    clean_owned(STAGE)
    CACHE.mkdir(parents=True, exist_ok=True)
    app, runtime = STAGE / 'app', STAGE / 'runtime'
    app.mkdir()
    names, package = copy_source(app)
    pin = json.loads((ROOT / 'packaging/runtime-lock.json').read_text())
    archive = CACHE / pin['url'].rsplit('/', 1)[1]
    download(pin['url'], archive, pin['sha256'])
    python = runtime / 'base/python'
    python.mkdir(parents=True)
    safe_extract(archive, python)
    dependencies = runtime_dependencies(python)
    write_json(app / 'notices/python-dependencies.json', dependencies)
    files = [{'path': path.relative_to(runtime).as_posix(), 'size': path.stat().st_size, 'sha256': digest(path)}
             for path in sorted(runtime.rglob('*')) if path.is_file()]
    write_json(runtime / 'runtime.json', {
        'schemaVersion': 1, 'platform': 'win32', 'arch': 'x64', 'python': 'base/python/Scripts/python.exe',
        'application': '../app', 'launch': ['-I', '-B', 'src/server.py', '--desktop', '--port', '0'],
        'pythonPin': pin, 'dependencyLock': '../app/requirements/qq-windows-py312.lock',
        'dependencyLockSha256': digest(LOCK), 'files': files,
        'optionalCapabilities': {'scoreConfigEnv': ['USiC_SCORE_LAB_CONFIG', 'USIC_SCORE_LAB_CONFIG'],
                                 'bundledModels': False, 'bundledTorch': False, 'bundledSoundfonts': False},
    })
    verify_source_snapshot(app, names)
    scan = scan_tree()
    record = {'stage': 'output/packaging-stage', 'cleanStaging': True,
              'version': package['version'], 'sourceFiles': len(names), 'scan': scan,
              'runtimeBytes': sum(item['size'] for item in files), 'runtimeFiles': len(files),
              'pythonVersion': dependencies['version'], 'hashlockSha256': digest(LOCK),
              'dependenciesInstalledAndVersionChecked': len(dependencies['dependencies']),
              'pipCheck': True, 'qqSourceFilesVerified': dependencies['qqSourceFilesVerified'],
              'sourceSnapshot': [{'path': name, 'sha256': digest(app / name)} for name in names]}
    write_json(ROOT / 'output/outsource/packaging-staging.json', record)
    print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-only', action='store_true')
    args = parser.parse_args()
    if args.audit_only:
        print(json.dumps(scan_tree()))
    else:
        build()
