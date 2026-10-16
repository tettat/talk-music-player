"""Build the private, offline Windows ScoreLab directory pack (stdlib only).

Never relocate a venv, import its site hooks, or put the application in _pth.
The only third-party inputs are installed RECORD-owned files. Each retained
file is checked against its installation hash before it enters the payload.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "output/score-pack-build"
NAME = "Usic-ScoreLab-0.1.0-windows-x64"
DEST = ROOT / "dist" / NAME
RESULT = ROOT / "output/outsource/usic-score-pack-result.json"
PYTHON_SHA = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
PATHS = {"python": "python/python.exe", "sheetRepo": "models/sheetsage2",
         "baseRepo": "models/mert_base", "assetsDir": "render-assets"}
IMPORTS = ("torch", "soundfile", "pretty_midi", "music21", "transformers",
           "safetensors", "torchaudio")
PTH = "python312.zip\n.\nLib/site-packages\nimport site\n"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                "tests", "test", "test-results", "htmlcov"}
LICENSES = {
    "pretty-midi": {
        "version": "0.2.10", "file": "pretty_midi-0.2.10-LICENSE.txt",
        "url": "https://raw.githubusercontent.com/craffel/pretty-midi/0.2.10/LICENSE.txt",
        "sha256": "220f461a41157a81832520a84d95bfa5c4616f553dbf3ef8ac1fb9562244c069"},
    "tokenizers": {
        "version": "0.20.3", "file": "tokenizers-0.20.3-LICENSE.txt",
        "url": "https://raw.githubusercontent.com/huggingface/tokenizers/v0.20.3/LICENSE",
        "sha256": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"},
}
# No sys.path additions. site only enables Python's normal sysconfig defaults;
# every third-party .pth is discarded, and -I disables user-site/env injection.
# Keep DLL-directory handles alive for the process, never search userbase/bin.
BOOTSTRAP = '''"""Owned embedded-runtime policy; see PACK.txt. No path injection."""
import os
from pathlib import Path
import site
import sys
import sysconfig

sys.dont_write_bytecode = True
site.ENABLE_USER_SITE = False
_root = Path(__file__).resolve().parent
sysconfig.get_config_vars()['userbase'] = str(_root)
_dll_handles = []
if os.name == 'nt':
    for _relative in ('.', 'Lib/site-packages/torch/lib',
                      'Lib/site-packages/torchaudio/lib',
                      'Lib/site-packages/numpy.libs',
                      'Lib/site-packages/scipy.libs',
                      'Lib/site-packages/_soundfile_data'):
        _directory = (_root / _relative).resolve()
        if not _directory.is_relative_to(_root):
            raise RuntimeError('SCORE_RUNTIME_DLL_PATH_INVALID')
        if _directory.is_dir():
            _dll_handles.append(os.add_dll_directory(str(_directory)))
'''


class PackError(RuntimeError):
    """Errors intentionally use payload-relative names, not private paths."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def relative_name(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or value.startswith("/") or any(p in ("", ".", "..") or ":" in p
                                            for p in value.split("/"))):
        raise PackError("Invalid payload-relative path")
    return value


def contained(root, name):
    root = Path(root).resolve()
    path = root / relative_name(name)
    if not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise PackError("Payload path escapes root")
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise PackError("Reparse points are not accepted in pack inputs")
        current = current.parent
    return path


def notice(name):
    return any(word in PurePosixPath(name).name.lower()
               for word in ("license", "licence", "copying", "notice", "copyright", "authors"))


def excluded(name):
    """Licenses win over test-tree trimming, never over unsafe file types."""
    p = PurePosixPath(name)
    lower = p.name.lower()
    if lower in ("direct_url.json", "record"):
        return "installation-metadata"
    if lower.endswith((".pyc", ".pyo", ".pth", ".egg-link")) or lower.startswith("__editable__"):
        return "bytecode-or-site-hook"
    # music21.__init__ imports test.testRunner, whose package imports test_*.
    # This upstream namespace is a runtime import dependency, not test output.
    runtime_test_namespace = name.startswith("music21/test/")
    if not notice(name) and not runtime_test_namespace and any(part.lower() in EXCLUDE_DIRS for part in p.parts):
        return "tests-or-cache"
    if lower.endswith((".log", ".coverage", ".db", ".sqlite", ".sqlite3")):
        return "test-output"
    return None


