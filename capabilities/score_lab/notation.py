"""A bounded, immutable notation snapshot for printing; no model imports."""
from __future__ import annotations

import re

if __package__:
    from .common import public_sample, read_json, sample_dir
else:
    from common import public_sample, read_json, sample_dir

MAX_ABC_BYTES = 512 * 1024


def notation_snapshot(config, sid, revision=None):
    if revision is not None and (not isinstance(revision, str) or
                                not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', revision)):
        raise ValueError('Invalid score revision')
    directory = sample_dir(config, sid)
    meta = public_sample(directory)
    if meta['status']['state'] != 'completed':
        raise ValueError('Score is not complete')
    if revision is None:
        result = read_json(directory / 'result.json')
        arrangement = result.get('arrangement') or {}
        abc = arrangement.get('abc') if arrangement.get('verified') else result.get('abc')
    else:
        if __package__:
            from .editing import read_revision_result
        else:
            from editing import read_revision_result
        result = read_revision_result(config, sid, revision)
        abc = result.get('abc')
    if not isinstance(abc, str) or not abc.strip() or len(abc.encode('utf-8')) > MAX_ABC_BYTES:
        raise ValueError('Printable notation is unavailable')
    composer = re.search(r'^C:\s*([^\r\n]+)', abc, re.M)
    return {'id': sid, 'revisionId': revision,
            'title': str(meta.get('title') or '乐谱')[:256],
            'artist': str(meta.get('artist') or (composer.group(1) if composer else ''))[:256], 'abc': abc}
