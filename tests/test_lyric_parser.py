"""All lyric strings below are synthetic, not copied song lyrics."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lyric_parser import normalize_lyrics
from library import PlayerError


class LyricTests(unittest.TestCase):
    def test_lrc_chinese_and_sorted_timestamps(self):
        data = normalize_lyrics('[00:20.123]测试乙\n[00:01.20]测试甲', 60)
        self.assertEqual(data['lines'][0], {'time':1.2,'text':'测试甲'}); self.assertEqual(data['format'],'lrc')

    def test_multiple_stamps_and_duplicates(self):
        data=normalize_lyrics('[00:01][00:02]测试\n[00:01]测试',60)
        self.assertEqual([r['time'] for r in data['lines']],[1,2])

    def test_offset_and_metadata(self):
        data=normalize_lyrics('[ar:测试作者]\n[offset:-500]\n[00:01]测试',60)
        self.assertEqual(data['lines'][0]['time'],.5); self.assertEqual(data['credits']['artist'],'测试作者')

    def test_qrc_word_times_removed(self):
        data=normalize_lyrics('[1000,2000]甲(1000,800)乙(1800,1200)',60)
        self.assertEqual(data['lines'],[{'time':1.,'text':'甲乙'}]); self.assertEqual(data['format'],'qrc')

    def test_xml_qrc_only_bounded_attribute(self):
        data=normalize_lyrics('<QrcInfos><LyricInfo><Lyric_1 LyricContent="[1000,1000]测试(1000,1000)"/></LyricInfo></QrcInfos>',60)
        self.assertEqual(data['text'],'测试')

    def test_entities_and_doctype_rejected(self):
        for raw in ['<!DOCTYPE x><QrcInfos/>','<!ENTITY x SYSTEM "file:///secret">','<?xml?><broken']:
            with self.assertRaises(PlayerError):normalize_lyrics(raw,60)

    def test_empty_unavailable_and_metadata_only(self):
        for raw in ['', '暂无歌词','[ar:测试]\n[ti:合成曲]','[00:00]纯音乐，请欣赏']:
            self.assertEqual(normalize_lyrics(raw,60)['lines'],[])

    def test_plain_text_and_control_characters(self):
        self.assertEqual(normalize_lyrics('测试甲\x00\n测试乙',60)['lines'],
            [{'time':None,'text':'测试甲'},{'time':None,'text':'测试乙'}])

    def test_markup_remains_inert_text_not_executed(self):
        self.assertEqual(normalize_lyrics('[00:01]<img onerror=x>',60)['text'],'<img onerror=x>')

    def test_time_outside_track_is_not_rendered(self):
        self.assertEqual(len(normalize_lyrics('[00:01]测试\n[99:59]越界',60)['lines']),1)

    def test_invalid_data_and_limits(self):
        for raw,duration in [(None,60),('测试',True),('测试',float('nan')),('f'*100,60),('x'*262145,60),('x\n'*1001,60)]:
            with self.assertRaises(PlayerError):normalize_lyrics(raw,duration)

    def test_export_reparsable(self):
        parsed=normalize_lyrics('[00:01.20]甲\n[00:02.50]乙',60)
        self.assertEqual(normalize_lyrics(parsed['lrc'],60)['lines'],parsed['lines'])


if __name__ == '__main__':unittest.main(verbosity=2)
