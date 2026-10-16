"""Fetch pinned public rendering assets only; no music or credentials leave this PC.

Weights must be obtained separately under the model's license. Each rendering
file is checked against the official model manifest before being installed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

if __package__:
    from .common import child_file, load_config
else:
    from common import child_file, load_config

REVISION = "32c7c7473e7b59e1ed36bd8a8fefbd11ae8d468a"
MANIFEST_SHA256 = "aa0b4bfb84d71169e4d0c9f6cc3ed5cf8761e49c2c178961aa100cf6d83000b3"


def download(relative, mirror):
    if mirror == "modelscope":
        url = ("https://modelscope.cn/api/v1/models/m-a-p/SheetSage2/repo"
               "?Revision=master&FilePath=" + urllib.parse.quote("render_assets/" + relative))
    else:
        url = f"https://huggingface.co/m-a-p/SheetSage2/resolve/{REVISION}/render_assets/{relative}"
    last = None
    for retry in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={
                    "User-Agent": "Usic-Local-Score-Lab/1.0"}), timeout=30) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("Rendering asset too large")
                return body
        except Exception as exc:
            last = exc
            time.sleep(retry + 1)
    raise RuntimeError(f"Asset download failed: {relative}") from last


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--mirror", choices=("huggingface", "modelscope"), default="huggingface")
    args = parser.parse_args()
    root = load_config(args.config)["assetsDir"]
    root.mkdir(parents=True, exist_ok=True)
    manifest = download("manifest.json", args.mirror)
    if hashlib.sha256(manifest).hexdigest() != MANIFEST_SHA256:
        raise ValueError("Rendering manifest does not match the supported pinned build")
    inventory = json.loads(manifest)["files"]

    def install(item):
        relative, digest = item
        path = child_file(root, relative)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            return
        body = download(relative, args.mirror)
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError(f"Asset checksum mismatch: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(body)
        temporary.replace(path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(install, inventory.items()))
    (root / "manifest.json").write_bytes(manifest)
    print(f"Verified {len(inventory)} rendering assets. No model weights downloaded.")


if __name__ == "__main__":
    main()