class PrivacyScanner:
    """Scan all bytes, including DLLs, ZIP members and UTF-16 strings."""
    def __init__(self, paths):
        variants = set()
        for path in paths:
            raw = str(Path(path).resolve())
            for form in (raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\")):
                variants.add(form.lower())
        self.needles = tuple({x.encode(encoding) for x in variants
                              for encoding in ("utf-8", "utf-16-le", "utf-16-be")})
        self.overlap = max((len(x) for x in self.needles), default=1) - 1

    def check_stream(self, stream, name):
        tail = b""
        while chunk := stream.read(4 * 1024 * 1024):
            block = (tail + chunk).lower()
            if any(needle in block for needle in self.needles):
                raise PackError("Private device path detected in " + name)
            tail = block[-self.overlap:] if self.overlap else b""

    def check_file(self, path, name):
        with path.open("rb") as stream:
            self.check_stream(stream, name)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for item in archive.infolist():
                    if not item.is_dir():
                        with archive.open(item) as stream:
                            self.check_stream(stream, name + "!" + item.filename)


def copy_checked(source, target, expected):
    """Copy rather than link, hash while streaming; do not mutate the source."""
    target.parent.mkdir(parents=True, exist_ok=True)
    algorithm, wanted = (expected.split("=", 1) if "=" in expected else ("sha256", expected))
    if algorithm != "sha256":
        raise PackError("Only SHA-256 inputs are accepted")
    h = hashlib.sha256()
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while block := incoming.read(4 * 1024 * 1024):
            h.update(block)
            outgoing.write(block)
    actual = h.hexdigest() if "=" not in expected else base64.urlsafe_b64encode(h.digest()).decode().rstrip("=")
    if actual != wanted:
        target.unlink()
        raise PackError("Input hash mismatch: " + target.name)
    return h.hexdigest()


def dependencies(site):
    """Select only wheel/installed RECORD-owned files; no site-directory sweep."""
    plans, packages, seen = [], [], set()
    trimmed = Counter()
    installed = sorted(metadata.distributions(path=[str(site)]),
                       key=lambda d: canonical(d.metadata["Name"]))
    for dist in installed:
        name = dist.metadata["Name"]
        normalized = canonical(name)
        if normalized in seen:
            raise PackError("Duplicate installed distribution: " + normalized)
        seen.add(normalized)
        if normalized == "pip":
            trimmed["pip-distribution"] += len(dist.files or ())
            continue
        meta = Path(dist._path).name
        relative_name(meta)
        record = contained(site, meta + "/RECORD")
        if not record.is_file():
            raise PackError("Missing installation RECORD: " + normalized)
        owned, licenses = [], []
        for row in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if len(row) != 3:
                raise PackError("Malformed RECORD: " + normalized)
            rel, expected, size = row
            # Installed console launchers embed the original interpreter path.
            # They are outside site-packages and are never needed by this pack.
            if rel.startswith("../"):
                source = (site / rel).resolve()
                if not source.is_relative_to(site.parent.parent.resolve()) or source.is_relative_to(site):
                    raise PackError("Unexpected external RECORD entry: " + normalized)
                trimmed["console-launcher-or-external-doc"] += 1
                continue
            relative_name(rel)
            reason = excluded(rel)
            if reason:
                trimmed[reason] += 1
                continue
            source = contained(site, rel)
            if not source.is_file() or not expected.startswith("sha256="):
                raise PackError("Missing/unhashed installed file: " + rel)
            if size and source.stat().st_size != int(size):
                raise PackError("Installed size mismatch: " + rel)
            owned.append(rel)
            if notice(rel):
                licenses.append("python/Lib/site-packages/" + rel)
            plans.append((source, "python/Lib/site-packages/" + rel, expected))
        supplemental = LICENSES.get(normalized)
        if supplemental:
            if dist.version != supplemental["version"]:
                raise PackError("Supplemental license version mismatch: " + normalized)
            licenses.append("licenses/" + supplemental["file"])
        if not licenses:
            raise PackError("No preserved license for installed distribution: " + normalized)
        packages.append({"name": name, "version": dist.version, "distInfo": meta,
                         "sourceRecordSha256": digest(record),
                         "licenseExpression": dist.metadata.get("License-Expression",
                                                                 dist.metadata.get("License", "")),
                         "licenses": sorted(licenses), "files": sorted(owned),
                         "requiresDist": dist.metadata.get_all("Requires-Dist", [])})
    return plans, packages, dict(trimmed)


def runtime_archive(path):
    if digest(path) != PYTHON_SHA:
        raise PackError("Official embedded Python ZIP SHA-256 mismatch")
    # Also respect a packaging-team lock if present; never change their files.
    for name in ("python.lock.json", "runtime-lock.json"):
        lock_file = ROOT / "packaging" / name
        if lock_file.is_file():
            lock = read_json(lock_file)
            if lock.get("sha256") != PYTHON_SHA or lock.get("pythonVersion", "3.12.10") != "3.12.10":
                raise PackError("Packaging Python lock differs; review before building")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise PackError("Duplicate embedded ZIP entries")
        for item in names:
            relative_name(item)
            if "/" in item or item.lower() not in ("python.exe", "pythonw.exe", "python312.zip",
                                                    "python312._pth", "python.cat", "license.txt") and not item.lower().endswith((".pyd", ".dll")):
                raise PackError("Unexpected embedded runtime entry")
        required = {"python.exe", "python312.dll", "python3.dll", "python312.zip", "LICENSE.txt"}
        if not required.issubset(names):
            raise PackError("Incomplete embedded runtime")
        return names


def get_licenses(fetch=False):
    plans = []
    cache = BUILD / "license-cache"
    cache.mkdir(parents=True, exist_ok=True)
    for spec in LICENSES.values():
        path = cache / spec["file"]
        if not path.is_file():
            if not fetch:
                raise PackError("Missing supplemental license; explicitly use --fetch-licenses")
            with urllib.request.urlopen(spec["url"], timeout=60) as response:
                data = response.read(128 * 1024 + 1)
            if len(data) > 128 * 1024 or hashlib.sha256(data).hexdigest() != spec["sha256"]:
                raise PackError("Supplemental license download SHA-256 mismatch")
            path.write_bytes(data)
        plans.append((path, "licenses/" + spec["file"], spec["sha256"]))
    return plans


def pinned_files(config):
    plans = []
    for key, inventory in read_json(ROOT / "capabilities/score_lab/model.lock.json")["repositories"].items():
        for rel, expected in inventory.items():
            plans.append((contained(config[key], rel), PATHS[key] + "/" + rel, expected))
        # Only exact pinned content plus top-level upstream notices/readmes.
        for source in sorted(config[key].iterdir()):
            if source.is_file() and source.name not in inventory and (notice(source.name) or source.name.lower().startswith("readme")):
                source = contained(config[key], source.name)
                plans.append((source, PATHS[key] + "/" + source.name, digest(source)))
    for rel, expected in read_json(ROOT / "capabilities/score_lab/render-assets.lock.json")["files"].items():
        plans.append((contained(config["assetsDir"], rel), PATHS["assetsDir"] + "/" + rel, expected))
    # The lightweight host uses this pinned inventory as its readiness marker.
    # It is metadata, not an extra unpinned rendering asset.
    source = ROOT / "capabilities/score_lab/render-assets.lock.json"
    plans.append((source, PATHS["assetsDir"] + "/manifest.json", digest(source)))
    for rel in ("model.lock.json", "render-assets.lock.json"):
        source = ROOT / "capabilities/score_lab" / rel
        plans.append((source, rel, digest(source)))
    for rel in ("abcjs-MIT.txt", "piano-attribution.txt"):
        source = ROOT / "capabilities/score_lab/notices" / rel
        plans.append((source, "licenses/" + rel, digest(source)))
    source = ROOT / "LICENSE"
    plans.append((source, "licenses/Usic-GPL-3.0.txt", digest(source)))
    return plans


def regenerate_records(pack, packages):
    """No dangling ../../../Scripts, removed .pth or source direct_url entries."""
    site = pack / "python/Lib/site-packages"
    for item in packages:
        rows = []
        for rel in item["files"]:
            path = contained(site, rel)
            raw = bytes.fromhex(digest(path))
            rows.append((rel, "sha256=" + base64.urlsafe_b64encode(raw).decode().rstrip("="), path.stat().st_size))
        record_rel = item["distInfo"] + "/RECORD"
        rows.append((record_rel, "", ""))
        with contained(site, record_rel).open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream, lineterminator="\n").writerows(rows)
        item["files"].append(record_rel)
        item["files"].sort()


