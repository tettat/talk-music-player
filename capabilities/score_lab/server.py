"""Loopback-only, standalone score lab. Stdlib server; serialized model processes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

if __package__:
    from .lifetime import DataLock, OwnedJob
    from .common import (EXPORTS, MAX_UPLOAD, MAX_CLIP, MEDIA, ROOT, atomic_json, child_file,
                         load_config, public_sample, read_json, sample_dir, sha256)
else:
    from lifetime import DataLock, OwnedJob
    from common import (EXPORTS, MAX_UPLOAD, MAX_CLIP, MEDIA, ROOT, atomic_json, child_file,
                        load_config, public_sample, read_json, sample_dir, sha256)

WEB = Path(__file__).with_name("web")
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.mjs": "app.mjs",
          "/timeline.mjs": "timeline.mjs", "/style.css": "style.css", "/embedded.css": "embedded.css"}
ABCJS_SHA256 = "abdab74cf95c39fb9ff4ae0c96735b9c35222851f0844ce471ddd4354739bc75"


def byte_range(value, size):
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()) or size == 0:
        raise ValueError("Invalid byte range")
    a, b = match.groups()
    if not a:
        count = int(b)
        if count <= 0:
            raise ValueError("Invalid suffix range")
        return max(0, size - count), size - 1, True
    start, end = int(a), min(int(b), size - 1) if b else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, end, True


def runtime_ready(config):
    return all(p.is_file() for p in (config["python"], config["sheetRepo"] / "model.safetensors",
                                   config["baseRepo"] / "model.safetensors",
                                   config["assetsDir"] / "manifest.json"))


class Lab:
    def __init__(self, config):
        self.config = config
        self.token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.jobs = queue.Queue(maxsize=3)
        self.closed = threading.Event()
        self.process = None
        self.owned_job = None
        self.process_lock = threading.Lock()
        self.recover()
        self.thread = threading.Thread(target=self.dispatch, name="score-lab-jobs", daemon=True)
        self.thread.start()

    def samples(self):
        root = self.config["dataDir"] / "samples"
        result = []
        if root.is_dir():
            for directory in root.iterdir():
                try:
                    directory = sample_dir(self.config, directory.name)
                    if (directory / "sample.json").is_file():
                        result.append(public_sample(directory))
                except (ValueError, KeyError, OSError):
                    continue
        return sorted(result, key=lambda item: item["createdAt"] or "")

    def dispatch(self):
        while not self.closed.is_set():
            try:
                sample_id = self.jobs.get(timeout=.5)
            except queue.Empty:
                continue
            directory = sample_dir(self.config, sample_id)
            try:
                with OwnedJob() as job, (directory / "worker.log").open("wb") as log:
                    command = [str(self.config["python"]), "-m", "capabilities.score_lab.worker",
                               "--config", str(self.config["configPath"]), "--sample", sample_id]
                    if job.name:
                        command.extend(["--job-name", job.name])
                    with self.process_lock:
                        if self.closed.is_set():
                            raise RuntimeError("Server closing")
                        self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                                        stdout=log, stderr=subprocess.STDOUT,
                                                        start_new_session=os.name != "nt",
                                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        self.owned_job = job
                        job.process = self.process
                        process = self.process
                    try:
                        code = process.wait(timeout=self.config["jobTimeoutSeconds"])
                    except subprocess.TimeoutExpired:
                        job.close()
                        process.kill()
                        process.wait(timeout=15)
                        raise RuntimeError("Recognition timeout")
                    if code != 0 or read_json(directory / "status.json").get("state") != "completed":
                        raise RuntimeError("Model worker failed")
            except Exception:
                atomic_json(directory / "status.json", {"state": "failed", "phase": "error",
                                                       "message": "识别未完成，请检查音频或本机模型环境。"})
            finally:
                with self.process_lock:
                    self.process = None
                    self.owned_job = None
                self.jobs.task_done()

    def recover(self):
        # DataLock is held before a real Lab starts. A surviving CLI-only
        # experiment has no request.json and is not ours to mark interrupted.
        for info in self.samples():
            directory = sample_dir(self.config, info["id"])
            if (directory / "request.json").is_file() and info["status"].get("state") in {"queued", "running"}:
                atomic_json(directory / "status.json", {"state": "failed", "phase": "interrupted",
                                                       "message": "上次识别已中断，请重新导入。"})

    def close(self):
        self.closed.set()
        with self.process_lock:
            if self.owned_job:
                self.owned_job.close()
            if self.process and self.process.poll() is None:
                self.process.terminate()
        self.thread.join(timeout=15)
        while True:
            try:
                pending = self.jobs.get_nowait()
            except queue.Empty:
                break
            atomic_json(sample_dir(self.config, pending) / "status.json",
                        {"state": "failed", "phase": "interrupted", "message": "服务已关闭，请重新导入。"})
            self.jobs.task_done()


class Handler(BaseHTTPRequestHandler):
    server_version = "UsicScoreLab/1"

    @property
    def lab(self):
        return self.server.lab

    def log_message(self, *_args):
        pass  # no private titles, paths or upload requests in access logs

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def send_headers(self, status, length, content_type, **extra):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
                         "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()

    def json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_headers(status, len(body), "application/json; charset=utf-8")
        if self.command != "HEAD":
            self.wfile.write(body)

    def file(self, path, download=False):
        size = path.stat().st_size
        try:
            start, end, partial = byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_headers(416, 0, "text/plain", **{"Content-Range": f"bytes */{size}"})
            return
        mime = {".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css; charset=utf-8",
                ".html": "text/html; charset=utf-8", ".mid": "audio/midi", ".abc": "text/plain; charset=utf-8",
                ".musicxml": "application/vnd.recordare.musicxml+xml"}.get(path.suffix)
        mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        extra = {"Accept-Ranges": "bytes"}
        if partial:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        if download:
            extra["Content-Disposition"] = f'attachment; filename="{path.name}"'
        self.send_headers(206 if partial else 200, max(0, end - start + 1), mime, **extra)
        if self.command != "HEAD":
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = stream.read(min(128 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            if not self.valid_host():
                return self.json({"message": "仅允许本机访问"}, 403)
            path = unquote(urlsplit(self.path).path)
            if path in STATIC:
                return self.file(WEB / STATIC[path])
            if path == "/favicon.ico":
                return self.file(ROOT / "desktop/assets/usic.ico")
            if path == "/flower.png":
                return self.file(ROOT / "web/assets/usic.png")
            if path == "/vendor/abcjs-basic-min.js":
                asset = child_file(self.lab.config["assetsDir"], "abcjs-basic-min.js")
                if sha256(asset) != ABCJS_SHA256:
                    raise ValueError("ABC renderer checksum mismatch")
                return self.file(asset)
            if path == "/api/health":
                return self.json({"service": "usic-score-lab", "online": True,
                                  "instance": self.lab.instance,
                                  "runtimeReady": runtime_ready(self.lab.config)})
            if path == "/api/catalog":
                return self.json({"samples": self.lab.samples(), "token": self.lab.token,
                                  "runtimeReady": runtime_ready(self.lab.config)})
            match = re.fullmatch(r"/api/samples/([a-z0-9-]+)", path)
            if match:
                directory = sample_dir(self.lab.config, match[1])
                info = public_sample(directory)
                if info["status"]["state"] == "completed":
                    info["result"] = read_json(child_file(directory, "result.json"))
                return self.json(info)
            match = re.fullmatch(r"/(media|export)/([a-z0-9-]+)/([a-zA-Z0-9_.-]+)", path)
            if match:
                kind, sample_id, name = match.groups()
                if name not in (MEDIA if kind == "media" else EXPORTS):
                    raise FileNotFoundError()
                directory = sample_dir(self.lab.config, sample_id)
                # Partial artifacts of an unfinished worker are never exposed.
                if public_sample(directory)["status"]["state"] != "completed":
                    return self.json({"message": "识别尚未完成"}, 409)
                return self.file(child_file(directory, *(["result", name] if kind == "export" else [name])),
                                 download=kind == "export")
            return self.json({"message": "未找到"}, 404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, socket.timeout):
            return  # Browsers cancel old Range requests while switching A/B.
        except (FileNotFoundError, NotADirectoryError):
            self.json({"message": "未找到"}, 404)
        except (ValueError, KeyError, TypeError, OSError):
            self.json({"message": "本机资料暂不可用"}, 422)

    def do_POST(self):
        directory = None
        accepted = False
        try:
            port = self.server.server_port
            if (not self.valid_host() or
                    self.headers.get("Origin") not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"} or
                    not secrets.compare_digest(self.headers.get("X-Score-Lab-Token", ""), self.lab.token)):
                return self.json({"message": "请求来源不受信任"}, 403)
            url = urlsplit(self.path)
            if url.path != "/api/import":
                return self.json({"message": "未找到"}, 404)
            if not runtime_ready(self.lab.config):
                return self.json({"message": "本机模型环境尚未配置"}, 503)
            if self.lab.jobs.full():
                return self.json({"message": "请等待当前识别完成"}, 429)
            if self.headers.get("Transfer-Encoding"):
                return self.json({"message": "不支持此上传方式"}, 400)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_UPLOAD:
                return self.json({"message": "请选择 64 MB 以内的音频"}, 413)
            params = parse_qs(url.query, max_num_fields=5)
            name = params.get("name", [""])[0]
            ext = Path(name).suffix.lower()
            if ext not in {".wav", ".mp3", ".flac", ".ogg", ".aiff", ".aif"}:
                return self.json({"message": "请选择 MP3、WAV、FLAC 或 OGG 音频"}, 400)
            offset = float(params.get("offset", [0])[0])
            if not math.isfinite(offset) or not 0 <= offset <= 86400:
                return self.json({"message": "片段起点无效"}, 400)
            length_choice = params.get("seconds", ["30"])[0]
            seconds = None if length_choice == "full" else float(length_choice)
            if seconds is not None and (not math.isfinite(seconds) or not 1 <= seconds <= MAX_CLIP):
                return self.json({"message": "识别长度无效"}, 400)
            sample_id = secrets.token_hex(12)
            directory = sample_dir(self.lab.config, sample_id)
            directory.mkdir(parents=True, exist_ok=False)
            incoming = directory / ("input" + ext)
            with incoming.open("wb") as stream:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(128 * 1024, remaining))
                    if not block:
                        raise ValueError("Upload interrupted")
                    stream.write(block)
                    remaining -= len(block)
            title = re.sub(r"[\x00-\x1f\x7f]", "", Path(name.replace("\\", "/")).stem)[:80] or "未命名"
            atomic_json(directory / "sample.json", {"title": title, "artist": "", "source": "local",
                        "offset": offset, "duration": seconds, "full": seconds is None,
                        "createdAt": datetime.now(timezone.utc).isoformat()})
            atomic_json(directory / "request.json", {"input": incoming.name, "offset": offset, "seconds": seconds})
            atomic_json(directory / "status.json", {"state": "queued", "phase": "queued"})
            self.lab.jobs.put_nowait(sample_id)
            accepted = True
            self.json({"id": sample_id}, 202)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, socket.timeout):
            # Once queued, a client navigation must not turn a real job into a
            # failure. Its persisted result can be reopened from the catalog.
            if not accepted and directory is not None and (directory / "sample.json").is_file():
                atomic_json(directory / "status.json", {"state": "failed", "phase": "error", "message": "导入未完成"})
            return
        except (ValueError, OSError, queue.Full):
            if directory is not None and (directory / "sample.json").is_file():
                atomic_json(directory / "status.json", {"state": "failed", "phase": "error", "message": "导入未完成"})
            self.json({"message": "导入未完成，请重新选择音频"}, 400)


class LocalServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR can silently share an occupied listening port.
    # Never route this demo's requests to another local app by accident.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(config, args):
    lab = Lab(config)
    try:
        server = LocalServer(("127.0.0.1", args.port if args.port is not None else config["port"]), Handler)
    except OSError:
        lab.close()
        print("本机端口已被占用。可加 --port 0 使用空闲端口。", file=sys.stderr)
        return 1
    server.daemon_threads = True
    server.lab = lab
    url = f"http://127.0.0.1:{server.server_port}/"
    atomic_json(config["dataDir"] / "server.json", {"service": "usic-score-lab", "url": url,
                "instance": lab.instance,
                "pid": os.getpid(), "startedAt": datetime.now(timezone.utc).isoformat()})
    print(f"小黄花 · 谱室  {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        lab.close()
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--port", type=int)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    try:
        with DataLock(config["dataDir"] / "server.lock"):
            return serve(config, args)
    except (BlockingIOError, PermissionError):
        print("谱室已运行。请用 npm run score-lab 打开现有页面。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
