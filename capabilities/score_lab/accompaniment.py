"""Engrave the chord notes already present in SheetSage2's playback MIDI.

This is NOT a second accompaniment transcription or inferred arpeggio pattern.
The original melody ABC/MIDI remain untouched. Chord pitches/reattacks are kept;
notation alone is quantized onto the very same model beat/subbeat grid.
"""
from __future__ import annotations

from bisect import bisect_left
from fractions import Fraction
import importlib
import math
from pathlib import Path
import re
import sys

if __package__:
    from .common import child_file, read_json, sha256
else:
    from common import child_file, read_json, sha256

VOICE = "Accomp"
DURATIONS = (48, 32, 24, 16, 12, 8, 6, 4, 3, 2, 1)


def nearest_index(times, value):
    right = min(bisect_left(times, value), len(times) - 1)
    left = max(0, right - 1)
    return left if abs(times[left] - value) <= abs(times[right] - value) else right


def quantize_notes(notes, times):
    if len(times) < 2 or not all(math.isfinite(t) for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Invalid model beat grid")
    result, shifts, expanded = [], [], 0
    for index, note in enumerate(notes):
        a, b = nearest_index(times, note["start"]), nearest_index(times, note["end"])
        if b <= a:
            expanded += 1
            a = min(a, len(times) - 2)
            b = a + 1
        result.append({"id": index, "pitch": note["pitch"], "start": a, "end": b})
        shifts.extend([abs(times[a] - note["start"]), abs(times[b] - note["end"])])
    return result, {"sourceNotes": len(notes), "expandedShortNotes": expanded,
                    "maxQuantizationShiftSeconds": round(max(shifts, default=0), 6)}


def split_duration(units):
    if type(units) is not int or units <= 0:
        raise ValueError("Nonpositive notation duration")
    result = []
    for chunk in DURATIONS:
        while units >= chunk:
            result.append(chunk)
            units -= chunk
    return result


def count_bars(line):
    """Count the pinned model's canonical bars, including compressed Z rests."""
    return sum(int(m[1] or 1) if m[1] is not None else 1
               for m in re.finditer(r'"[^"\n]*"|Z(\d*)\||\|', line) if m[0].endswith("|"))


def inject_staff(original, bars):
    """Keep source systems/section boundaries, including groups shorter than 4."""
    lines = original.splitlines()
    header_end = next(i for i, line in enumerate(lines) if line.startswith("K:"))
    out = lines[:header_end] + [f'V: {VOICE} clef=bass name="和弦伴奏" snm="伴奏"', lines[header_end]]
    i, cursor = header_end + 1, 0
    while i < len(lines):
        if lines[i].strip() != "V: Ins":
            out.append(lines[i]); i += 1; continue
        end = i + 1
        while end < len(lines) and not lines[end].startswith("V:"):
            end += 1
        music = [j for j in range(i + 1, end) if "|" in lines[j] and not lines[j].startswith("%")]
        if not music:
            raise ValueError("Missing instrumental system in canonical ABC")
        last = music[-1]
        count = sum(count_bars(lines[j]) for j in music)
        if count <= 0 or cursor + count > len(bars):
            raise ValueError("Canonical ABC/beat measure count mismatch")
        out.extend(lines[i:last + 1])
        out.append("V: " + VOICE)
        out.extend(line for line in lines[i + 1:music[0]] if line.startswith(("M:", "K:")))
        out.append("".join(bar + "|" for bar in bars[cursor:cursor + count]))
        out.extend(lines[last + 1:end])
        cursor += count
        i = end
    if cursor != len(bars):
        raise ValueError("Not all accompaniment measures were engraved")
    return "\n".join(out) + "\n"


def load_notation(config):
    sheet = config["sheetRepo"]
    if not sheet.name.isidentifier():
        raise ValueError("Invalid model code package name")
    lock = read_json(Path(__file__).with_name("model.lock.json"))["repositories"]["sheetRepo"]
    for name, digest in lock.items():
        if name.endswith(".py") and sha256(child_file(sheet, name)) != digest:
            raise ValueError("Unsupported notation code build")
    sys.path.insert(0, str(sheet.parent))
    return importlib.import_module(sheet.name + ".notation_sheetsage2")


def render_measures(score, notes, notation, unit):
    grid = score.subbeat_times
    quantized, report = quantize_notes(notes, grid.tolist())
    active = [dict() for _ in range(len(grid) - 1)]
    for note in quantized:
        for t in range(note["start"], note["end"]):
            if note["pitch"] in active[t]:
                raise ValueError("Overlapping identical chord pitches need separate voices")
            active[t][note["pitch"]] = note["id"]
    bars, expected, position = [], [], 0
    mapped = {}
    for measure in score.measures:
        total = measure.abc_numerator * unit // measure.abc_denominator
        actual = measure.numerator * unit // measure.denominator
        padding = total - actual
        before = padding if measure.pad_before else 0
        after = padding - before
        parts = ["z" + (str(n) if n != 1 else "") for n in split_duration(before)] if before else []
        t, key, accidentals = measure.start_t, str(score.key_arr[measure.start_t]), {}
        while t < measure.end_t:
            end = t + 1
            while (end < measure.end_t and active[end] == active[t] and
                   score.key_arr[end] == score.key_arr[t]):
                end += 1
            if str(score.key_arr[t]) != key:
                key = str(score.key_arr[t]); accidentals = {}
                parts.append(f"[K:{key}]")
            units = notation._duration_units(score, t, end, unit)
            offset = position + before + notation._duration_units(score, measure.start_t, t, unit)
            for pitch, note_id in active[t].items():
                if note_id not in mapped:
                    mapped[note_id] = {"pitch": pitch, "startUnits": offset, "endUnits": offset + units}
                else:
                    if mapped[note_id]["endUnits"] != offset:
                        raise ValueError("Chord tie would cross synthetic rest padding")
                    mapped[note_id]["endUnits"] = offset + units
            chunks = split_duration(units)
            for index, chunk in enumerate(chunks):
                current = active[t]
                following = current if index < len(chunks) - 1 else active[end] if end < len(active) and not after else {}
                text = "z"
                if current:
                    pitches = sorted(current)
                    spelled = [notation.note_to_abc(p, notation.get_key_accidentals(key), accidentals) for p in pitches]
                    tied = [following.get(p) == current[p] for p in pitches]
                    if all(tied):
                        text = "[" + "".join(spelled) + "]"
                    else:
                        text = "[" + "".join(s + ("-" if tie else "") for s, tie in zip(spelled, tied)) + "]"
                    parts.append(text + (str(chunk) if chunk != 1 else "") + ("-" if all(tied) else ""))
                else:
                    parts.append(text + (str(chunk) if chunk != 1 else ""))
            t = end
        if after:
            parts.extend("z" + (str(n) if n != 1 else "") for n in split_duration(after))
        bars.append("".join(parts))
        position += total
    if len(mapped) != len(notes):
        raise ValueError("A source chord note is missing from the notation")
    expected = sorted((n["pitch"], Fraction(n["startUnits"] * 4, unit), Fraction(n["endUnits"] * 4, unit))
                      for n in mapped.values())
    return bars, expected, report


def tied_events(part):
    """Read individual pitches back, joining ties but never repeated attacks."""
    from music21 import harmony
    active, result = {}, []
    for item in part.flatten().notes:
        if isinstance(item, harmony.Harmony):
            continue
        start, end = Fraction(item.offset), Fraction(item.offset) + Fraction(item.quarterLength)
        for note in item.notes if item.isChord else [item]:
            pitch = int(note.pitch.midi)
            kind = note.tie.type if note.tie else None
            if kind in ("continue", "stop"):
                if pitch not in active or active[pitch][1] != start:
                    raise ValueError("Invalid or discontinuous chord tie")
                active[pitch][1] = end
                if kind == "stop":
                    result.append((pitch, *active.pop(pitch)))
            elif kind == "start":
                if pitch in active:
                    raise ValueError("Overlapping tied chord notes")
                active[pitch] = [start, end]
            else:
                if pitch in active:
                    raise ValueError("Chord reattack incorrectly tied")
                result.append((pitch, start, end))
    if active:
        raise ValueError("Unclosed chord ties")
    return sorted(result)


def single_voice_abc(abc, target):
    """music21's ABC parser does not merge repeated named voice blocks.

    Parse each canonical voice independently instead of concatenating systems
    or losing Z multimeasure rests. These are conversion/display adaptations;
    the original model ABC stays on disk unchanged.
    """
    header, active, meter, unit = True, None, (4, 4), 16
    result = []
    for line in abc.splitlines():
        if line.startswith("V:"):
            if not header:
                active = line[2:].strip().split()[0]
            continue
        if not header and active != target:
            continue
        match = re.match(r"M:(\d+)/(\d+)", line)
        if match:
            meter = tuple(map(int, match.groups()))
        match = re.match(r"L:1/(\d+)", line)
        if match:
            unit = int(match[1])
        if header:
            result.append(line)
            if line.startswith("K:"):
                header = False
            continue
        if not re.match(r"^[A-Za-z]:|^%", line):
            length = meter[0] * unit // meter[1]
            line = re.sub(r'"[^"\n]*"|\[[^\]\n]*\]|Z(\d*)\|',
                          lambda m: m[0] if m[1] is None else (f"z{length}|") * int(m[1] or 1), line)
        result.append(line)
    return "\n".join(result) + "\n"


def conversion_voice(source):
    """Adapt only the pinned model's small ABC grammar for music21.

    music21 ignores inline K: and drops body key signatures. Spell each pitch
    explicitly for conversion, then restore the actual key changes at their
    measure offsets. The displayed/exported ABC is never rewritten this way.
    Accidental propagation matches the pinned model: one letter, all octaves.
    """
    from music21 import abcFormat

    token = re.compile(r'"[^"\n]*"|\[K:([^\]\n]+)\]|\[([_=^A-Ga-g,\x27-]+)\](\d*)(-?)'
                       r"|([_=^]*[A-Ga-gz][,']*)(\d*)(-?)|\|")
    pitch_token = re.compile(r"([_=^]*)([A-Ga-g])([,']*)")
    header, unit, active_meter, bar, position = True, 8, "4/4", 0, 0
    key_defaults, accidentals, signatures, keys, lines, events = {}, {}, [], [], [], []

    def change_key(value):
        nonlocal key_defaults, accidentals
        item = abcFormat.ABCMetadata("K:" + value)
        item.preParse()
        signature = item.getKeySignatureObject()
        if signature is None:
            raise ValueError("Unsupported canonical key signature")
        key_defaults = {p.step: int(p.accidental.alter) for p in signature.alteredPitches}
        accidentals = {}
        keys.append((bar, Fraction(position * 4, unit), signature))

    def spell(match):
        marks, letter, octave = match.groups()
        name = letter.upper()
        if marks:
            value = marks.count("^") - marks.count("_")
            accidentals[name] = value
        else:
            value = accidentals.get(name, key_defaults.get(name, 0))
        return ("^" * value if value > 0 else "_" * -value if value < 0 else "=") + letter + octave

    def record_notes(text, duration, tied):
        for match in re.finditer(r"([_=^]*)([A-Ga-g])([,']*)(-?)", text):
            marks, letter, octave, note_tie = match.groups()
            pitch = (60 + {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter.upper()]
                     + (12 if letter.islower() else 0) + 12 * (octave.count("'") - octave.count(","))
                     + marks.count("^") - marks.count("_"))
            events.append((bar, Fraction(position * 4, unit), Fraction(duration * 4, unit),
                           pitch, bool(tied or note_tie)))

    for line in source.splitlines():
        if line.startswith("M:"):
            active_meter = line[2:].strip()
        if line.startswith("L:"):
            unit = int(re.fullmatch(r"L:1/(\d+)", line)[1])
        if line.startswith("K:"):
            change_key(line[2:].strip())
            if not header:
                continue
            header = False
            lines.append(line)
            continue
        if header or re.match(r"^[A-Za-z]:|^%", line):
            lines.append(line)
            continue
        cursor, out = 0, []
        for match in token.finditer(line):
            if line[cursor:match.start()].strip():
                raise ValueError("Unsupported canonical ABC conversion token")
            cursor = match.end()
            text = match[0]
            if match[1] is not None:
                change_key(match[1])
                continue
            if text == "|":
                numerator, denominator = map(int, active_meter.split("/"))
                if position != numerator * unit // denominator:
                    raise ValueError("Canonical measure duration disagrees with its meter")
                signatures.append(active_meter)
                bar, position, accidentals = bar + 1, 0, {}
            elif match[2] is not None:
                inside, duration = pitch_token.sub(spell, match[2]), int(match[3] or 1)
                record_notes(inside, duration, match[4])
                text = "[" + inside + "]" + match[3] + match[4]
                position += duration
            elif match[5] is not None:
                if match[5] == "z" and match[7]:
                    raise ValueError("A canonical rest cannot be tied")
                inside, duration = pitch_token.sub(spell, match[5]), int(match[6] or 1)
                record_notes(inside, duration, match[7])
                text = inside + match[6] + match[7]
                position += duration
            out.append(text)
        if line[cursor:].strip():
            raise ValueError("Unsupported canonical ABC conversion tail")
        lines.append("".join(out))
    if position:
        raise ValueError("Unterminated canonical ABC measure")
    return "\n".join(lines) + "\n", signatures, keys, events


def restore_ties(measures, events):
    """music21 ignores partial chord ties such as [C-E]4; restore per pitch."""
    from music21 import harmony, tie

    observed, active = {}, {}
    for index, measure in enumerate(measures):
        for item in measure.notes:
            if isinstance(item, harmony.Harmony):
                continue
            for note in item.notes if item.isChord else [item]:
                identity = (index, Fraction(item.offset), Fraction(item.quarterLength), int(note.pitch.midi))
                if identity in observed:
                    raise ValueError("Duplicate canonical ABC chord pitch")
                observed[identity] = note
    if set(observed) != {event[:4] for event in events} or len(observed) != len(events):
        raise ValueError("ABC conversion changed individual note pitches or durations")
    for index, offset, duration, pitch, outgoing in sorted(events):
        start = Fraction(measures[index].offset) + offset
        incoming = active.pop(pitch, None)
        if incoming is not None and incoming != start:
            raise ValueError("Discontinuous canonical ABC tie")
        kind = ("continue" if outgoing else "stop") if incoming is not None else "start" if outgoing else None
        observed[(index, offset, duration, pitch)].tie = tie.Tie(kind) if kind else None
        if outgoing:
            active[pitch] = start + duration
    if active:
        raise ValueError("Unclosed canonical ABC tie")


def parse_voice_score(abc):
    from music21 import clef, converter, key, metadata, meter, stream

    names = {"Vocal": ("人声旋律", "人声"), "Ins": ("器乐旋律", "器乐"), VOICE: ("和弦伴奏", "伴奏")}
    result = stream.Score()
    result.metadata = metadata.Metadata(title="", composer="")
    for voice in names:
        if not re.search(r"^V:\s*" + voice + r"\b", abc, re.M):
            continue
        source = single_voice_abc(abc, voice)
        converted, signatures, keys, events = conversion_voice(source)
        parsed = converter.parse(converted, format="abc")
        if len(parsed.parts) != 1:
            raise ValueError("Ambiguous single-voice ABC conversion")
        part = parsed.parts[0]
        part.id, (part.partName, part.partAbbreviation) = voice, names[voice]
        # music21 also ignores body M: changes. Without restoring canonical
        # meters its MusicXML writer silently pads short bars to the old 4/4.
        measures = list(part.getElementsByClass(stream.Measure))
        if len(measures) != len(signatures):
            raise ValueError("Single-voice conversion lost a measure")
        previous = None
        for measure, signature in zip(measures, signatures):
            for old in list(measure.getElementsByClass((meter.TimeSignature, key.KeySignature))):
                measure.remove(old)
            if signature != previous:
                measure.insert(0, meter.TimeSignature(signature))
            previous = signature
        restore_ties(measures, events)
        for (index, offset), signature in {(i, offset): sig for i, offset, sig in keys}.items():
            if index < len(measures):
                measures[index].insert(offset, signature)
        if voice == VOICE:
            for old in list(part.recurse().getElementsByClass(clef.Clef)):
                old.activeSite.remove(old)
            first = next(iter(part.getElementsByClass(stream.Measure)), part)
            first.insert(0, clef.BassClef())
        result.insert(0, part)
    if not result.parts or len({Fraction(p.highestTime) for p in result.parts}) != 1:
        raise ValueError("Score voices have different measure timelines")
    return result


def export_voice_musicxml(abc, destination):
    from music21 import converter

    parsed = parse_voice_score(abc)
    expected = [tied_events(part) for part in parsed.parts]
    pending = destination.with_name(destination.stem + ".pending.musicxml")
    parsed.write("musicxml", fp=str(pending))
    reread = converter.parse(str(pending), format="musicxml")
    if [tied_events(part) for part in reread.parts] != expected:
        raise ValueError("MusicXML changed voice pitches, timing or ties")
    pending.replace(destination)
    return parsed


def build_arrangement(config, output, playback, original):
    import numpy as np
    import pretty_midi

    notes = [n for t in playback["tracks"] if t["role"] == "chords" for n in t["notes"]]
    if not notes or not original:
        return None
    notation = load_notation(config)
    # beat.lab excludes the final synthetic boundary used by canonical ABC.
    # Prefer the model's exact saved notation grid, not an incomplete raw LAB.
    beat_path, key_path = output / "notation/song_beats.txt", output / "notation/song_keys.txt"
    if beat_path.is_file():
        beats = [line.split() for line in beat_path.read_text().splitlines() if line.strip()]
    else:
        beats = [line.split() for line in (output / "beat.lab").read_text().splitlines() if line.strip()]
        beats = [[float(a), int(b), int(c), int(d)] for a, b, c, d in beats]
        period = float(np.median(np.diff([b[0] for b in beats[-9:]])))
        if not period > 0:
            raise ValueError("Invalid terminal beat period")
        while beats[-1][0] < playback["duration"] - 1e-6:
            previous = beats[-1]
            beats.append([previous[0] + period, previous[1] % previous[2] + 1, previous[2], previous[3]])
    keys = notation.read_keys(key_path if key_path.is_file() else output / "key.lab")
    score = notation.build_rebuilt_abc_score_from_data(
        pretty_midi.PrettyMIDI(), beats, [], keys, [], meter_conflict="infer", melody_only=True)
    if len(score.measures) != len(playback["measures"]):
        raise ValueError("Canonical notation grid disagrees with the playback measure map")
    for measure, row in zip(score.measures, playback["measures"]):
        if abs(float(score.beats[measure.start_beat].time) - row["start"]) > .00001:
            raise ValueError("Canonical notation measure starts differ from playback")
    unit = int(re.search(r"^L:1/(\d+)$", original, re.M)[1])
    bars, expected, report = render_measures(score, notes, notation, unit)
    abc = inject_staff(original, bars)
    parsed = parse_voice_score(abc)
    candidates = [part for part in parsed.parts if part.id == VOICE and tied_events(part) == expected]
    if len(candidates) != 1:
        raise ValueError("Accompaniment ABC readback does not match the source chord pitches/attacks")
    melody = parse_voice_score(original)
    for part in melody.parts:
        other = next(p for p in parsed.parts if p.id == part.id)
        if tied_events(other) != tied_events(part):
            raise ValueError("Adding accompaniment changed a melody voice")
    export_voice_musicxml(abc, output / "arrangement.musicxml")
    (output / "arrangement.abc").write_text(abc, encoding="utf-8")
    return {"abc": abc, "measures": len(bars), **report, "verified": True,
            "source": "existing model MIDI chord voicing; not a full accompaniment transcription",
            "timing": "model beat-grid notation; original-time audio and MIDI unchanged"}
