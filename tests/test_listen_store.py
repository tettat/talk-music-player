"""Fake clocks and synthetic segments; no real listening-history records are created."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from library import Library, PlayerError
from listen_store import History

TRACK = {'mid': '0039MnYb0qxYhV', 'songId': 1, 'title': '合成测试曲', 'artist': '测试', 'album': '测试', 'duration': 100}


class ListenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / 'library.sqlite3'
        self.lib = Library(self.path); self.lib.put([TRACK]); self.now = 1791624000.
        self.h = History(self.lib, lambda: self.now)
        self.packet = {'id': 'a'*32, 'key': TRACK['mid'], 'seq': 1, 'seconds': 0, 'segments': [], 'reason': 'playing'}

    def tearDown(self): self.lib.close(); self.temp.cleanup()

    def update(self, seconds, segments=None, reason='playing'):
        self.now += 10; self.packet = {**self.packet, 'seq': self.packet['seq']+1, 'seconds': seconds,
            'segments': segments if segments is not None else [[0, seconds]], 'reason': reason}
        return self.h.heartbeat(self.packet)

    def test_empty_and_zero_do_not_fake_history(self):
        self.assertEqual(self.h.summary()['songCount'], 0)
        self.h.heartbeat(self.packet); self.assertEqual(self.h.recent()['items'], [])

    def test_duplicate_old_seq_and_pause_do_not_add_time(self):
        self.h.heartbeat(self.packet); self.update(10); self.h.heartbeat(self.packet)
        self.h.heartbeat({**self.packet, 'seq': 1}); self.update(10, reason='pause')
        self.assertEqual(self.h.summary()['totalSeconds'], 10)

    def test_play_threshold_is_once_per_session(self):
        self.h.heartbeat(self.packet)
        for seconds in (10,20,30,40): self.update(seconds)
        self.assertEqual(self.h.summary()['playCount'], 1)

    def test_end_after_seek_is_not_completion(self):
        self.h.heartbeat(self.packet); self.update(2, [[98,100]], 'ended')
        self.assertFalse(self.h.recent()['items'][0]['completed'])

    def test_union_coverage_not_replayed_seconds(self):
        self.h.heartbeat(self.packet); self.update(10); self.update(20, [[0,10],[0,10]])
        self.assertAlmostEqual(self.h.recent()['items'][0]['coverage'], .1)

    def test_real_coverage_completion(self):
        self.h.heartbeat(self.packet)
        for seconds in range(10,91,10): self.update(seconds)
        self.assertTrue(self.h.recent()['items'][0]['completed']); self.assertEqual(self.h.summary()['completedCount'], 1)

    def test_closed_session_retry_only(self):
        self.h.heartbeat(self.packet); self.update(5, reason='skip'); self.h.heartbeat(self.packet)
        with self.assertRaises(PlayerError): self.update(6)
        self.assertEqual(self.h.summary()['skippedCount'], 1)

    def test_bad_input_atomic_and_cumulative_rollback(self):
        self.h.heartbeat(self.packet); self.update(10); before = self.h.recent()
        for data in [{**self.packet, 'seq': 3, 'seconds': 2}, {**self.packet, 'seconds': float('nan')},
                     {**self.packet, 'seq': True}, {**self.packet, 'segments': [[-1,5]]},
                     {**self.packet, 'segments': [[0,99]]}, {**self.packet, 'extra': 'bad'}]:
            with self.assertRaises(PlayerError): self.h.heartbeat(data)
        self.assertEqual(self.h.recent(), before)

    def test_huge_wall_sleep_and_first_packet_bounds(self):
        with self.assertRaises(PlayerError): self.h.heartbeat({**self.packet, 'seconds': 99, 'segments': [[0,99]]})
        self.h.heartbeat(self.packet); self.now += 3600
        with self.assertRaises(PlayerError): self.h.heartbeat({**self.packet, 'seq': 2, 'seconds': 100, 'segments': [[0,100]]})

    def test_restart_keeps_records_and_aggregation(self):
        self.h.heartbeat(self.packet); self.update(10); expected = self.h.summary(0)
        self.lib.close(); self.lib = Library(self.path); self.h = History(self.lib, lambda: self.now)
        self.assertEqual(self.h.summary(0), expected); self.assertEqual(len(self.h.recent()['items']), 1)

    def test_days_and_paging_validation(self):
        for days in (True, 1, -1, '7'):
            with self.assertRaises(PlayerError): self.h.summary(days)
        for limit, offset in ((True,0),(101,0),(1,-1)):
            with self.assertRaises(PlayerError): self.h.recent(limit,offset)

    def test_period_uses_heartbeat_day_not_library_timestamp(self):
        self.h.heartbeat(self.packet); self.update(10); self.now += 8*86400
        self.assertEqual(self.h.summary(7)['totalSeconds'], 0)
        self.assertEqual(self.h.summary(0)['totalSeconds'], 10)


if __name__ == '__main__': unittest.main(verbosity=2)
