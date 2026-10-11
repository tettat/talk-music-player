"""Explicit live QA probe; lyric text stays in the private QA directory, never stdout."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bridge import Bridge


def main():
    private = ROOT / '.private/qa'
    bridge = Bridge(private)
    track = {'mid': '0039MnYb0qxYhV', 'key': '0039MnYb0qxYhV', 'songId': 97773, 'duration': 269}
    result = bridge.run('lyrics', track=track)
    private.mkdir(parents=True, exist_ok=True)
    (private / 'lyric-probe.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'songId': result['songId'], 'characters': len(result['lyric']),
                      'timeTags': len(re.findall(r'\[\d+:\d+', result['lyric'])),
                      'sha256': hashlib.sha256(result['lyric'].encode()).hexdigest(),
                      'translationCharacters': len(result['translation']),
                      'session': bridge.public()['session']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'code': getattr(error, 'code', 'LYRIC_PROBE_FAILED')}))
        raise SystemExit(1)