def inventory(pack, scanner=None):
    files, total = {}, 0
    for directory, dirs, names in os.walk(pack, followlinks=False):
        for subdir in dirs:
            contained(pack, (Path(directory) / subdir).relative_to(pack).as_posix())
        for name in sorted(names):
            path = Path(directory) / name
            rel = path.relative_to(pack).as_posix()
            contained(pack, rel)
            if scanner:
                scanner.check_file(path, rel)
            total += path.stat().st_size
            if rel != "score-pack.json":
                files[rel] = digest(path)
    return dict(sorted(files.items())), total


def verify_pack(pack, scanner=None):
    manifest_file = pack / "score-pack.json"
    if manifest_file.stat().st_size > 8 * 1024 * 1024:
        raise PackError("Manifest exceeds native size limit")
    manifest = read_json(manifest_file)
    files, total = inventory(pack, scanner)
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict) or not 0 < len(files) <= 60000:
        raise PackError("Manifest schema/count invalid")
    if manifest["files"] != files:
        raise PackError("Payload has extra, missing or modified files")
    for key, rel in PATHS.items():
        if manifest.get(key) != rel:
            raise PackError("Manifest path contract mismatch")
        value = contained(pack, rel)
        if key == "python" and (not value.is_file() or rel not in files):
            raise PackError("Manifest Python missing")
        if key != "python" and not value.is_dir():
            raise PackError("Manifest resource directory missing")
    for key, pins in read_json(ROOT / "capabilities/score_lab/model.lock.json")["repositories"].items():
        for rel, expected in pins.items():
            if files.get(PATHS[key] + "/" + rel) != expected:
                raise PackError("Model pin mismatch: " + rel)
    for rel, expected in read_json(ROOT / "capabilities/score_lab/render-assets.lock.json")["files"].items():
        if files.get(PATHS["assetsDir"] + "/" + rel) != expected:
            raise PackError("Asset pin mismatch: " + rel)
    if files.get(PATHS['assetsDir'] + '/manifest.json') != digest(ROOT / 'capabilities/score_lab/render-assets.lock.json'):
        raise PackError('Host rendering readiness manifest missing or corrupt')
    if (pack / "python/python312._pth").read_text(encoding="utf-8") != PTH:
        raise PackError("Unexpected embedded Python search path")
    for rel in files:
        p = PurePosixPath(rel)
        if p.suffix.lower() in (".pyc", ".pyo", ".pth", ".egg-link") or p.name.lower() in ("pyvenv.cfg", "direct_url.json") or "__pycache__" in p.parts:
            raise PackError("Forbidden runtime state: " + rel)
        if "pip" in p.parts or any(part.lower().startswith("pip-") and part.endswith(".dist-info") for part in p.parts):
            raise PackError("pip must not be shipped")
    return {"passed": True, "filesCount": len(files) + 1, "hashedFilesCount": len(files),
            "sizeBytes": total, "manifestSha256": digest(manifest_file),
            "manifestBytes": manifest_file.stat().st_size}


