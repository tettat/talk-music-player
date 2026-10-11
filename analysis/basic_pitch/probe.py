"""Local CPU music-note model. No network, credentials, or arbitrary model loading."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMBA_NUM_THREADS'):
    os.environ[name] = '2'
logging.disable(logging.WARNING)  # Upstream optional TF/CoreML absence is expected in this ONNX-only bundle.
sys.path.insert(0, str(HERE / 'vendor'))


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def run(input_path, output_path, start, seconds):
    import numpy as np
    import soundfile as sf
    import librosa
    import onnxruntime as ort
    from basic_pitch.inference import Model, run_inference
    from basic_pitch.note_creation import model_output_to_notes
    from basic_pitch.constants import AUDIO_SAMPLE_RATE, FFT_HOP

    source = Path(input_path).resolve(strict=True)
    output_path = Path(output_path).resolve()
    if source.suffix.lower() != '.mp3' or source.stat().st_size > 24 * 1024**2:
        raise ValueError('INPUT_INVALID')
    if not math.isfinite(start) or not 0 <= start < 1800 or not math.isfinite(seconds) or not .5 <= seconds <= 60:
        raise ValueError('CLIP_INVALID')
    lock = json.loads((HERE / 'sources.lock.json').read_text(encoding='utf-8'))
    for name, expected in lock['sources'].items():
        if digest(HERE / 'vendor/basic_pitch' / name) != expected:
            raise ValueError('MODEL_SOURCE_MISMATCH')
    model_path = Path(os.environ.get('TALK_BASIC_PITCH_MODEL', ROOT / '.private/models/basic_pitch/nmp.onnx'))
    if digest(model_path) != lock['model']['sha256']:
        raise ValueError('MODEL_HASH_MISMATCH')
    started = time.monotonic()
    info = sf.info(str(source))
    if info.samplerate not in (32000, 44100, 48000) or info.channels not in (1, 2):
        raise ValueError('INPUT_INVALID')
    y, sr = sf.read(str(source), start=int(start * info.samplerate), frames=int(seconds * info.samplerate),
                    dtype='float32', always_2d=True)
    if len(y) < .5 * sr or not np.isfinite(y).all():
        raise ValueError('CLIP_INVALID')
    actual_seconds = len(y) / sr
    mono = np.mean(y, axis=1)
    mono = librosa.resample(mono, orig_sr=sr, target_sr=AUDIO_SAMPLE_RATE, res_type='soxr_hq')
    if np.max(np.abs(mono)) < 1e-7:
        raise ValueError('CLIP_SILENT')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2; options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    model = Model.__new__(Model)
    model.model_type = Model.MODEL_TYPES.ONNX
    model.model = ort.InferenceSession(str(model_path), sess_options=options, providers=['CPUExecutionProvider'])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='clip-', dir=output_path.parent) as temp:
        clip = Path(temp) / 'clip.wav'
        sf.write(str(clip), mono, AUDIO_SAMPLE_RATE, subtype='FLOAT')
        matrices = run_inference(clip, model)
        _, events = model_output_to_notes(matrices, onset_thresh=.5, frame_thresh=.3,
                        min_note_len=int(round(.1277 * AUDIO_SAMPLE_RATE / FFT_HOP)), include_pitch_bends=False)
    notes = []
    for begin, end, midi, amplitude, _ in events:
        begin, end = max(0., float(begin)), min(actual_seconds, float(end))
        if begin < actual_seconds and end > begin:
            notes.append({'start': round(start + begin, 4), 'end': round(start + end, 4),
                          'midi': int(midi), 'confidence': round(min(1., max(0., float(amplitude))), 5)})
    notes.sort(key=lambda n: (n['start'], n['midi']))
    if len(notes) > 2000:
        notes = notes[:2000]
    tempo, _ = librosa.beat.beat_track(y=mono, sr=AUDIO_SAMPLE_RATE)
    bpm = float(np.asarray(tempo).reshape(-1)[0])
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=mono, sr=AUDIO_SAMPLE_RATE)))
    rms = float(np.sqrt(np.mean(mono.astype(np.float64)**2)))
    result = {'schemaVersion': 1, 'model': {**lock['model'], 'id': 'spotify/basic-pitch',
                  'version': '0.4.0', 'license': 'Apache-2.0'},
              'audio': {'sha256': digest(source), 'start': start, 'seconds': actual_seconds,
                        'sampleRate': AUDIO_SAMPLE_RATE},
              'notes': notes,
              'summary': {'noteCount': len(notes), 'pitchLow': min((n['midi'] for n in notes), default=None),
                          'pitchHigh': max((n['midi'] for n in notes), default=None),
                          'bpmEstimate': round(bpm, 2), 'spectralCentroidHz': round(centroid, 1),
                          'rmsDb': round(20 * math.log10(max(rms, 1e-12)), 2)},
              'evidence': {'matrixShapes': {k: list(v.shape) for k, v in matrices.items()},
                           'runtime': 'onnxruntime-1.20.1/CPU', 'elapsedSeconds': round(time.monotonic()-started, 3),
                           'thresholds': {'onset': .5, 'frame': .3, 'minimumNoteMs': 127.7}}}
    temporary = output_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.replace(temporary, output_path)
    # Summary only; no private input path, whole audio or lyrics on stdout.
    print(json.dumps({'ok': True, 'model': result['model']['id'], 'audio': result['audio'],
                      'summary': result['summary'], 'evidence': result['evidence']}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True); parser.add_argument('--output', required=True)
    parser.add_argument('--start', type=float, default=30); parser.add_argument('--seconds', type=float, default=30)
    args = parser.parse_args()
    try:
        run(args.input, args.output, args.start, args.seconds)
    except Exception:
        print('{"ok":false,"code":"MODEL_RUN_FAILED"}')
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
