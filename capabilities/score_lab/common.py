"""Small, stdlib-only boundary shared by the demo server and model worker."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / ".private/score-lab/config.json"
SAMPLE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
MAX_UPLOAD = 64 * 1024 * 1024
DEFAULT_CLIP = 30.0
MAX_CLIP = 900.0
ROLES = ("vocal", "instrumental", "chords")
EXPORTS = {"transcription.mid", "melody_vocal.mid", "melody_instrumental.mid",
           "chords.mid", "score.abc", "score.musicxml", "notes.json",
           "arrangement.abc", "arrangement.musicxml"}
MEDIA = {"original.wav", "piano_mix.wav", "piano_vocal.wav",
         "piano_instrumental.wav", "piano_chords.wav"}


def read_json(path: Path, limit=2 * 1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError("JSON exceeds size limit")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path: Path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_config(path=None):
    path = Path(path or os.environ.get("USIC_SCORE_LAB_CONFIG", DEFAULT_CONFIG)).resolve()
    config = read_json(ROOT / "capabilities/score_lab/config.example.json")
    if path.is_file():
        config.update(read_json(path, 32768))
    for key in ("dataDir", "python", "sheetRepo", "baseRepo", "assetsDir"):
        config[key] = (ROOT / config[key]).resolve()
    config["configPath"] = path
    if config["device"] not in ("cpu", "cuda"):
        raise ValueError("device must be cpu or cuda")
    config["threads"] = max(1, min(4, int(config["threads"])))
    config["port"] = int(config["port"])
    config["jobTimeoutSeconds"] = max(60, min(7200, int(config["jobTimeoutSeconds"])))
    if not 0 <= config["port"] <= 65535:
        raise ValueError("Invalid port")
    return config


def child_file(root: Path, *parts):
    """An explicit file under a configured root, never an escaped/symlink target."""
    root = root.resolve()
    candidate = root.joinpath(*parts)
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Path escapes local demo directory")
    return candidate


def sample_dir(config, sample_id):
    if not isinstance(sample_id, str) or not SAMPLE_ID.fullmatch(sample_id):
        raise ValueError("Invalid sample ID")
    return child_file(config["dataDir"] / "samples", sample_id)


def role_of(name):
    name = name.strip().lower()
    if "chord" in name:
        return "chords"
    if "vocal" in name:
        return "vocal"
    return "instrumental"


def validate_playback(data):
    """Only original-second MIDI events; never concatenate LAB and MIDI views."""
    duration = float(data["duration"])
    if not math.isfinite(duration) or not 0 < duration <= MAX_CLIP + .1:
        raise ValueError("Invalid clip duration")
    tracks = []
    for track in data["tracks"]:
        notes = []
        for note in track["notes"]:
            start, end = float(note["start"]), float(note["end"])
            pitch, velocity = note["pitch"], note.get("velocity", 100)
            if (not all(math.isfinite(x) for x in (start, end)) or
                    not 0 <= start < end <= duration + .02 or
                    type(pitch) is not int or not 0 <= pitch <= 127 or
                    type(velocity) is not int or not 0 <= velocity <= 127):
                raise ValueError("Invalid original-time MIDI note")
            notes.append(dict(pitch=pitch, start=start, end=end, velocity=velocity))
        if notes:
            tracks.append(dict(role=role_of(track["name"]), name=track["name"], notes=notes))
    if sum(len(t["notes"]) for t in tracks) > 15000:
        raise ValueError("Too many notes for a demo clip")
    measures = []
    for row in data.get("measures", []):
        start, end = float(row["start"]), float(row["end"])
        if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= duration + .02:
            raise ValueError("Invalid score/audio measure map")
        measures.append({k: row[k] for k in ("index", "start", "end", "leading_rest", "trailing_rest")})
    return dict(duration=duration, tracks=tracks, measures=measures)


def public_sample(directory):
    meta = read_json(child_file(directory, "sample.json"), 32768)
    status_path = child_file(directory, "status.json")
    status = read_json(status_path, 32768) if status_path.is_file() else {"state": "pending"}
    return {"id": directory.name, **{k: meta.get(k) for k in
            ("title", "artist", "source", "offset", "duration", "full", "createdAt")},
            "status": status}