def clean_scratch(path):
    # Windows deletion guard: absolute resolved target must remain in our output.
    root = BUILD.resolve()
    target = path.resolve()
    if target == root or not target.is_relative_to(root) or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise PackError("Refusing unsafe scratch cleanup")
    shutil.rmtree(target)


def qa_environment(scratch):
    system = os.environ.get("SystemRoot", r"C:\Windows")
    home = scratch / "empty-home"
    home.mkdir(exist_ok=True)
    temp = scratch / "temp"
    temp.mkdir(exist_ok=True)
    # Start from an allowlist; no auth, model cache, PATH or Python inheritance.
    return {"SystemRoot": system, "WINDIR": system, "PATH": system + r"\System32",
            "COMSPEC": system + r"\System32\cmd.exe", "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "TEMP": str(temp), "TMP": str(temp), "USERPROFILE": str(home), "HOME": str(home),
            "APPDATA": str(home), "LOCALAPPDATA": str(home), "XDG_CACHE_HOME": str(home),
            "MPLCONFIGDIR": str(home / "matplotlib"), "HF_HOME": str(home / "huggingface"),
            "TORCH_HOME": str(home / "torch"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "OMP_NUM_THREADS": "2",
            "PYTHONHOME": str(scratch / "forbidden-host-python"), "PYTHONPATH": str(scratch),
            "PYTHONIOENCODING": "utf-8"}


def run(command, scratch, env, timeout=300):
    result = subprocess.run(command, cwd=scratch, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # Keep no raw tracebacks/logs: they could disclose app/config paths.
        message = result.stderr.decode("utf-8", errors="replace")
        message = message.replace(str(ROOT), "<app>").replace(str(BUILD), "<build>")
        raise PackError("QA subprocess failed (exit %d): %s" % (result.returncode, message[-3500:]))
    return result.stdout.decode("utf-8", errors="strict")


def import_check(pack, scratch, env):
    code = '''import importlib, json, pathlib, sys, sysconfig
root=pathlib.Path(sys.executable).resolve().parent
assert sys.flags.isolated and sys.flags.ignore_environment and sys.dont_write_bytecode
assert sys.prefix == sys.base_prefix
assert sysconfig.get_config_var('userbase') == str(root)
assert all(pathlib.Path(p).resolve().is_relative_to(root) for p in sys.path)
modules={}
for name in %r:
    module=importlib.import_module(name)
    origin=pathlib.Path(module.__file__).resolve()
    assert origin.is_relative_to(root)
    modules[name]={'version':getattr(module,'__version__',None),'origin':origin.relative_to(root).as_posix()}
import torch, torchaudio, numpy as np
assert torch.version.cuda is None
assert torch.tensor([1.,2.]).sum().item() == 3.
assert torchaudio.functional.resample(torch.zeros(1,160),16000,8000).shape[-1] == 80
print(json.dumps({'passed':True,'python':sys.version.split()[0],'architecture':__import__('platform').machine(),
                  'isolated':True,'noHostPython':True,'cpuOnly':True,'modules':modules}))
''' % (IMPORTS,)
    return json.loads(run([str(pack / PATHS["python"]), "-I", "-B", "-c", code], scratch, env))


def app_fixture(scratch):
    app = scratch / "调用方 应用"
    target = app / "capabilities/score_lab"
    target.mkdir(parents=True)
    (app / "capabilities/__init__.py").write_text("", encoding="utf-8")
    for name in ("__init__.py", "entry.py", "worker.py", "common.py", "lifetime.py",
                 "accompaniment.py", "config.example.json", "model.lock.json", "render-assets.lock.json"):
        shutil.copyfile(ROOT / "capabilities/score_lab" / name, target / name)
    editing = ROOT / "capabilities/score_lab/editing.py"
    if editing.is_file():
        shutil.copyfile(editing, target / "editing.py")
    return app


def worker_checks(pack, app, scratch, env, smoke):
    exe = str(pack / PATHS["python"])
    entry = str(app / "capabilities/score_lab/entry.py")
    config = {key: str(pack / rel) for key, rel in PATHS.items()}
    config.update(dataDir=str(scratch / "new-data"), device="cpu", threads=2, port=0, jobTimeoutSeconds=900)
    config_file = scratch / "temporary-config.json"
    write_json(config_file, config)
    code = '''import json, pathlib, sys
sys.path.insert(0,sys.argv[1])
from capabilities.score_lab import worker
config={k:pathlib.Path(v) for k,v in json.loads(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8')).items()
        if k in ('sheetRepo','baseRepo','assetsDir')}
worker.verify_model(config)
assets=worker.verify_assets(config['assetsDir'])
print(json.dumps({'passed':True,'verify_model':True,'verify_assets':True,'assetPins':len(assets)}))
'''
    pins = json.loads(run([exe, "-I", "-B", "-c", code, str(app), str(config_file)], scratch, env))
    help_text = run([exe, "-I", "-B", entry, "worker", "--help"], scratch, env)
    if "--notation-only" not in help_text:
        raise PackError("App-owned worker entry interface missing")
    pins["appOwnedEntry"] = True
    pins["editingEntryHelp"] = "not-present-in-caller"
    if (app / "capabilities/score_lab/editing.py").is_file():
        run([exe, "-I", "-B", entry, "editing", "--help"], scratch, env)
        pins["editingEntryHelp"] = "passed"
    if not smoke:
        return pins, {"status": "not-run", "reason": "--smoke not requested"}
    generate = '''import array, math, pathlib, sys, wave
rate=16000
pcm=array.array('h')
frequencies=(261.6256,329.6276,391.9954,523.2511)
for i in range(rate*30):
    t=i/rate
    beat=t %% .5
    envelope=min(beat/.015,1)*math.exp(-beat*5)
    tone=math.sin(2*math.pi*frequencies[int(t/.5)%%4]*t)
    bass=math.sin(2*math.pi*130.8128*t)*.25
    pcm.append(round(9000*envelope*(tone+bass)))
with wave.open(sys.argv[1],'wb') as stream:
    stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(rate);stream.writeframes(pcm.tobytes())
'''.replace("%%", "%")
    audio = scratch / "synthetic-30-seconds.wav"
    run([exe, "-I", "-B", "-c", generate, str(audio)], scratch, env)
    started = time.monotonic()
    run([exe, "-I", "-B", entry, "worker", "--config", str(config_file), "--sample", "pack-synthetic-30s",
         "--audio", str(audio), "--seconds", "30", "--title", "Synthetic portability test"], scratch, env, timeout=900)
    directory = scratch / "new-data/samples/pack-synthetic-30s"
    result = read_json(directory / "result.json")
    state = read_json(directory / "status.json")
    if state.get("state") != "completed" or not result["provenance"]["modelExecuted"] or result["provenance"]["reused"]:
        raise PackError("Synthetic inference did not genuinely complete")
    return pins, {"status": "passed", "input": "new synthetic tones; no personal music or existing samples",
                  "durationSeconds": 30, "wallSeconds": round(time.monotonic() - started, 2),
                  "inferenceSeconds": result["provenance"]["inferenceSeconds"],
                  "modelExecuted": True, "reused": False, "counts": result["counts"],
                  "exports": result["exports"], "warnings": result["provenance"].get("warnings", []),
                  "accuracyEvidence": False, "temporaryDataRemoved": True}


def native_check(pack, scratch, env):
    node = shutil.which("node")
    if not node:
        raise PackError("Node is required for actual desktop native verification")
    code = "const {verifyBundle}=require(process.argv[1]); verifyBundle(process.argv[2],process.argv[3]).then(v=>console.log(JSON.stringify({passed:true,schema:v.schema_version}))).catch(()=>{console.error('NATIVE_BUNDLE_VERIFY_FAILED');process.exit(1)});"
    return json.loads(run([node, "-e", code, str(ROOT / "desktop/score-bundle.cjs"), str(pack), str(ROOT)], scratch, env, timeout=600))


def runtime_checks(pack, smoke=False, native=True):
    scratch = Path(tempfile.mkdtemp(prefix="外部 工作区-", dir=BUILD))
    try:
        # A hostile cwd/PYTHONPATH must not win over pack-owned imports.
        for name in ("torch.py", "sitecustomize.py"):
            (scratch / name).write_text("raise RuntimeError('CWD/PYTHONPATH injection')\n", encoding="utf-8")
        env = qa_environment(scratch)
        imports = import_check(pack, scratch, env)
        app = app_fixture(scratch)
        pins, inference = worker_checks(pack, app, scratch, env, smoke)
        native_result = native_check(pack, scratch, env) if native else {"status": "not-run"}
        return {"imports": imports, "workerPins": pins, "native": native_result, "inference": inference,
                "relocation": {"passed": True, "unicodeAndSpaces": True,
                               "hostileCwdAndPythonEnvIgnored": True, "appCopiedOutsideRepository": True}}
    finally:
        clean_scratch(scratch)


def scanner_for(config):
    return PrivacyScanner([ROOT, Path.home(), config["python"].parent.parent,
                           config["sheetRepo"], config["baseRepo"], config["assetsDir"]])


def check_output_locations():
    # Verify both sides of all later directory renames before any output writes.
    # Resolve/reparse-check ancestors too, rather than trusting a path prefix.
    for relative in ("output/score-pack-build", "dist/" + NAME,
                     "output/outsource/usic-score-pack-result.json"):
        contained(ROOT, relative)


def run_unit_tests():
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tests/test_score_pack.py")],
                            cwd=BUILD, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    output = result.stderr.decode("utf-8", errors="replace")
    count = re.search(r"Ran (\d+) tests?", output)
    skipped = re.search(r"skipped=(\d+)", output)
    if result.returncode or not count:
        raise PackError("Score pack unit regressions failed")
    return {"passed": True, "testsRun": int(count[1]), "skipped": int(skipped[1]) if skipped else 0,
            "skipReason": "Windows symlink creation requires unavailable privilege" if skipped else None}


def build(args):
    if sys.version_info[:2] != (3, 12) or os.name != "nt" or struct.calcsize("P") != 8:
        raise PackError("Build with Windows x64 Python 3.12; never the host's python alias")
    config = read_json(args.config)
    config = {key: (ROOT / config[key]).resolve() for key in PATHS}
    site = config["python"].parent.parent / "Lib/site-packages"
    if not site.is_dir():
        raise PackError("Configured model environment site-packages missing")
    check_output_locations()
    BUILD.mkdir(parents=True, exist_ok=True)
    unit_tests = run_unit_tests()
    if args.verify_only:
        pack = DEST.resolve()
        report = verify_pack(pack, scanner_for(config))
        checks = runtime_checks(pack, smoke=args.smoke)
        report.update(checks)
        report["unitTests"] = unit_tests
        write_json(BUILD / "verification.json", report)
        return report
    work = BUILD / "work" / NAME
    relocated = BUILD / "谱室 portable 重定位" / "能力 包"
    previous = None
    if args.resume_validation:
        if DEST.exists() or work.exists() == relocated.exists():
            raise PackError("Resume requires exactly one complete owned staging directory, and no final output")
        if relocated.exists():
            work = relocated
        verify_pack(work)
        previous = read_json(work / "score-pack.json")["files"]
    elif DEST.exists() or work.exists() or relocated.exists():
        raise PackError("Owned pack output already exists; use --verify-only, --resume-validation, or remove only the exact owned payload")
    names = runtime_archive(args.runtime_zip)
    plans, packages, trimmed = dependencies(site)
    plans += pinned_files(config) + get_licenses(args.fetch_licenses)
    source_pack_doc = ROOT / "capabilities/score_lab/PACK.txt"
    plans.append((source_pack_doc, "PACK.txt", digest(source_pack_doc)))
    required = sum(source.stat().st_size for source, _, _ in plans) + 64 * 1024 * 1024
    free = shutil.disk_usage(BUILD).free
    if free < required + 1024 * 1024 * 1024:
        raise PackError("Insufficient disk space: one payload plus 1 GiB safety reserve required")
    print(json.dumps({"phase": "copy", "plannedFiles": len(plans), "sourceBytes": required,
                      "freeBytesBefore": free}, ensure_ascii=True), flush=True)
    work.mkdir(parents=True, exist_ok=args.resume_validation)
    py = work / "python"
    py.mkdir(exist_ok=args.resume_validation)
    if not args.resume_validation:
        with zipfile.ZipFile(args.runtime_zip) as archive:
            for name in names:
                if name not in ("python312._pth", "python.cat"):
                    with archive.open(name) as incoming, (py / name).open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
    (py / "python312._pth").write_text(PTH, encoding="utf-8", newline="\n")
    (py / "sitecustomize.py").write_text(BOOTSTRAP, encoding="utf-8", newline="\n")
    copied, added = set(), 0
    for index, (source, rel, expected) in enumerate(plans):
        if rel in copied:
            raise PackError("Overlapping distribution ownership: " + rel)
        copied.add(rel)
        target = contained(work, rel)
        if previous is not None and target.is_file():
            if rel == "PACK.txt":
                target.unlink()  # Owned documentation, after full previous-manifest verification.
            else:
                expected_hex = expected
                if expected.startswith("sha256="):
                    expected_hex = base64.urlsafe_b64decode(expected.split("=", 1)[1] + "=").hex()
                if previous.get(rel) != expected_hex:
                    raise PackError("Staged source/version mismatch; refusing replacement: " + rel)
                continue
        copy_checked(source, target, expected)
        added += 1
        if index and index % 5000 == 0:
            print(json.dumps({"phase": "copy", "copiedFiles": index}), flush=True)
    regenerate_records(work, packages)
    write_json(work / "dependencies.lock.json", {"schema": 1, "target": "cp312-win_amd64",
               "python": "3.12.10", "sourcePythonABI": "cp312", "packages": packages,
               "excluded": trimmed, "supplementalLicenses": list(LICENSES.values()),
               "policy": "Original RECORD hashes validated; delivery RECORDs regenerated after filtering; score-pack.json hashes every delivered file."})
    write_json(work / "python-runtime.lock.json", {"schema": 1, "pythonVersion": "3.12.10", "architecture": "amd64",
               "url": PYTHON_URL, "sha256": PYTHON_SHA, "extraction": "official archive only; never venv launchers or host stdlib",
               "sitePolicy": "isolated _pth; no .pth files; owned sitecustomize only; fixed package-local DLL directories; no sys.path injection",
               "externalPrerequisites": ["Windows x64", "Microsoft Visual C++ 2015-2022 x64 runtime (msvcp140.dll); not copied from System32"],
               "securityLimit": "3.12.10 is a historical pinned runtime; security review/update required before public release"})
    print('{"phase":"hash-and-privacy-scan"}', flush=True)
    files, _ = inventory(work, scanner_for(config))
    if len(files) > 60000:
        raise PackError("Native 60000-file limit exceeded")
    write_json(work / "score-pack.json", {"schema": 1, "name": "Usic ScoreLab", "version": "0.1.0",
               "platform": "windows-x64", **PATHS, "usage": "personal-noncommercial-testing-only", "files": files})
    relocated.parent.mkdir(parents=True, exist_ok=True)
    if work != relocated:
        os.replace(work, relocated)  # One payload, no duplicate model copies or ZIP temp.
    print('{"phase":"relocated-runtime-checks"}', flush=True)
    checks = runtime_checks(relocated, smoke=args.smoke)
    report = verify_pack(relocated, scanner_for(config))
    DEST.parent.mkdir(parents=True, exist_ok=True)
    os.replace(relocated, DEST)
    scratch = Path(tempfile.mkdtemp(prefix="最终 移动-", dir=BUILD))
    try:
        checks["finalLocationImports"] = import_check(DEST, scratch, qa_environment(scratch))
    finally:
        clean_scratch(scratch)
    report.update({"status": "built-and-verified", "artifact": "dist/" + NAME, "zip": None,
                   "createdAt": datetime.now(timezone.utc).isoformat(), "installedPackages": len(packages),
                   "unitTests": unit_tests, "resumedValidation": args.resume_validation,
                   "copiedFilesThisRun": added,
                   "sizeGiB": round(report["sizeBytes"] / 1024**3, 4), "freeBytesAfter": shutil.disk_usage(BUILD).free,
                   "privacy": {"passed": True, "allPayloadBytesScanned": True, "zipMembersScanned": True,
                               "sourcePathsInManifest": False, "accountsCachesLogsDatabasesPersonalMusic": False},
                   "validation": checks,
                   "interfaces": {"native": "Select the directory containing score-pack.json; native creates local bundle-config.json outside the pack.",
                       "worker": '<pack>/python/python.exe -I -B <app>/capabilities/score_lab/entry.py worker --config <local-bundle-config.json> --sample <new-id> --audio <input.wav> --seconds 30',
                       "notationOnly": '<pack>/python/python.exe -I -B <app>/capabilities/score_lab/entry.py worker --config <local-bundle-config.json> --sample <id> --notation-only',
                       "editing": '<pack>/python/python.exe -I -B <app>/capabilities/score_lab/entry.py editing --config <local-bundle-config.json> (requires caller-owned editing module)'},
                   "knownLimitations": ["Personal noncommercial testing only; no public distribution/upload authorization.",
                       "Windows x64 CPU/FP32 only; no clean Windows VM, GPU, installer or full UI acceptance test.",
                       "Microsoft Visual C++ 2015-2022 x64 redistributable required; system DLLs were not repackaged.",
                       "Python 3.12.10 is pinned but historical; update/security and full Sigstore trust verification remain release work.",
                       "Manifest SHA-256 detects corruption, not publisher authenticity; no signature or malicious-pack trust guarantee.",
                       "Application entry/worker/editing are owned by the main app, not duplicated into this pack.",
                       "Synthetic inference demonstrates execution/portability, not transcription accuracy.",
                       "No ZIP created, avoiding a second large payload and compression temporary files."]})
    write_json(BUILD / "build-result.json", report)
    write_json(RESULT, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".private/score-lab/config.json")
    parser.add_argument("--runtime-zip", type=Path, default=ROOT / "output/packaging-cache/python-3.12.10-embed-amd64.zip")
    parser.add_argument("--fetch-licenses", action="store_true", help="Explicitly fetch two version/hash-pinned missing license texts; no package/model downloads")
    parser.add_argument("--smoke", action="store_true", help="Actually infer a NEW synthetic 30-second WAV; delete all temporary data")
    parser.add_argument("--verify-only", action="store_true", help="Verify the existing final directory without rebuilding/copying it")
    parser.add_argument("--resume-validation", action="store_true", help="Rehash complete owned staging, add newly required RECORD files, then retry QA; no second full payload")
    args = parser.parse_args(argv)
    if args.verify_only and args.resume_validation:
        parser.error("--verify-only and --resume-validation are mutually exclusive")
    try:
        report = build(args)
    except (PackError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Avoid persistent source-path tracebacks or logs in the build directory.
        message = str(exc).replace(str(ROOT), "<app>").replace(str(Path.home()), "<home>")
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps({key: report[key] for key in ("status", "filesCount", "sizeBytes") if key in report}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
