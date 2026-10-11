"""Validate and fetch one account-authorized ordinary MP3; no DRM conversion."""
from __future__ import annotations

import hashlib
import importlib.metadata
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CDN = 'https://isure.stream.qqmusic.qq.com/'
MAX_BYTES = 24 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    """Do not leak a signed provider URL through arbitrary redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('SOURCE_REDIRECT_REFUSED')


def authorized_url(item, target):
    """Accept only the requested clear MP3 and keep all signed URL data in memory."""
    expected = 'M500' + target['mediaMID'] + '.mp3'
    if item.mid != target['songMID'] or item.filename != expected:
        raise ValueError('SOURCE_IDENTITY_MISMATCH')
    if item.result != 0 or not item.purl or not item.vkey:
        raise ValueError('AUTHORIZED_SOURCE_UNAVAILABLE')
    if item.ekey:
        raise ValueError('PROTECTED_SOURCE_NOT_SUPPORTED')
    parts = urlsplit(item.purl)
    if parts.scheme or parts.netloc or parts.fragment or parts.path != expected:
        raise ValueError('SOURCE_PATH_REJECTED')
    if any(ord(c) < 32 or ord(c) == 127 for c in item.purl):
        raise ValueError('SOURCE_PATH_REJECTED')
    return CDN + item.purl


def inspect_mp3(data, expected_seconds):
    """Scan all MPEG Layer III frames; reject previews, malformed and protected bytes."""
    if not data or len(data) > MAX_BYTES:
        raise ValueError('AUDIO_SIZE_REJECTED')
    offset = 0
    if data.startswith(b'ID3'):
        if len(data) < 10 or any(x & 128 for x in data[6:10]):
            raise ValueError('INVALID_ID3_HEADER')
        tag_size = sum(data[6 + i] << (21 - i * 7) for i in range(4))
        offset = 10 + tag_size + (10 if data[5] & 16 else 0)
        if offset >= len(data):
            raise ValueError('INVALID_ID3_LENGTH')
    frame_count = 0
    seconds = 0.0
    sample_rate = None
    channels = None
    end = len(data) - 128 if len(data) >= 128 and data[-128:-125] == b'TAG' else len(data)
    while offset < end:
        remaining = end - offset
        if remaining <= 4096 and not any(data[offset:end]):
            break
        if remaining < 4:
            raise ValueError('TRUNCATED_MP3_HEADER')
        header = int.from_bytes(data[offset:offset + 4], 'big')
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, frequency = (header >> 12) & 15, (header >> 10) & 3
        if header >> 21 != 2047 or version == 1 or layer != 1 or bitrate_index in (0, 15) or frequency == 3:
            raise ValueError('ORDINARY_MP3_REQUIRED')
        rates = (44100, 48000, 32000)
        rate = rates[frequency] // (1 if version == 3 else 2 if version == 2 else 4)
        bitrates = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                    if version == 3 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
        samples = 1152 if version == 3 else 576
        length = (144000 if version == 3 else 72000) * bitrates[bitrate_index] // rate + ((header >> 9) & 1)
        channel_count = 1 if (header >> 6) & 3 == 3 else 2
        if offset + length > end:
            raise ValueError('TRUNCATED_MP3_FRAME')
        if sample_rate is not None and (sample_rate != rate or channels != channel_count):
            raise ValueError('INCONSISTENT_MP3_STREAM')
        sample_rate, channels = rate, channel_count
        frame_count += 1
        if frame_count > 50000:
            raise ValueError('MP3_FRAME_LIMIT')
        seconds += samples / rate
        offset += length
    if frame_count == 0 or abs(seconds - expected_seconds) > 2:
        raise ValueError('FULL_TRACK_DURATION_REQUIRED')
    return {'format': 'mp3', 'sampleRate': sample_rate, 'channels': channels,
            'frameCount': frame_count, 'durationSeconds': round(seconds, 3),
            'completeMp3FrameCoverage': True, 'decoderVerified': False,
            'soundOutputVerified': False, 'completeMemberPlaybackVerified': False}


def decoded_metadata(decoded, expected_seconds):
    """Require full-length non-silent PCM, separate from physical sound output."""
    if abs(decoded.duration - expected_seconds) > 2 or not any(decoded.samples):
        raise ValueError('FULL_NON_SILENT_DECODE_REQUIRED')
    return {'decoderVerified': True, 'decoder': 'miniaudio-1.71',
            'decodedPcmFrames': decoded.num_frames,
            'decodedDurationSeconds': round(decoded.duration, 3), 'nonSilentPcmObserved': True}


def fetch_mp3(url, destination, expected_seconds):
    """Fetch with TLS verification, bounded bytes/time and atomic validated cache output."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != 'isure.stream.qqmusic.qq.com' or parts.port:
        raise ValueError('SOURCE_HOST_REJECTED')
    destination = Path(destination)
    fd, temporary = tempfile.mkstemp(prefix='member-source-', suffix='.part', dir=str(destination.parent))
    started = time.monotonic()
    count = 0
    try:
        with os.fdopen(fd, 'wb') as output:
            with build_opener(NoRedirect()).open(Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                                       'Accept-Encoding': 'identity'}), timeout=15) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError('SOURCE_HTTP_RESPONSE_REJECTED')
                declared = response.headers.get('Content-Length')
                if declared is not None and (not declared.isdecimal() or int(declared) > MAX_BYTES):
                    raise ValueError('AUDIO_SIZE_REJECTED')
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_BYTES or time.monotonic() - started > 80:
                        raise ValueError('SOURCE_FETCH_LIMIT')
                    output.write(chunk)
                if declared is not None and count != int(declared):
                    raise ValueError('SOURCE_LENGTH_MISMATCH')
        data = Path(temporary).read_bytes()
        metadata = inspect_mp3(data, expected_seconds)
        if importlib.metadata.version('miniaudio') != '1.71':
            raise ValueError('DECODER_VERSION_MISMATCH')
        import miniaudio
        # Memory decode also avoids native Windows fopen Unicode-path limitations.
        decoded = miniaudio.decode(data, sample_rate=44100, nchannels=2)
        metadata.update(decoded_metadata(decoded, expected_seconds))
        os.replace(temporary, destination)
        return {**metadata, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                'cacheStoredPrivately': True, 'signedUrlExported': False}
    finally:
        Path(temporary).unlink(missing_ok=True)
