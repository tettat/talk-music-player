"""Offline desktop-lyric UI fixture: synthetic metadata, synthetic silent WAV, no accounts/network providers."""
from pathlib import Path
import json
import sys
import threading
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from server import Player, Server, Handler
from library import PlayerError
from providers import ProviderRegistry
from process_lifetime import own_process_tree

own_process_tree()
private = Path(sys.argv[1]).resolve()
private.mkdir(parents=True, exist_ok=True)
tone = private / 'synthetic-silence.wav'
with wave.open(str(tone), 'wb') as out:
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(8000)
    out.writeframes(b'\0\0' * 8000 * 60)

tracks = [
    {'source': 'qq', 'mid': 'SynthQQTrack001', 'songId': 987654321, 'title': '窗边的风', 'artist': 'Usic', 'album': '合成测试', 'duration': 60},
    {'source': 'netease', 'songId': 987654322, 'title': '山间来信', 'artist': 'Usic', 'album': '合成测试', 'duration': 60, 'availability': 'free'},
    {'source': 'qq', 'mid': 'SynthQQTrack002', 'songId': 987654323, 'title': '无歌词测试', 'artist': 'Usic', 'album': '合成测试', 'duration': 60},
    {'source': 'netease', 'songId': 987654324, 'title': '静态歌词测试', 'artist': 'Usic', 'album': '合成测试', 'duration': 60, 'availability': 'free'},
]

class FixtureProvider:
    qr = None
    auth_cancel = False
    capabilities = ['search', 'play', 'lyrics']

    def __init__(self, source):
        self.id, self.name = source, source

    def public(self):
        return {'session': 'required', 'auth': 'idle', 'qrReady': False}

    def run(self, operation, **args):
        if operation == 'source':
            return {'track': args['track'], 'path': tone, 'media': {}}
        if operation == 'lyrics':
            track = args['track']
            if track['songId'] == 987654323:
                raw = ''
            elif track['songId'] == 987654324:
                raw = '静态歌词甲\n静态歌词乙'
            elif self.id == 'qq':
                raw = '[00:00]风从窗边轻轻经过\n[00:03]把遥远的山色带给我\n[00:08]一朵小花正在等风\n[00:13]于是黄昏也变得温柔'
            else:
                raw = '[00:00]收到了山间的来信\n[00:03]每一页都是轻轻的风\n[00:08]抬头又看见一片晴空\n[00:13]把今天留在花影里'
            return {'lyric': raw, 'translation': ''}
        raise PlayerError('LOGIN_REQUIRED', 401)

class FixtureHandler(Handler):
    def headers_out(self, status, kind, length, extra=None):
        # Production uses MP3; the isolated fixture explicitly serves a generated WAV.
        if kind == 'audio/mpeg':
            kind = 'audio/wav'
        return super().headers_out(status, kind, length, extra)

app = Player(private, live=False)
app.bridge = FixtureProvider('qq')
app.providers = ProviderRegistry([app.bridge, FixtureProvider('netease')])
cleaned = app.library.put(tracks, used=True)
app.library.save_state({'queue': [t['key'] for t in cleaned], 'current': cleaned[0]['key'], 'position': 0})
server = Server(('127.0.0.1', 0), app)
server.RequestHandlerClass = FixtureHandler

def owner():
    sys.stdin.buffer.readline(128)
    server.shutdown()

threading.Thread(target=owner, daemon=True).start()
print(json.dumps({'port': server.server_port, 'url': f'http://127.0.0.1:{server.server_port}/', 'pid': __import__('os').getpid(), 'synthetic': True}), flush=True)
try:
    server.serve_forever(poll_interval=.1)
finally:
    server.server_close()
    app.library.close()
