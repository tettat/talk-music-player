"""Offline loopback contracts; synthetic lyric and model results are not live evidence."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'src'))
import test_player as fixtures
TRACK = fixtures.TRACK


class InsightHttpTests(unittest.TestCase):
    setUp = fixtures.HttpTests.setUp
    tearDown = fixtures.HttpTests.tearDown
    request = fixtures.HttpTests.request
    def test_stats_empty_and_validated(self):
        for days in ('7','30','0'):
            status, _, body = self.request('/api/stats?days='+days)
            self.assertEqual(status,200); self.assertEqual(json.loads(body)['totalSeconds'],0)
        for days in ('-1','1','NaN'):
            self.assertEqual(self.request('/api/stats?days='+days)[0],400)

    def test_listening_requires_csrf_and_deduplicates(self):
        packet={'id':'c'*32,'key':TRACK['mid'],'seq':1,'seconds':0,'segments':[],'reason':'playing'}
        self.assertEqual(self.request('/api/listen',packet,{'X-Talk-CSRF':'wrong'})[0],403)
        self.assertEqual(self.request('/api/listen',packet)[0],200)
        packet={**packet,'seq':2,'seconds':1,'segments':[[0,1]],'reason':'close'}
        self.assertEqual(self.request('/api/listen',packet)[0],200)
        self.assertEqual(self.request('/api/listen',packet)[0],200)
        self.assertEqual(json.loads(self.request('/api/stats?days=0')[2])['totalSeconds'],1)
        self.assertEqual(len(json.loads(self.request('/api/history')[2])['items']),1)

    def test_missing_lyrics_and_analysis_are_not_fake(self):
        for kind in ('lyrics','analysis'):
            result=json.loads(self.request('/api/'+kind+'/'+TRACK['mid'])[2])
            self.assertEqual(result['status'],'missing'); self.assertIsNone(result[kind])

    def test_lyric_job_cache_and_export(self):
        old_run=self.app.bridge.run; calls=[]
        def run(operation,**args):
            if operation=='lyrics':
                calls.append(operation); return {'lyric':'[00:01]合成测试甲\n[00:02]合成测试乙','translation':''}
            return old_run(operation,**args)
        self.app.bridge.run=run
        status,_,body=self.request('/api/lyrics',{'key':TRACK['mid']}); self.assertEqual(status,202)
        jid=json.loads(body)['job']
        for _ in range(100):
            result=json.loads(self.request('/api/jobs/'+jid)[2])
            if result['status']!='loading':break
            time.sleep(.01)
        self.assertEqual(result['status'],'ready'); self.assertTrue(result['lyrics']['available'])
        self.assertEqual(self.request('/api/lyrics',{'key':TRACK['mid']})[0],200)
        self.assertEqual(calls,['lyrics'])
        status,headers,body=self.request('/api/lyrics/'+TRACK['mid']+'/export')
        self.assertEqual(status,200); self.assertIn('attachment;',headers['Content-Disposition'])
        self.assertIn('合成测试甲',body.decode())

    def test_lyric_auth_errors_sanitized_and_unlock_provider(self):
        status,_,body=self.request('/api/lyrics',{'key':TRACK['mid']}); self.assertEqual(status,202)
        jid=json.loads(body)['job']
        for _ in range(100):
            result=json.loads(self.request('/api/jobs/'+jid)[2])
            if result['status']!='loading':break
            time.sleep(.01)
        self.assertEqual(result['code'],'LOGIN_REQUIRED'); self.assertNotIn('exception',result)
        self.assertFalse(self.app.network.locked())

    def test_analysis_missing_runtime_and_refresh_validation(self):
        with patch('server.analysis_runner.ready',return_value=False):
            self.assertEqual(self.request('/api/analyze',{'key':TRACK['mid']})[0],503)
        for path in ('/api/analyze','/api/lyrics'):
            self.assertEqual(self.request(path,{'key':TRACK['mid'],'refresh':1})[0],400)

    def test_analysis_busy_does_not_take_provider_lock(self):
        self.app.analysis_slot.acquire()
        try:
            with patch('server.analysis_runner.ready',return_value=True):
                self.assertEqual(self.request('/api/analyze',{'key':TRACK['mid']})[0],409)
            self.assertFalse(self.app.network.locked())
        finally: self.app.analysis_slot.release()

    def test_pending_analysis_deduplicates_even_when_model_is_busy(self):
        jid='a'*32
        self.app.extra_jobs[jid]={'id':jid,'kind':'analysis','key':TRACK['mid'],'status':'loading','created':time.time()}
        status,_,body=self.request('/api/analyze',{'key':TRACK['mid']})
        self.assertEqual(status,202); self.assertEqual(json.loads(body)['job'],jid)
        self.assertFalse(self.app.network.locked())

    def test_analysis_failure_releases_both_locks(self):
        with patch('server.analysis_runner.ready',return_value=True), patch('server.analysis_runner.work_directory',side_effect=RuntimeError('PRIVATE_DETAIL')):
            status,_,body=self.request('/api/analyze',{'key':TRACK['mid']}); self.assertEqual(status,202)
            jid=json.loads(body)['job']
            for _ in range(100):
                result=json.loads(self.request('/api/jobs/'+jid)[2])
                if result['status']!='loading':break
                time.sleep(.01)
        self.assertEqual(result['code'],'ANALYSIS_FAILED'); self.assertNotIn('PRIVATE_DETAIL',json.dumps(result))
        self.assertFalse(self.app.network.locked()); self.assertFalse(self.app.analysis_slot.locked())

    def test_cached_analysis_never_exposes_private_fields(self):
        value=self.app.insights.put('analysis',TRACK['mid'],{'kind':'pitch','estimated':True})
        result=json.loads(self.request('/api/analysis/'+TRACK['mid'])[2])
        self.assertEqual(result['analysis'],value); self.assertNotIn('path',result['analysis'])

    def test_export_missing_and_analysis_export_rejected(self):
        for kind in ('lyrics','analysis'):
            self.assertEqual(self.request('/api/'+kind+'/'+TRACK['mid']+'/export')[0],404)


if __name__ == '__main__': unittest.main(verbosity=2)
