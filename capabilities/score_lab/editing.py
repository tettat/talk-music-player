"""Pitch-only canonical ABC revisions. Importing this module needs only stdlib.

Run the CLI in the model venv, never in the HTTP process. See EDITING.txt for
the draft, immutable revision, and pinned media/export protocols.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
from statistics import median
import struct
import subprocess
import sys
import tempfile

if __package__:
    from .common import load_config, sample_dir, validate_playback
    from .lifetime import DataLock, OwnedJob, join_job
else:
    from common import load_config, sample_dir, validate_playback
    from lifetime import DataLock, OwnedJob, join_job


SCHEMA = 1
MAX_ABC_BYTES = 128 * 1024
MAX_DRAFT_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_NOTES = 15000
MAX_MEASURES = 2048
MAX_BEATS = 10000
MAX_REVISIONS = 1024
MIN_PITCH, MAX_PITCH = 21, 109  # the installed, verified FluidR3 piano samples
EXPORTS = frozenset({"edited.abc", "edited.musicxml", "edited.mid", "notes.json"})
MEDIA = frozenset({"piano_mix.wav", "piano_vocal.wav", "piano_instrumental.wav", "piano_chords.wav"})
VOICE_ROLES = {"Vocal": "vocal", "Ins": "instrumental", "Accomp": "chords"}
REVISION_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
PITCH = re.compile(r"(?:\^{1,2}|_{1,2}|=)?[A-Ga-g][,']{0,4}")
KEY = re.compile(r"[A-G](?:#{1,2}|b{1,2})?(?:m|maj|min|dor|mix|lyd|phr|loc|aeo|ion)?\Z")


class EditError(ValueError):
    code = "invalid_edit"


class EditConflict(EditError):
    code = "conflict"


def _token(value):
    if not isinstance(value, str) or not REVISION_ID.fullmatch(value):
        raise EditError("Invalid revision ID (use 1-64 lowercase letters, digits, hyphens)")
    return value


def _path(root, *parts):
    """Fixed relative paths only; reject symlinks AND Windows junctions/reparse files."""
    root = Path(root).resolve()
    candidate = root
    for part in parts:
        if not isinstance(part, str) or not part or part in (".", "..") or any(c in part for c in "/\\:"):
            raise EditError("Unsafe local path")
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise EditError("Symlinks and reparse points are not accepted")
    if not candidate.resolve().is_relative_to(root):
        raise EditError("Path escapes the sample directory")
    return candidate


def _directory(config, sample_id):
    # sample_dir validates the ID; check unresolved components as well.
    sample_dir(config, sample_id)
    directory = _path(config["dataDir"], "samples", sample_id)
    if not directory.is_dir():
        raise EditError("Sample does not exist")
    return directory


def _bytes(path, limit):
    if not path.is_file():
        raise EditError("Required local artifact is missing")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise EditError("Artifact exceeds size limit")
    return data


def _json(path, limit=MAX_RESULT_BYTES):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EditError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        return json.loads(_bytes(path, limit).decode("utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(EditError("Nonfinite JSON number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EditError("Invalid JSON") from exc


def _hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _normalize_abc(abc):
    if not isinstance(abc, str):
        raise EditError("abc must be a string")
    try:
        size = len(abc.encode("utf-8"))
    except UnicodeError as exc:
        raise EditError("Invalid ABC Unicode") from exc
    if not 1 <= size <= MAX_ABC_BYTES:
        raise EditError("ABC exceeds size limit or is empty")
    abc = abc.replace("\r\n", "\n")
    if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in abc):
        raise EditError("ABC contains control characters")
    return abc.rstrip("\n") + "\n"


def inspect_abc(abc):
    """Bound the pinned canonical grammar BEFORE music21 sees any input.

    Only pitch lexemes are masked. Everything else (including chord labels,
    ties, rest compression, header/body keys, voice order and whitespace) is
    retained in the skeleton, so source editing cannot smuggle other changes.
    """
    abc = _normalize_abc(abc)
    skeleton, pitches, declared, bars = [], [], [], {}
    header, active, unit, have_meter = True, None, None, False
    fields = set()
    cursor = 0
    for raw in abc.splitlines(keepends=True):
        line = raw[:-1]
        if len(line) > 8192:
            raise EditError("ABC line exceeds size limit")
        if line.startswith("%%"):
            raise EditError("ABC directives/includes are forbidden")
        if not line.strip() or line.startswith("%"):
            skeleton.append(raw); cursor += len(raw); continue
        field = re.match(r"^([A-Za-z]):(.*)$", line)
        if field:
            name, value = field[1], field[2].strip()
            if name not in {"X", "T", "M", "L", "Q", "V", "K"}:
                raise EditError("Unsupported ABC field/directive")
            if name == "V":
                voice = value.split()[0] if value else ""
                if voice not in VOICE_ROLES:
                    raise EditError("Unsupported voice; no existing voice may be dropped")
                if header:
                    if voice in declared:
                        raise EditError("Duplicate voice declaration")
                    declared.append(voice); bars[voice] = 0
                else:
                    if voice not in declared or value != voice:
                        raise EditError("Invalid body voice switch")
                    active = voice
            elif name == "M":
                match = re.fullmatch(r"([1-9][0-9]?)/(1|2|4|8|16|32|64)", value)
                if not match or int(match[1]) > 32:
                    raise EditError("Unsupported meter")
                have_meter = True
            elif name == "L":
                match = re.fullmatch(r"1/(1|2|4|8|16|32|64|128)", value)
                if not header or not match or unit is not None:
                    raise EditError("Unsupported unit length")
                unit = int(match[1])
            elif name == "K":
                if not KEY.fullmatch(value):
                    raise EditError("Unsupported key")
                if header:
                    if not declared or unit is None or not have_meter or not {"X", "T"} <= fields:
                        raise EditError("Missing canonical ABC header")
                    header = False
                elif active is None:
                    raise EditError("Body key without a voice")
            elif not header:
                raise EditError("Only meter/key/voice fields are accepted in the body")
            elif name == "X" and value != "1":
                raise EditError("Exactly one canonical tune is required")
            elif name == "Q" and not re.fullmatch(r"1/4=[1-9][0-9]{0,2}", value):
                raise EditError("Unsupported notation tempo")
            if header and name != "V":
                if name in fields:
                    raise EditError("Duplicate ABC header field")
                fields.add(name)
            skeleton.append(raw); cursor += len(raw); continue
        if header or active is None:
            raise EditError("Music must belong to a declared voice")
        out, i = [], 0

        def pitch_at(position):
            match = PITCH.match(line, position)
            if not match:
                raise EditError("Invalid ABC pitch")
            if len(pitches) >= MAX_NOTES * 4:
                raise EditError("Too many notation pitch fragments")
            pitches.append({"startChar": cursor + position, "endChar": cursor + match.end(),
                            "voice": active, "text": match[0]})
            out.append("@")
            return match.end()

        def duration_at(position):
            end = position
            while end < len(line) and line[end].isdigit():
                end += 1
            text = line[position:end]
            if len(text) > 4 or text and (text[0] == "0" or int(text) > 1024):
                raise EditError("Invalid/oversize notation duration")
            out.append(text)
            return end

        while i < len(line):
            ch = line[i]
            if ch in " \t":
                out.append(ch); i += 1
            elif ch == '"':
                end = line.find('"', i + 1)
                if end < 0 or end - i > 129:
                    raise EditError("Invalid/oversize chord label")
                out.append(line[i:end + 1]); i = end + 1
            elif line.startswith("[K:", i):
                end = line.find("]", i + 3)
                if end < 0 or not KEY.fullmatch(line[i + 3:end]):
                    raise EditError("Invalid inline key")
                out.append(line[i:end + 1]); i = end + 1
            elif ch == "[":
                out.append("["); i += 1; count = 0
                while i < len(line) and line[i] != "]":
                    i = pitch_at(i); count += 1
                    if count > 16:
                        raise EditError("Too many pitches in one chord")
                    if i < len(line) and line[i] == "-":
                        out.append("-"); i += 1
                if not count or i == len(line):
                    raise EditError("Invalid chord")
                out.append("]"); i = duration_at(i + 1)
                if i < len(line) and line[i] == "-":
                    out.append("-"); i += 1
            elif ch == "Z":
                match = re.match(r"Z([1-9][0-9]{0,3})?\|", line[i:])
                if not match:
                    raise EditError("Invalid multimeasure rest")
                bars[active] += int(match[1] or 1)
                out.append(match[0]); i += len(match[0])
            elif ch == "z":
                out.append("z"); i = duration_at(i + 1)
            elif ch == "|":
                bars[active] += 1
                out.append("|"); i += 1
            else:
                i = pitch_at(i); i = duration_at(i)
                if i < len(line) and line[i] == "-":
                    out.append("-"); i += 1
            if bars[active] > MAX_MEASURES:
                raise EditError("Too many expanded measures")
        skeleton.append("".join(out) + "\n"); cursor += len(raw)
    if header or not pitches or not bars or min(bars.values()) < 1 or len(set(bars.values())) != 1:
        raise EditError("Missing music or unequal voice measure counts")
    return {"abc": abc, "skeleton": "".join(skeleton), "voices": declared, "pitches": pitches,
            "measures": next(iter(bars.values()))}


def validate_draft(draft):
    if not isinstance(draft, dict) or set(draft) != {"abc", "baseRevision", "sourceFingerprint"}:
        raise EditError("Draft requires exactly abc, baseRevision, sourceFingerprint")
    if type(draft["baseRevision"]) is not int or not 0 <= draft["baseRevision"] < MAX_REVISIONS:
        raise EditError("Invalid baseRevision")
    if not isinstance(draft["sourceFingerprint"], str) or not DIGEST.fullmatch(draft["sourceFingerprint"]):
        raise EditError("Invalid sourceFingerprint")
    return {**draft, "abc": _normalize_abc(draft["abc"])}


def _source(directory):
    arrangement = _path(directory, "result", "arrangement.abc")
    name = "arrangement.abc" if arrangement.exists() else "score.abc"
    canonical = _path(directory, "result", name)
    beat = _path(directory, "result", "notation", "song_beats.txt")
    beat_parts = ("result", "notation", "song_beats.txt") if beat.exists() else ("result", "beat.lab")
    paths = [("result.json", ("result.json",)), ("result/" + name, ("result", name)),
             ("/".join(beat_parts), beat_parts)]
    blobs = {label: _bytes(_path(directory, *parts), MAX_RESULT_BYTES if label == "result.json" else MAX_ABC_BYTES)
             for label, parts in paths}
    inventory = {label: _hash_bytes(data) for label, data in blobs.items()}
    fingerprint = _hash_bytes(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("ascii"))
    try:
        abc = _normalize_abc(blobs["result/" + name].decode("utf-8-sig"))
    except UnicodeError as exc:
        raise EditError("Invalid original ABC Unicode") from exc
    return {"name": name, "abc": abc, "fingerprint": fingerprint, "inputs": inventory,
            "beatPath": _path(directory, *beat_parts), "savedBeats": len(beat_parts) == 3}


def _pointer(directory, sample_id):
    path = _path(directory, "edits", "current.json")
    if not path.exists():
        return {"schemaVersion": SCHEMA, "sampleId": sample_id, "revision": 0, "revisionId": None, "history": []}
    value = _json(path, 256 * 1024)
    try:
        n, history = value["revision"], value["history"]
        if (value["schemaVersion"] != SCHEMA or value["sampleId"] != sample_id or type(n) is not int or
                not 1 <= n <= MAX_REVISIONS or not isinstance(history, list) or len(history) != n or
                not DIGEST.fullmatch(value["sourceFingerprint"])):
            raise EditError("Invalid revision pointer")
        ids = []
        for row in history:
            ids.append(_token(row["revisionId"]))
            if not DIGEST.fullmatch(row["resultSha256"]):
                raise EditError("Invalid revision result digest")
        if len(set(ids)) != n or ids[-1] != value["revisionId"]:
            raise EditError("Invalid revision history")
    except (KeyError, TypeError) as exc:
        raise EditError("Invalid revision pointer") from exc
    return value


def _revision_result(directory, pointer, revision_id):
    revision_id = _token(revision_id)
    matches = [(i + 1, row) for i, row in enumerate(pointer["history"]) if row["revisionId"] == revision_id]
    if len(matches) != 1:
        raise EditError("Revision is not published")
    number, record = matches[0]
    root = _path(directory, "edits", "rev-" + revision_id)
    data = _bytes(_path(root, "result.json"), MAX_RESULT_BYTES)
    if _hash_bytes(data) != record["resultSha256"]:
        raise EditError("Revision result was modified")
    result = _json(_path(root, "result.json"))
    if (result.get("revision") != number or result.get("revisionId") != revision_id or
            result.get("sourceFingerprint") != pointer["sourceFingerprint"]):
        raise EditError("Revision identity mismatch")
    return root, result


def read_current(config, sample_id):
    """Stdlib-only editor bootstrap. Revision 0 uses the ORIGINAL canonical ABC."""
    directory = _directory(config, sample_id)
    source = _source(directory)
    pointer = _pointer(directory, sample_id)
    exports, media, abc = [], [], source["abc"]
    if pointer["revision"]:
        if pointer["sourceFingerprint"] != source["fingerprint"]:
            raise EditConflict("Original score/timing changed; existing edits cannot be silently rebased")
        root, result = _revision_result(directory, pointer, pointer["revisionId"])
        abc = _bytes(_path(root, "edited.abc"), MAX_ABC_BYTES).decode("utf-8")
        if _hash_bytes(abc.encode("utf-8")) != result["artifacts"]["edited.abc"]["sha256"]:
            raise EditError("Revision ABC was modified")
        exports, media = result["exports"], result["media"]
    return {"schemaVersion": SCHEMA, "sampleId": sample_id, "revision": pointer["revision"],
            "revisionId": pointer["revisionId"], "sourceAbc": source["name"],
            "sourceFingerprint": source["fingerprint"], "abc": abc, "exports": exports, "media": media}


def read_revision_result(config, sample_id, revision_id):
    """Read a published, pinned revision; previous revisions remain readable."""
    directory = _directory(config, sample_id)
    return _revision_result(directory, _pointer(directory, sample_id), revision_id)[1]


def revision_file(config, sample_id, revision_id, name, *, kind):
    """HTTP whitelist helper, NOT a user-supplied relative-path resolver."""
    allowed = EXPORTS if kind == "export" else MEDIA if kind == "media" else frozenset()
    if not isinstance(name, str) or name not in allowed:
        raise EditError("Unknown export/media name")
    directory = _directory(config, sample_id)
    root, result = _revision_result(directory, _pointer(directory, sample_id), revision_id)
    if name not in result["exports" if kind == "export" else "media"]:
        raise EditError("Artifact is not available in this revision")
    path = _path(root, name)
    if (not path.is_file() or path.stat().st_size != result["artifacts"][name]["bytes"] or
            _hash_file(path) != result["artifacts"][name]["sha256"]):
        raise EditError("Revision artifact is missing or modified")
    return path


def read_draft(config, sample_id, path):
    """CLI draft files must be server-owned direct children of edits/drafts/."""
    directory = _directory(config, sample_id)
    drafts = _path(directory, "edits", "drafts")
    path = Path(path).absolute()
    if path.parent != drafts or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,69}\.json", path.name):
        raise EditError("Draft must be a safe .json directly under this sample's edits/drafts/")
    return validate_draft(_json(_path(drafts, path.name), MAX_DRAFT_BYTES))


def write_draft(config, sample_id, draft, revision_id):
    """Stdlib-only ingress helper; never accept a browser-provided file path.

    Returns an absolute path suitable for --draft. Exclusive creation prevents
    a queued request's draft from being overwritten by another request.
    """
    draft, revision_id = validate_draft(draft), _token(revision_id)
    inspect_abc(draft["abc"])
    directory = _directory(config, sample_id)
    drafts = _path(directory, "edits", "drafts")
    drafts.mkdir(parents=True, exist_ok=True)
    path = _path(drafts, "draft-" + revision_id + ".json")
    try:
        _write(path, _json_bytes(draft))
    except FileExistsError as exc:
        raise EditConflict("Draft transaction already exists; do not overwrite a queued draft") from exc
    return path


class ScoreClock:
    """Canonical quarters -> original clip seconds, including pickup padding.

    leading_rest/trailing_rest are WHOLE NOTE lengths, not seconds and not
    fractions of a measure. Saved beat times provide every tempo fluctuation.
    """
    def __init__(self, lengths, rows, beat_text, duration, *, saved_beats=True):
        if not 0 < duration <= 900.1 or not math.isfinite(duration):
            raise EditError("Invalid original duration")
        if not 1 <= len(lengths) == len(rows) <= MAX_MEASURES:
            raise EditError("Notation and original measure map disagree")
        beats = []
        for line in beat_text.splitlines():
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 4 or len(beats) >= MAX_BEATS:
                raise EditError("Invalid/oversize saved beat grid")
            try:
                t, beat, numerator, denominator = float(fields[0]), *map(int, fields[1:])
            except (ValueError, OverflowError) as exc:
                raise EditError("Invalid saved beat row") from exc
            if (not math.isfinite(t) or not 0 <= t <= duration + 30 or
                    not 1 <= beat <= numerator <= 32 or denominator not in (1, 2, 4, 8, 16, 32, 64) or
                    beats and t <= beats[-1][0]):
                raise EditError("Invalid saved beat grid")
            beats.append((t, beat, numerator, denominator))
        if len(beats) < 2:
            raise EditError("Missing original beat timing")
        self.synthetic_terminal_beats = 0
        if not saved_beats:
            window = beats[-9:]
            period = median(b[0] - a[0] for a, b in zip(window, window[1:]))
            if not .01 <= period <= 30:
                raise EditError("Invalid terminal beat period")
            while beats[-1][0] < duration - 1e-6:
                t, beat, numerator, denominator = beats[-1]
                beats.append((t + period, beat % numerator + 1, numerator, denominator))
                self.synthetic_terminal_beats += 1
                if self.synthetic_terminal_beats > 32 or len(beats) > MAX_BEATS:
                    raise EditError("Missing terminal beat boundary")
        times = [b[0] for b in beats]

        def beat_index(t):
            right = bisect_left(times, t)
            found = min((max(0, right - 1), min(right, len(times) - 1)), key=lambda i: abs(times[i] - t))
            if abs(times[found] - t) > .00002:
                raise EditError("Measure start does not match the saved beat grid")
            return found

        self.duration, self.sections, self.padding, self.measures = duration, [], [], []
        position = Fraction(0)
        for index, (length, row) in enumerate(zip(lengths, rows)):
            length = Fraction(length)
            try:
                before, after = (Fraction(str(row[k])) * 4 for k in ("leading_rest", "trailing_rest"))
                start, end = float(row["start"]), float(row["end"])
                if row["index"] != index or not all(math.isfinite(t) for t in (start, end)):
                    raise EditError("Invalid measure map")
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                raise EditError("Invalid measure map") from exc
            actual = length - before - after
            if not 0 < actual <= length <= 128 or before < 0 or after < 0 or not 0 <= start < end <= duration + .02:
                raise EditError("Invalid measure padding/timing")
            a = beat_index(start)
            if index + 1 < len(rows):
                b = beat_index(float(rows[index + 1]["start"]))
            else:
                intervals = actual / Fraction(4, beats[a][3])
                if intervals.denominator != 1:
                    raise EditError("Ambiguous terminal beat meter")
                b = a + int(intervals)
            if not a < b < len(beats) or abs(min(times[b], duration) - end) > .00002:
                raise EditError("Saved beat boundaries disagree with the measure map")
            step = actual / (b - a)
            if any(Fraction(4, beats[i][3]) != step for i in range(a, b)):
                raise EditError("Saved beat meter disagrees with canonical quarters")
            q0, q1 = position + before, position + length - after
            quarters = [q0 + step * i for i in range(b - a + 1)]
            self.sections.append((q0, q1, quarters, times[a:b + 1]))
            if before:
                self.padding.append((position, q0))
            if after:
                self.padding.append((q1, position + length))
            self.measures.append({**row, "scoreQuarterStart": float(position),
                                  "scoreQuarterEnd": float(position + length)})
            position += length
        self.starts = [s[0] for s in self.sections]

    def seconds(self, quarter):
        q = Fraction(quarter)
        i = max(0, bisect_right(self.starts, q) - 1)
        a, b, quarters, seconds = self.sections[i]
        if not a <= q <= b:
            raise EditError("A note lies in synthetic rest padding or outside the score")
        j = max(0, min(bisect_right(quarters, q) - 1, len(quarters) - 2))
        proportion = float((q - quarters[j]) / (quarters[j + 1] - quarters[j]))
        return seconds[j] + proportion * (seconds[j + 1] - seconds[j])

    def note_seconds(self, start, end):
        if any(max(start, a) < min(end, b) for a, b in self.padding):
            raise EditError("A note crosses synthetic rest padding")
        a, b = self.seconds(start), min(self.duration, self.seconds(end))
        if not 0 <= a < b <= self.duration:
            raise EditError("A note lies outside the original audio duration")
        return a, b


def _score_events(score):
    # Lazy heavy dependency: nothing above this line imports music21/numpy/etc.
    if __package__:
        from .accompaniment import tied_events
    else:
        from accompaniment import tied_events
    return {str(part.id): tied_events(part) for part in score.parts}


def _validate_scores(original, edited, source_inspection):
    from music21 import harmony, stream

    expected, actual = _score_events(original), _score_events(edited)
    if set(expected) != set(source_inspection["voices"]) or set(expected) != set(actual):
        raise EditError("Conversion lost or added a voice")
    for before, after in zip(original.parts, edited.parts):
        if before.id != after.id:
            raise EditError("Voice order changed")

        def layout(part):
            return [(Fraction(item.offset), Fraction(item.quarterLength),
                     len(item.notes) if item.isChord else 1, item.isRest)
                    for item in part.flatten().notesAndRests if not isinstance(item, harmony.Harmony)]

        def measures(part):
            return [(Fraction(m.offset), Fraction(m.quarterLength)) for m in part.getElementsByClass(stream.Measure)]

        if layout(before) != layout(after) or measures(before) != measures(after):
            raise EditError("Only pitch changes are supported; event offsets/durations must stay unchanged")
        intervals = lambda events: sorted((a, b) for _, a, b in events)
        if intervals(expected[before.id]) != intervals(actual[after.id]):
            raise EditError("Pitch edit changed a tied note's offset/duration; edit the entire tie chain")
    notes = [event for events in actual.values() for event in events]
    if not notes or len(notes) > MAX_NOTES:
        raise EditError("Invalid/oversize note count")
    if any(not MIN_PITCH <= pitch <= MAX_PITCH for pitch, _, _ in notes):
        raise EditError("Piano preview pitches must be MIDI 21-109")
    return actual


def _write(path, data):
    with path.open("xb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".current-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _vlq(value):
    if not 0 <= value < 1 << 28:
        raise EditError("MIDI delta exceeds limit")
    result = [value & 127]
    while value >> 7:
        value >>= 7; result.insert(0, (value & 127) | 128)
    return bytes(result)


def write_midi(playback, destination):
    """Serialize ALREADY beat-mapped seconds; never multiply quarters by Q:.

    A 20,000 ticks/second transport clock gives <=25us rounding and keeps even
    silent named voices in the SMF. This is not a fixed-tempo score rendition.
    MusicXML/ABC retain the actual notated meter/key/quarter-note durations.
    """
    ticks_per_second, division = 20000, 10000

    def track(events):
        content, previous = bytearray(), 0
        for tick, _, message in sorted(events, key=lambda row: (row[0], row[1])):
            content.extend(_vlq(tick - previous)); content.extend(message); previous = tick
        content.extend(_vlq(max(previous, round(playback["duration"] * ticks_per_second)) - previous))
        content.extend(b"\xff\x2f\x00")
        return b"MTrk" + struct.pack(">I", len(content)) + content

    tracks = [track([(0, -1, b"\xff\x51\x03\x07\xa1\x20")])]
    for channel, part in enumerate(playback["tracks"]):
        name = part["voice"].encode("ascii")
        events = [(0, -2, b"\xff\x03" + _vlq(len(name)) + name), (0, -1, bytes((0xc0 + channel, 0)))]
        for note in part["notes"]:
            a, b = (round(note[k] * ticks_per_second) for k in ("start", "end"))
            if not a < b:
                raise EditError("Note is shorter than the MIDI clock resolution")
            events.extend([(a, 1, bytes((0x90 + channel, note["pitch"], note["velocity"]))),
                           (b, 0, bytes((0x80 + channel, note["pitch"], 0)))])
        tracks.append(track(events))
    _write(destination, b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), division) + b"".join(tracks))


def _build(config, directory, stage, source, inspection, draft, number, revision_id):
    from music21 import stream
    if __package__:
        from .accompaniment import export_voice_musicxml, parse_voice_score
        from .worker import render_piano, verify_assets
    else:
        from accompaniment import export_voice_musicxml, parse_voice_score
        from worker import render_piano, verify_assets

    try:
        original = parse_voice_score(source["abc"])
    except ValueError as exc:
        raise EditError("Original canonical ABC failed exact voice/tie validation") from exc
    try:
        edited = parse_voice_score(draft["abc"])
    except ValueError as exc:
        raise EditError("Draft failed exact ABC/voice/tie validation") from exc
    events = _validate_scores(original, edited, inspection)
    previous = _json(_path(directory, "result.json"))
    playback_source = validate_playback(previous)
    lengths = [Fraction(m.quarterLength) for m in original.parts[0].getElementsByClass(stream.Measure)]
    clock = ScoreClock(lengths, playback_source["measures"],
                       _bytes(source["beatPath"], MAX_ABC_BYTES).decode("utf-8-sig"),
                       playback_source["duration"], saved_beats=source["savedBeats"])
    tracks, frozen_voices = [], []
    for voice, notes in events.items():
        role = VOICE_ROLES[voice]
        velocities = [n["velocity"] for t in playback_source["tracks"] if t["role"] == role for n in t["notes"]]
        velocity = int(median(velocities)) if velocities else (48 if role == "chords" else 100)
        result_notes = []
        for index, (pitch, start, end) in enumerate(sorted(notes, key=lambda e: (e[1], e[2], e[0]))):
            a, b = clock.note_seconds(start, end)
            result_notes.append({"id": f"{voice}-{index}", "pitch": pitch, "start": a, "end": b,
                                 "velocity": velocity, "quarterStart": float(start), "quarterEnd": float(end)})
        tracks.append({"voice": voice, "role": role, "name": voice, "editable": True, "notes": result_notes})
    # A score.abc fallback may not engrave the already recognized chord track.
    # Never silently discard it: keep non-notated roles frozen in source seconds.
    for voice, role in VOICE_ROLES.items():
        if voice in events:
            continue
        frozen = [dict(n) for t in playback_source["tracks"] if t["role"] == role for n in t["notes"]]
        if not frozen:
            continue
        for index, note in enumerate(frozen):
            if not MIN_PITCH <= note["pitch"] <= MAX_PITCH:
                raise EditError("Frozen source playback exceeds piano MIDI range 21-109")
            note.update(id=f"{voice}-frozen-{index}", quarterStart=None, quarterEnd=None)
        tracks.append({"voice": voice, "role": role, "name": voice, "editable": False, "notes": frozen})
        frozen_voices.append(voice)
    playback = {"schemaVersion": SCHEMA, "revision": number, "revisionId": revision_id,
                "duration": clock.duration, "tracks": tracks, "measures": clock.measures}
    validate_playback(playback)  # validate, but deliberately retain silent voice tracks
    _write(stage / "edited.abc", draft["abc"].encode("utf-8"))
    export_voice_musicxml(draft["abc"], stage / "edited.musicxml")
    write_midi(playback, stage / "edited.mid")
    _write(stage / "notes.json", _json_bytes(playback))
    verify_assets(config["assetsDir"])
    evidence = render_piano(playback, config["assetsDir"], stage)
    exports = sorted(EXPORTS)
    media = sorted(name for name in MEDIA if (stage / name).is_file())
    artifacts = {name: {"bytes": (stage / name).stat().st_size, "sha256": _hash_file(stage / name)}
                 for name in exports + media}
    result = {**playback, "sampleId": directory.name, "baseRevision": draft["baseRevision"],
              "sourceFingerprint": source["fingerprint"], "sourceAbc": source["name"],
              "sourceInputs": source["inputs"], "abc": draft["abc"], "voices": [t["voice"] for t in tracks],
              "notatedVoices": list(events), "frozenPlaybackVoices": frozen_voices,
              "counts": {role: sum(len(t["notes"]) for t in tracks if t["role"] == role) for role in VOICE_ROLES.values()},
              "key": previous.get("key"), "meter": previous.get("meter"), "notationBpm": previous.get("notationBpm"),
              "arrangement": {"abc": draft["abc"], "verified": True, "measures": len(lengths)} if "Accomp" in events else None,
              "audioParts": list(evidence), "audioEvidence": evidence, "exports": exports, "media": media,
              "artifacts": artifacts, "createdAt": datetime.now(timezone.utc).isoformat(),
              "provenance": {"modelExecuted": False, "recognitionReused": True, "groundTruthAvailable": False,
                             "editing": "pitch-only canonical ABC; all voices and rhythmic/tie layout preserved",
                             "playbackClock": "saved beat anchors + original measure map; piecewise quarter-to-second interpolation",
                             "midiClock": "20,000 ticks per original second (<=25 microseconds rounding)",
                             "syntheticTerminalBeats": clock.synthetic_terminal_beats,
                             "velocity": "source per-role median; no dynamics editing",
                             "piano": "FluidR3 samples by Frank Wen (CC BY 3.0 US)",
                             "warnings": ["Edited playback follows quantized canonical notation, not unquantized source MIDI.",
                                          "Final note releases are clipped to original clip duration; no rhythm/key/meter editing."] +
                                         (["Non-notated source playback roles are preserved unchanged and are not editable."] if frozen_voices else [])}}
    _write(stage / "result.json", _json_bytes(result))
    # Flush library-written XML/WAV files before the revision directory is published.
    for name in exports + media:
        # Windows _commit/fsync needs a writable descriptor (staging files only).
        with (stage / name).open("r+b") as handle:
            os.fsync(handle.fileno())
    return result


def commit_revision(config, sample_id, draft, revision_id):
    """Heavy execution boundary. Call ONLY inside the queued model CLI process."""
    draft, revision_id = validate_draft(draft), _token(revision_id)
    edited_inspection = inspect_abc(draft["abc"])
    directory = _directory(config, sample_id)
    edits = _path(directory, "edits")
    edits.mkdir(exist_ok=True)
    try:
        lock = DataLock(_path(edits, ".write.lock"))
    except OSError as exc:
        raise EditConflict("Another editor commit owns this sample") from exc
    with lock:
        source = _source(directory)
        pointer = _pointer(directory, sample_id)
        if (draft["baseRevision"] != pointer["revision"] or draft["sourceFingerprint"] != source["fingerprint"] or
                pointer["revision"] and pointer["sourceFingerprint"] != source["fingerprint"]):
            raise EditConflict("Stale baseRevision or changed original score/timing")
        final = _path(edits, "rev-" + revision_id)
        if final.exists() or any(row["revisionId"] == revision_id for row in pointer["history"]):
            raise EditConflict("Revision ID already exists; use a new transaction ID")
        inspection = inspect_abc(source["abc"])
        if inspection["skeleton"] != edited_inspection["skeleton"]:
            raise EditError("Only pitch lexemes may change; preserve rhythm, ties, voices, labels, meter and key")
        stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=edits))
        try:
            number = pointer["revision"] + 1
            result = _build(config, directory, stage, source, inspection, draft, number, revision_id)
            if (_source(directory)["fingerprint"] != source["fingerprint"] or _pointer(directory, sample_id) != pointer):
                raise EditConflict("Original score or current revision changed during rendering")
            record = {"revisionId": revision_id, "resultSha256": _hash_file(stage / "result.json")}
            next_pointer = {"schemaVersion": SCHEMA, "sampleId": sample_id, "revision": number,
                            "revisionId": revision_id, "sourceFingerprint": source["fingerprint"],
                            "history": pointer["history"] + [record]}
            os.rename(stage, final)  # ready immutable directory first; never overwrite a revision
            _atomic_json(_path(edits, "current.json"), next_pointer)  # visibility/commit point
            return {"ok": True, "sampleId": sample_id, "revision": number, "revisionId": revision_id,
                    "result": f"edits/rev-{revision_id}/result.json", "exports": result["exports"], "media": result["media"]}
        finally:
            # Only our freshly allocated staging directory, never an original or published revision.
            if stage.exists():
                checked = _path(edits, stage.name)
                if checked == stage and stage.name.startswith(".pending-"):
                    shutil.rmtree(checked)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--revision", required=True, help="Unique transaction ID; sequence is baseRevision + 1")
    parser.add_argument("--job-name", help=argparse.SUPPRESS)
    parser.add_argument("--_run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.job_name:
            join_job(args.job_name)
        config = load_config(args.config)
        draft = read_draft(config, args.sample, args.draft)
        _token(args.revision)
        if args._run:
            result = commit_revision(config, args.sample, draft, args.revision)
            print(json.dumps(result, ensure_ascii=True), flush=True)
            return 0
        # A hard wall-clock deadline also bounds third-party parser/writer bugs.
        # This Job/process group contains ONLY this invocation, not the live lab.
        command = [sys.executable, str(Path(__file__).resolve()), "--config", str(config["configPath"]),
                   "--sample", args.sample, "--draft", str(args.draft.absolute()), "--revision", args.revision, "--_run"]
        with OwnedJob() as job:
            if job.name:
                command += ["--job-name", job.name]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
            job.process = process
            try:
                stdout, stderr = process.communicate(timeout=min(600, config["jobTimeoutSeconds"]))
            except subprocess.TimeoutExpired:
                job.close()
                if process.poll() is None:
                    process.kill()
                process.communicate()
                print(json.dumps({"ok": False, "code": "timeout", "message": "Edit exceeded its execution deadline"}), flush=True)
                return 124
            if stdout:
                print(stdout, end="", flush=True)
            if stderr:
                print(stderr, end="", file=sys.stderr, flush=True)
            return process.returncode
    except Exception as exc:
        # Never expose filesystem paths, config values, or third-party tracebacks over HTTP.
        known = isinstance(exc, EditError)
        print(json.dumps({"ok": False, "code": exc.code if known else "execution_failed",
                          "message": str(exc) if known else "Edit failed; inspect the local model environment"}), flush=True)
        return 3 if isinstance(exc, EditConflict) else 2 if known else 1


if __name__ == "__main__":
    raise SystemExit(main())
