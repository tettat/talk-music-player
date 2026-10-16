"""Bounded, offline SheetSage2 inference and sample-based piano rendering.

Run in the configured model environment. The HTTP server never imports torch.
All audio, caches, scores and logs remain in the independent private data dir.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

if __package__:
    from .lifetime import join_job
    from .common import (MAX_CLIP, ROLES, atomic_json, child_file, load_config,
                         read_json, sample_dir, sha256, validate_playback)
else:
    from lifetime import join_job
    from common import (MAX_CLIP, ROLES, atomic_json, child_file, load_config,
                        read_json, sample_dir, sha256, validate_playback)

MODEL = "m-a-p/SheetSage2"
REVISION = "32c7c7473e7b59e1ed36bd8a8fefbd11ae8d468a"
NOTE_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")


def status(directory, state, phase, **extra):
    atomic_json(directory / "status.json", {"state": state, "phase": phase, **extra})
    print(json.dumps({"state": state, "phase": phase}, ensure_ascii=True), flush=True)


def verify_assets(directory):
    inventory = read_json(Path(__file__).with_name("render-assets.lock.json"))["files"]
    for name, digest in inventory.items():
        path = child_file(directory, name)
        if not path.is_file() or sha256(path) != digest:
            raise ValueError("Rendering asset missing or corrupt: " + name)
    return inventory


def verify_model(config):
    lock = read_json(Path(__file__).with_name("model.lock.json"))
    for key, inventory in lock["repositories"].items():
        for name, digest in inventory.items():
            path = child_file(config[key], name)
            if not path.is_file() or sha256(path) != digest:
                raise ValueError(f"Unsupported or corrupt model build: {key}/{name}")


def render_piano(playback, assets, output, rate=44100):
    """Render the serialized MIDI's seconds, not a fixed-tempo score timeline.

    FluidR3 samples are literal piano notes; chord voicing comes from the model's
    exported MIDI, not a purported transcription of every accompaniment part.
    A shared peak guard keeps solo and mix levels comparable. No added reverb,
    guessed octave correction, or merged LAB events.
    """
    import numpy as np
    import soundfile as sf

    frames = round(playback["duration"] * rate)
    groups = {role: np.zeros((frames, 2), dtype=np.float32) for role in ROLES
              if any(t["role"] == role for t in playback["tracks"])}
    cache = {}
    for track in playback["tracks"]:
        destination = groups[track["role"]]
        for note in track["notes"]:
            pitch = note["pitch"]
            if not 21 <= pitch <= 109:
                raise ValueError("Piano preview supports MIDI pitches 21–109")
            if pitch not in cache:
                name = f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}.mp3"
                path = child_file(assets, "soundfonts", "acoustic_grand_piano-mp3", name)
                data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
                if sample_rate != rate:
                    x = np.arange(round(len(data) * rate / sample_rate)) * sample_rate / rate
                    data = np.column_stack([np.interp(x, np.arange(len(data)), c) for c in data.T]).astype("float32")
                if data.shape[1] == 1:
                    data = np.repeat(data, 2, axis=1)
                cache[pitch] = data[:, :2]
            start = round(note["start"] * rate)
            count = min(round(note["end"] * rate) - start, frames - start, len(cache[pitch]))
            if count <= 0:
                continue
            data = cache[pitch][:count].copy()
            release = min(count, round(.035 * rate))
            data[-release:] *= np.linspace(1, 0, release, dtype=np.float32)[:, None]
            destination[start:start + count] += data * (note["velocity"] / 127)
    mix = sum(groups.values(), np.zeros((frames, 2), dtype=np.float32))
    groups["mix"] = mix
    peak = max((float(np.max(np.abs(x))) for x in groups.values()), default=0)
    rms = float(np.sqrt(np.mean(mix ** 2)))
    gain = min(.08 / rms, .96 / peak) if rms and peak else 1
    evidence = {}
    for role, pcm in groups.items():
        pcm *= gain
        sf.write(output / f"piano_{role}.wav", pcm, rate, subtype="PCM_16")
        evidence[role] = {"frames": frames, "sampleRate": rate, "channels": 2,
                          "rms": round(float(np.sqrt(np.mean(pcm ** 2))), 7),
                          "peak": round(float(np.max(np.abs(pcm))), 6)}
    return evidence


def prepare_clip(source, destination, offset, seconds, precut=False):
    import numpy as np
    import soundfile as sf

    if (not math.isfinite(offset) or offset < 0 or
            seconds is not None and (not math.isfinite(seconds) or not 1 <= seconds <= MAX_CLIP)):
        raise ValueError("Clip must be 1–900 seconds with a nonnegative offset")
    info = sf.info(source)
    if not 8000 <= info.samplerate <= 192000 or not 1 <= info.channels <= 8:
        raise ValueError("Unsupported audio sample rate/channels")
    start = 0 if precut else round(offset * info.samplerate)
    if start >= info.frames:
        raise ValueError("Clip begins after the end of the audio")
    remaining = info.frames - start
    if seconds is None and remaining > round(MAX_CLIP * info.samplerate):
        raise ValueError("Whole audio exceeds the 15-minute local limit")
    frames = min(round(seconds * info.samplerate), remaining) if seconds is not None else remaining
    if frames * info.channels * 4 > 512 * 1024 * 1024:
        raise ValueError("Decoded audio exceeds the local memory budget")
    with sf.SoundFile(source) as audio:
        audio.seek(start)
        pcm = audio.read(frames, dtype="float32", always_2d=True)
    if len(pcm) < info.samplerate or not np.isfinite(pcm).all():
        raise ValueError("Audio is too short or contains invalid samples")
    if precut:
        if len(pcm) != info.frames or source.suffix.lower() != ".wav":
            raise ValueError("Reusing a result requires its exact complete WAV clip")
        shutil.copyfile(source, destination)
    else:
        sf.write(destination, pcm[:, :2], info.samplerate, subtype="PCM_16")
        pcm, _ = sf.read(destination, dtype="float32", always_2d=True)
    energy = np.max(np.abs(pcm), axis=1)
    bins = max(240, min(1200, round(len(pcm) / info.samplerate * 8)))
    waveform = [round(float(np.max(x)), 5) if len(x) else 0 for x in np.array_split(energy, bins)]
    return pcm, info.samplerate, waveform


def infer(config, pcm, rate, output, directory):
    sheet = config["sheetRepo"]
    if not sheet.name.isidentifier():
        raise ValueError("sheetRepo directory name must be a Python identifier")
    # Explicit offline imports avoid transformers' nested local-code cache bug.
    os.environ["HF_HOME"] = str(config["dataDir"] / "hf-cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    status(directory, "running", "loading")
    verify_model(config)
    import torch

    torch.set_num_threads(config["threads"])
    sys.path.insert(0, str(sheet.parent))
    cls = importlib.import_module(sheet.name + ".modeling_sheetsage2").SheetSage2Model
    model = cls.from_pretrained(str(sheet), base_model_path=str(config["baseRepo"]),
                               local_files_only=True).eval().to(config["device"])
    status(directory, "running", "recognizing")

    last_progress = [0.0]

    def progress(value):
        if value.get("stage") == "notation":
            status(directory, "running", "notation")
        elif value.get("stage") in {"encoding", "decoding", "window_complete"}:
            now = time.monotonic()
            if value["stage"] != "decoding" or now - last_progress[0] >= 1:
                status(directory, "running", "recognizing", **{k: value[k] for k in
                       ("window", "windows", "tokens") if k in value})
                last_progress[0] = now

    started = time.monotonic()
    with torch.inference_mode():
        result = model.transcribe(torch.as_tensor(pcm.mean(axis=1), dtype=torch.float32),
                                  sampling_rate=rate, output_dir=output, dtype="fp32",
                                  preset="default", progress=progress)
    return {"modelExecuted": True, "model": MODEL, "revision": REVISION,
            "device": config["device"], "dtype": "fp32",
            "inferenceSeconds": round(time.monotonic() - started, 2),
            "windows": result.get("windows", []), "duration": result.get("duration_seconds"),
            "warnings": result.get("warnings") or [], "reused": False}


def notation_result(config, output, playback, abc):
    if __package__:
        from .accompaniment import build_arrangement, export_voice_musicxml
    else:
        from accompaniment import build_arrangement, export_voice_musicxml
    warnings, arrangement = [], None
    if abc:
        try:
            export_voice_musicxml(abc, output / "score.musicxml")
        except Exception as exc:
            print(f"[notation] Original MusicXML: {type(exc).__name__}: {exc}", file=sys.stderr)
            warnings.append("Melody MusicXML conversion unavailable; original ABC/MIDI retained")
        try:
            arrangement = build_arrangement(config, output, playback, abc)
        except Exception as exc:
            print(f"[notation] Accompaniment: {type(exc).__name__}: {exc}", file=sys.stderr)
            warnings.append("Accompaniment engraving unavailable; original-time notes/playback retained")
    return arrangement, warnings


def export_names(output, arrangement):
    names = {"transcription.mid", "melody_vocal.mid", "melody_instrumental.mid", "chords.mid",
             "score.abc", "score.musicxml", "notes.json"}
    if arrangement and arrangement.get("verified"):
        names.update(("arrangement.abc", "arrangement.musicxml"))
    return [name for name in sorted(names) if (output / name).is_file()]


def build_result(config, directory, waveform, provenance):
    import re

    output = directory / "result"
    playback = validate_playback(read_json(output / "playback.json"))
    atomic_json(output / "notes.json", playback)
    abc_path = output / "score.abc"
    abc = abc_path.read_text(encoding="utf-8") if abc_path.is_file() else ""
    warnings = list(provenance.get("warnings", []))
    arrangement, notation_warnings = notation_result(config, output, playback, abc)
    warnings.extend(notation_warnings)
    status(directory, "running", "rendering")
    evidence = render_piano(playback, config["assetsDir"], directory)
    counts = {role: sum(len(t["notes"]) for t in playback["tracks"] if t["role"] == role) for role in ROLES}
    key_match, tempo_match, meter_match = (re.search(p, abc, re.M) for p in
                                         (r"^K:(.+)$", r"^Q:1/4=(\d+)", r"^M:(.+)$"))
    chords = []
    chord_path = output / "chord.lab"
    if chord_path.is_file():
        for line in chord_path.read_text(encoding="utf-8").splitlines():
            a, b, label = line.split(maxsplit=2)
            chords.append({"start": float(a), "end": float(b), "label": label})
    result = {**playback, "abc": abc, "arrangement": arrangement, "notationVersion": 2,
              "waveform": waveform, "counts": counts, "chords": chords,
              "key": key_match[1].strip() if key_match else None,
              "notationBpm": int(tempo_match[1]) if tempo_match else None,
              "meter": meter_match[1].strip() if meter_match else None,
              "audioParts": list(evidence), "audioEvidence": evidence,
              "exports": export_names(output, arrangement),
              "provenance": {**provenance, "inputSha256": sha256(directory / "original.wav"),
                             "playbackClock": "original MIDI seconds",
                             "scoreClock": "quantized model ABC; measure-level alignment only",
                             "piano": "FluidR3 samples by Frank Wen (CC BY 3.0 US)",
                             "groundTruthAvailable": False, "warnings": warnings}}
    atomic_json(directory / "result.json", result)
    meta = read_json(directory / "sample.json")
    meta["duration"] = playback["duration"]
    atomic_json(directory / "sample.json", meta)
    status(directory, "completed", "ready", finishedAt=datetime.now(timezone.utc).isoformat())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--audio", type=Path, help="CLI-only import; never a browser-supplied server path")
    parser.add_argument("--title")
    parser.add_argument("--artist", default="")
    parser.add_argument("--source", default="local")
    parser.add_argument("--offset", type=float, default=0)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--full", action="store_true", help="Transcribe the complete audio from the chosen offset")
    parser.add_argument("--reuse", type=Path, help="Exact previously verified result directory; no inference claim")
    parser.add_argument("--rerender", action="store_true", help="Rebuild previews without re-running the model")
    parser.add_argument("--notation-only", action="store_true", help="Add staff/exports without changing audio or MIDI")
    parser.add_argument("--job-name", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.job_name:
        join_job(args.job_name)
    config = load_config(args.config)
    directory = sample_dir(config, args.sample)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if args.notation_only:
            previous = read_json(directory / "result.json")
            output = directory / "result"
            arrangement, warnings = notation_result(config, output, previous, previous["abc"])
            if not arrangement and previous["counts"]["chords"]:
                raise ValueError("Accompaniment engraving did not pass validation")
            previous.update(arrangement=arrangement, notationVersion=2, exports=export_names(output, arrangement))
            previous["provenance"]["warnings"] = list(dict.fromkeys(previous["provenance"].get("warnings", []) + warnings))
            atomic_json(directory / "result.json", previous)
            print("Notation updated; original audio/MIDI unchanged", flush=True)
            return 0
        if args.rerender:
            previous = read_json(directory / "result.json")
            verify_assets(config["assetsDir"])
            build_result(config, directory, previous["waveform"], previous["provenance"])
            return 0
        if args.audio:
            if (directory / "sample.json").exists():
                raise ValueError("Sample already exists; use a new ID")
            meta = {"title": args.title or args.audio.stem, "artist": args.artist, "source": args.source,
                    "offset": args.offset, "duration": None if args.full else args.seconds, "full": args.full,
                    "createdAt": datetime.now(timezone.utc).isoformat()}
            atomic_json(directory / "sample.json", meta)
            source, offset, seconds = args.audio.resolve(), args.offset, None if args.full else args.seconds
        else:
            request = read_json(directory / "request.json")
            source = child_file(directory, request["input"])
            offset, seconds = float(request["offset"]), request["seconds"]
            seconds = None if seconds is None else float(seconds)
        status(directory, "running", "preparing")
        verify_assets(config["assetsDir"])
        pcm, rate, waveform = prepare_clip(source, directory / "original.wav", offset, seconds, precut=bool(args.reuse))
        meta = read_json(directory / "sample.json")
        meta["duration"] = len(pcm) / rate
        atomic_json(directory / "sample.json", meta)
        output = directory / "result"
        output.mkdir(exist_ok=True)
        if args.reuse:
            original_meta = read_json(args.reuse / "run_meta.json")
            if original_meta["input"]["sha256"] != sha256(directory / "original.wav"):
                raise ValueError("Existing recognition does not match the exact input clip")
            allowed = {"playback.json", "result.json", "score.abc", "score.musicxml", "chord.lab",
                       "key.lab", "beat.lab", "transcription.mid", "melody_vocal.mid",
                       "melody_instrumental.mid", "chords.mid"}
            allowed.update(("notation/song_beats.txt", "notation/song_keys.txt"))
            for name in allowed:
                p = args.reuse / name
                if p.is_file():
                    (output / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(p, output / name)
            provenance = {"modelExecuted": True, "model": MODEL, "revision": REVISION,
                          "device": original_meta["device"], "dtype": original_meta["dtype"],
                          "inferenceSeconds": original_meta["elapsedSecondsInference"],
                          "reused": True, "warnings": original_meta.get("warnings") or []}
        else:
            provenance = infer(config, pcm, rate, output, directory)
        build_result(config, directory, waveform, provenance)
        return 0
    except Exception as exc:
        if not args.notation_only:
            status(directory, "failed", "error", message="识别未完成，请检查音频或本机模型环境。")
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
