"""Regression tests for separate browser and official VK API discovery caches."""
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

import vk_api_read as api
import vk_groups as multi
import vk_batch as engine
import vk_unified as unified


class SeparateApiCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=engine.db_open(Path(self.tmp.name)/'db.sqlite')
        multi.init_dates(self.db)
        multi.init_scan_progress(self.db)
        self.g={'id':101,'name':'Test','enabled':True,'like_enabled':True,'repost_enabled':True}
        multi.mark_group_scanned(self.db,101,15)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_legacy_browser_scan_never_suppresses_first_api_discovery(self):
        self.assertEqual(api.choose_api_scan_groups(self.db,[self.g],3,
                         min_interval_hours=12),[self.g])

    def test_api_scan_updates_own_cache_and_is_not_immediately_retried(self):
        now=int(datetime.now(timezone.utc).timestamp())
        client=api.VKReadClient(self.db,'test-secret',transport=lambda p:(200,{
            'response':{'count':1,'items':[{'owner_id':-101,'id':1234,'date':now-60}]}}))
        self.assertEqual(len(api.choose_api_scan_groups(self.db,[self.g],3,
                             min_interval_hours=12)),1)
        out=api.scan_one_group(self.db,client,self.g)
        self.assertEqual(out['fetched'],1)
        self.assertEqual(api.choose_api_scan_groups(self.db,[self.g],3,
                         min_interval_hours=12),[])
        stamp=self.db.execute('SELECT last_scanned_at FROM api_group_scan_progress WHERE group_id=101').fetchone()
        self.assertIsNotNone(stamp)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertNotIn('test-secret',str(list(self.db.iterdump())))

    def test_failed_api_request_is_not_marked_scanned_and_does_not_delete_legacy_cache(self):
        client=api.VKReadClient(self.db,'test-secret',transport=lambda p:(200,{
            'error':{'error_code':7,'error_msg':'Access denied'}}))
        with self.assertRaises(api.VkApiAuthFailure):
            api.scan_one_group(self.db,client,self.g)
        self.assertEqual(api.choose_api_scan_groups(self.db,[self.g],3,
                         min_interval_hours=12),[self.g])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM group_scan_progress').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_group_scan_progress').fetchone()[0],0)

    def test_cli_discover_uses_api_cache_even_with_recent_legacy_scan(self):
        cfg=multi.load_config()
        calls=[]
        class FakeClient:
            def __init__(self,*a,**kw): pass
            def remaining(self): return 1
        with patch.object(unified.api,'VKReadClient',FakeClient), \
             patch.object(unified.api,'scan_one_group',side_effect=lambda *a,**k:(
                 calls.append(True) or {'fetched':1,'recent':1,'possibly_incomplete':False})):
            n=unified.discover_api(self.db,cfg,[self.g],required=True,token='test-secret')
        self.assertEqual(n,1)
        self.assertEqual(calls,[True])

if __name__=='__main__':
    unittest.main()
