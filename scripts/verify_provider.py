"""Explicit live, read-only account/search/lyric smoke; output metadata counts only."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from settings import SETTINGS
from bridge import Bridge


def main():
    bridge = Bridge(SETTINGS.data_dir / 'qa')
    account = bridge.run('status')
    result = bridge.run('search', query='周杰伦 晴天', page=1)
    chosen = next((track for track in result['tracks']
                   if track['title'] == '晴天' and '周杰伦' in track['artist']), None)
    if chosen is None: raise ValueError('EXPECTED_RECORDING_NOT_FOUND')
    lyrics = bridge.run('lyrics', track=chosen)
    summary = {'session': account['session'], 'searchResultCount': len(result['tracks']),
               'exactRecordingFound': True, 'lyricCharacters': len(lyrics['lyric']),
               'credentialExported': False, 'userLibraryModified': False}
    output = ROOT / 'output'
    output.mkdir(exist_ok=True)
    (output / 'provider-smoke.json').write_text(json.dumps(summary), encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'code': getattr(error, 'code', 'PROVIDER_SMOKE_FAILED')}))
        raise SystemExit(1)
