"""Pinned QQ adapter integration. Signed URLs and sessions never cross into the UI."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from library import PlayerError, clean_track, valid_mid


def load_adapter():
    from providers.qq import adapter
    problem = adapter.guard()
    if problem:
        raise PlayerError('RUNTIME_UNAVAILABLE', 503)
    adapter.isolate()
    return adapter


def metadata(track):
    return clean_track({'mid': track.mid, 'songId': track.id,
                        'title': track.title or track.name,
                        'artist': ' / '.join(s.name for s in track.singer),
                        'album': track.album.title or track.album.name,
                        'albumMid': track.album.mid, 'duration': track.interval})


class Bridge:
    id = 'qq'
    name = 'QQ Music'

    def __init__(self, private):
        self.private = Path(private)
        self.cache = self.private / 'audio'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.adapter = load_adapter()
        self.session = 'saved' if self.adapter.private_path('credential.dpapi').exists() else 'required'
        self.verified_at = None
        self.qr = None
        self.auth_status = 'idle'
        self.auth_provider = 'qq'
        self.auth_cancel = False
        self.generation = 0

    def public(self):
        return {'session': self.session, 'verifiedAt': self.verified_at,
                'auth': self.auth_status, 'provider': self.auth_provider,
                'qrReady': self.qr is not None, 'generation': self.generation}

    async def client_action(self, operation, **args):
        from qqmusic_api import Client, Credential, Platform
        from qqmusic_api.core.exceptions import CredentialRefreshError
        from qqmusic_api.modules.song import SongFileInfo, SongFileType

        path = self.adapter.private_path('credential.dpapi')
        if not path.exists():
            self.session = 'required'
            raise PlayerError('LOGIN_REQUIRED', 401)
        try:
            credential = Credential.model_validate_json(self.adapter.local_module('windows_secret').load(path))
        except Exception:
            self.session = 'required'
            raise PlayerError('LOGIN_REQUIRED', 401) from None
        if not credential.is_valid():
            self.session = 'required'
            raise PlayerError('LOGIN_REQUIRED', 401)
        transport = self.adapter.local_module('cookie_transport').build_transport(
            connect_retries=0, timeout=(10, 15), max_concurrency=1)
        async with Client(credential=credential, platform=Platform.ANDROID,
                          device_path=str(self.adapter.private_path('device.json')),
                          transport=transport) as client:
            if await client.login.check_expired(credential):
                try:
                    replacement = await client.login.refresh_credential(credential)
                except CredentialRefreshError:
                    self.session = 'required'
                    raise PlayerError('LOGIN_REQUIRED', 401) from None
                if not replacement.is_valid() or await client.login.check_expired(replacement):
                    self.session = 'required'
                    raise PlayerError('LOGIN_REQUIRED', 401)
                credential = replacement
                client.credential = credential
                self.adapter.save_credential(credential)
            self.session, self.verified_at = 'valid', time.time()
            if operation == 'status':
                return self.public()
            if operation == 'search':
                response = await client.search.search_by_type(args['query'], num=30,
                                                              page=args.get('page', 1), highlight=False)
                tracks = []
                seen = set()
                for row in response.song:
                    try:
                        track = metadata(row)
                    except PlayerError:
                        continue
                    if track['key'] not in seen:
                        tracks.append(track)
                        seen.add(track['key'])
                return {'tracks': tracks, 'hasMore': response.nextpage != -1}
            if operation == 'lyrics':
                selected = args['track']
                response = await client.lyric.get_lyric(selected['songId'], qrc=False, trans=True)
                if response.songid != selected['songId']:
                    raise PlayerError('TRACK_IDENTITY_MISMATCH', 502)
                lyric, translation = response.lyric or '', response.trans or ''
                if any(not isinstance(value, str) or len(value.encode('utf-8')) > 256 * 1024
                       for value in (lyric, translation)):
                    raise PlayerError('LYRICS_INVALID', 502)
                return {'lyric': lyric, 'translation': translation, 'songId': response.songid}
            if operation != 'source':
                raise PlayerError('INVALID_INPUT')
            selected = args['track']
            detail = await client.song.get_detail(selected['songId'])
            actual = metadata(detail.track)
            # Resolve the selected exact recording, not a fuzzy title or an untrusted browser payload.
            if (actual['songId'], actual['mid'], actual['duration']) != (
                    selected['songId'], selected['mid'], selected['duration']):
                raise PlayerError('TRACK_IDENTITY_MISMATCH', 502)
            media_mid = detail.track.file.media_mid
            if not valid_mid(media_mid):
                raise PlayerError('SOURCE_UNAVAILABLE', 422)
            response = await client.song.get_song_urls([
                SongFileInfo(actual['mid'], media_mid=media_mid)],
                file_type=SongFileType.MP3_128, credential=credential)
            if len(response.data) != 1:
                raise PlayerError('SOURCE_UNAVAILABLE', 422)
            helper = self.adapter.local_module('member_source')
            target = {'songMID': actual['mid'], 'mediaMID': media_mid}
            try:
                url = helper.authorized_url(response.data[0], target)
            except ValueError:
                raise PlayerError('SOURCE_UNAVAILABLE', 422) from None
            # Per-file provider authorization is obtained even when reusing a verified local cache.
            cache_key = hashlib.sha256((actual['mid'] + ':' + media_mid).encode()).hexdigest()
            destination = self.cache / (cache_key + '.mp3')
            manifest = self.cache / (cache_key + '.json')
            media = None
            if (destination.is_file() and manifest.is_file() and
                    destination.stat().st_size <= helper.MAX_BYTES and manifest.stat().st_size <= 8192):
                try:
                    saved = json.loads(manifest.read_text(encoding='utf-8'))
                    with destination.open('rb') as handle:
                        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                    if (saved['sha256'] == digest and saved.get('decoder') == 'miniaudio-1.71' and
                            saved.get('decoderVerified') is True and
                            saved.get('nonSilentPcmObserved') is True and
                            saved.get('completeMp3FrameCoverage') is True and
                            abs(saved.get('decodedDurationSeconds', 0) - actual['duration']) <= 2):
                        helper.inspect_mp3(destination.read_bytes(), actual['duration'])
                        media = saved
                except Exception:
                    media = None
            if media is None:
                try:
                    media = await asyncio.to_thread(helper.fetch_mp3, url, destination, actual['duration'])
                except ValueError:
                    raise PlayerError('SOURCE_UNAVAILABLE', 422) from None
                temporary = manifest.with_suffix('.tmp')
                temporary.write_text(json.dumps(media), encoding='utf-8')
                os.replace(temporary, manifest)
            destination.touch()
            self.trim_cache(destination)
            return {'track': actual, 'path': destination, 'media': media}

    def trim_cache(self, keep):
        files = sorted(self.cache.glob('*.mp3'), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for index, path in enumerate(files):
            total += path.stat().st_size
            if path != keep and (index >= 12 or total > 150 * 1024**2):
                path.unlink(missing_ok=True)
                path.with_suffix('.json').unlink(missing_ok=True)

    async def login(self, provider):
        from qqmusic_api import Client, Credential, Platform
        from qqmusic_api.models.login import QRLoginType, QRCodeLoginEvents
        from qqmusic_api.modules.login_utils import QRCodeLoginSession

        self.auth_provider = provider
        self.auth_status, self.auth_cancel, self.qr = 'creating', False, None
        self.generation += 1
        transport = self.adapter.local_module('cookie_transport').build_transport(
            connect_retries=0, timeout=(10, 15), max_concurrency=1)
        try:
            async with Client(credential=Credential(), platform=Platform.WEB,
                              device_path=str(self.adapter.private_path('device.json')),
                              transport=transport) as client:
                session = QRCodeLoginSession(client.login, QRLoginType(provider), interval=2, timeout_seconds=90)
                qr = await session.get_qrcode()
                if self.auth_cancel:
                    return
                self.qr, self.auth_status = bytes(qr.data), 'scan'
                async for event in session:
                    if self.auth_cancel:
                        return
                    if event.event == QRCodeLoginEvents.SCAN:
                        self.auth_status = 'confirm'
                    elif event.event == QRCodeLoginEvents.CONF:
                        self.auth_status = 'confirm'
                    elif event.event == QRCodeLoginEvents.DONE:
                        if event.credential is None or not event.credential.is_valid():
                            raise PlayerError('LOGIN_FAILED', 502)
                        if await client.login.check_expired(event.credential):
                            raise PlayerError('LOGIN_FAILED', 502)
                        self.adapter.save_credential(event.credential)
                        self.session, self.verified_at, self.auth_status = 'valid', time.time(), 'done'
                        return
                    elif event.event in (QRCodeLoginEvents.TIMEOUT, QRCodeLoginEvents.REFUSE):
                        self.auth_status = 'expired' if event.event == QRCodeLoginEvents.TIMEOUT else 'refused'
                        return
                self.auth_status = 'expired'
        finally:
            self.qr = None
            if self.auth_cancel:
                self.auth_status = 'idle'

    def run(self, operation, **args):
        try:
            return asyncio.run(asyncio.wait_for(self.client_action(operation, **args),
                                               timeout=110 if operation == 'source' else 50))
        except PlayerError:
            raise
        except TimeoutError:
            raise PlayerError('NETWORK_TIMEOUT', 504) from None
        except Exception:
            raise PlayerError('PROVIDER_UNAVAILABLE', 502) from None
