"""Bounded decoded LRC/QRC normalization; never decrypts music or executes markup."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from library import PlayerError

STAMP = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]')
QRC = re.compile(r'^\[(\d{1,9}),(\d{1,9})\]')
WORD = re.compile(r'\(\d{1,9},\d{1,9}\)')
META = re.compile(r'^\[([a-zA-Z]{1,12}):([^\]\r\n]{0,500})\]\s*$')
EMPTY = {'暂无歌词', '暂无歌词，请欣赏', '纯音乐，请欣赏', '此歌曲为没有填词的纯音乐，请您欣赏'}
META_KEYS = {'ar': 'artist', 'ti': 'title', 'al': 'album', 'by': 'contributor', 'length': 'length'}


def normalize_lyrics(raw, duration):
    if (not isinstance(raw, str) or type(duration) not in (int, float) or not math.isfinite(duration) or
            not 0 < duration <= 1800):
        raise PlayerError('LYRICS_INVALID', 502)
    if len(raw) > 256*1024 or len(raw.encode('utf-8')) > 256*1024:
        raise PlayerError('LYRICS_LIMIT', 422)
    raw = raw.lstrip('\ufeff').strip()
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', raw, re.IGNORECASE):
        raise PlayerError('LYRICS_INVALID', 502)
    if re.fullmatch(r'[a-fA-F0-9\s]{64,}', raw or '!'):
        raise PlayerError('LYRICS_INVALID', 502)
    if raw.startswith(('<?xml', '<Qrc', '<qrc')):
        try:
            root = ET.fromstring(raw)
            values = [element.attrib['LyricContent'] for element in root.iter() if 'LyricContent' in element.attrib]
            if len(values) != 1:
                raise ValueError()
            raw = values[0]
        except (ET.ParseError, ValueError):
            raise PlayerError('LYRICS_INVALID', 502) from None
    raw = ''.join(c for c in raw.replace('\r\n', '\n').replace('\r', '\n') if c in '\n\t' or ord(c) >= 32)
    source_lines = raw.split('\n')
    if len(source_lines) > 1000:
        raise PlayerError('LYRICS_LIMIT', 422)
    credits, offset = {}, 0.
    for line in source_lines:
        match = META.fullmatch(line.strip())
        if match:
            name, value = match.group(1).lower(), match.group(2).strip()
            if name in META_KEYS:
                credits[META_KEYS[name]] = value[:240]
            elif name == 'offset':
                try:
                    millis = int(value)
                    if abs(millis) > 600000: raise ValueError()
                    offset = millis/1000
                except ValueError:
                    raise PlayerError('LYRICS_INVALID', 502) from None
    lines, format_name = [], 'text'
    for line in source_lines:
        line = line.strip()
        if not line or META.fullmatch(line): continue
        qrc = QRC.match(line)
        if qrc:
            stamps = [int(qrc.group(1))/1000 + offset]
            text = WORD.sub('', line[qrc.end():]).strip()
            format_name = 'qrc'
        else:
            matches = list(STAMP.finditer(line))
            if matches:
                stamps = []
                for match in matches:
                    minute, second, fraction = match.groups()
                    if int(second) >= 60: raise PlayerError('LYRICS_INVALID', 502)
                    stamps.append(60*int(minute)+int(second)+(int(fraction)/10**len(fraction) if fraction else 0)+offset)
                text = STAMP.sub('', line).strip()
                if format_name != 'qrc': format_name = 'lrc'
            else:
                stamps, text = [None], line
        if not text or text.strip(' 。！!') in EMPTY: continue
        for stamp in stamps:
            if stamp is not None and stamp > duration + 5: continue
            lines.append({'time': max(0, round(stamp, 3)) if stamp is not None else None, 'text': text})
            if len(lines) > 1000: raise PlayerError('LYRICS_LIMIT', 422)
    lines.sort(key=lambda row: row['time'] if row['time'] is not None else -1)
    seen, unique = set(), []
    for line in lines:
        item = (line['time'], line['text'])
        if item not in seen:
            seen.add(item); unique.append(line)
    exported = []
    for line in unique:
        if line['time'] is None:
            exported.append(line['text'])
        else:
            cents = round(line['time']*100)
            exported.append(f'[{cents//6000:02d}:{cents//100%60:02d}.{cents%100:02d}]' + line['text'])
    return {'format': format_name, 'text': '\n'.join(line['text'] for line in unique),
            'lrc': '\n'.join(exported), 'lines': unique, 'credits': credits}
