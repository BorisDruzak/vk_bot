"""Offline regression: API-only discovery/selection, 7-day coverage, migration."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_groups as multi
import vk_unified as unified


class ApiOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=engine.db_open(Path(self.tmp.name)/'posts.sqlite')
        multi.init_dates(self.db)
        self.group={'id':101,'name':'Тестовое сообщество','enabled':True,
                    'like_enabled':True,'repost_enabled':True}
        self.second={'id':102,'name':'Второе сообщество','enabled':True,
                     'like_enabled':True,'repost_enabled':True}
        self.now=int(datetime.now(timezone.utc).timestamp())

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def post(self,pid,delta=500,group=101,pinned=False):
        return {'owner_id':-group,'id':pid,'date':self.now-delta,
                'is_pinned':1 if pinned else 0}

    def client(self,transport,per_run=3):
        return api.VKReadClient(self.db,'DUMMY_NEVER_PERSIST',transport=transport,
               per_run=per_run,pause_seconds=20,sleeper=lambda x:None)

    def api_page(self,items,count=None):
        obj={'items':items}
        if count is not None:
            obj['count']=count
        return 200,{'response':obj}

    def test_exactly_100_posts_total_means_complete(self):
        seen=[]
        posts=[self.post(i) for i in range(1,101)]
        def transport(p):
            seen.append(p['offset'])
            return self.api_page(posts,count=100)
        result=api.scan_one_group(self.db,self.client(transport),self.group,max_pages=3)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['reason'],'end-of-wall')
        self.assertEqual(seen,[0])

    def test_old_nonpinned_is_week_boundary_even_on_full_page(self):
        posts=[self.post(i,delta=80000+i) for i in range(1,100)]
        posts.append(self.post(100,delta=8*86400))
        seen=[]
        def transport(p):
            seen.append(p['offset'])
            return self.api_page(posts,count=800)
        result=api.scan_one_group(self.db,self.client(transport),self.group,max_pages=3)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['reason'],'older-unpinned')
        self.assertEqual(seen,[0])

    def test_old_pinned_does_not_stop_without_week_boundary(self):
        first=[self.post(900,delta=15*86400,pinned=True)]
        first += [self.post(i,delta=3600+i) for i in range(1,100)]
        second=[self.post(i,delta=8*86400+i) for i in range(100,150)]
        seen=[]
        def transport(p):
            seen.append(p['offset'])
            return self.api_page(first if p['offset']==0 else second,count=150)
        r=api.scan_one_group(self.db,self.client(transport),self.group,max_pages=3)
        self.assertEqual(seen,[0,100])
        self.assertEqual(r['status'],'complete')
        self.assertEqual(r['pages'],2)
        self.assertEqual(r['recent'],99)
        self.assertEqual(r['fetched'],150)

    def test_full_fresh_page_gets_next_offset(self):
        first=[self.post(i,delta=i*60) for i in range(1,101)]
        second=[self.post(i,delta=i*60) for i in range(101,150)]
        seen=[]
        def transport(p):
            seen.append(p['offset'])
            return self.api_page(first if p['offset']==0 else second,count=149)
        r=api.scan_one_group(self.db,self.client(transport),self.group,max_pages=3)
        self.assertEqual(seen,[0,100])
        self.assertEqual(r['status'],'complete')
        self.assertEqual(r['fetched'],149)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_seen_posts').fetchone()[0],149)

    def test_resume_incomplete_scan_without_reopening_first_page(self):
        pages={0:[self.post(i,delta=i*60) for i in range(1,101)],
               100:[self.post(i,delta=i*60) for i in range(101,150)]}
        seen=[]
        def transport(p):
            seen.append(p['offset'])
            return self.api_page(pages[p['offset']],count=149)
        c=self.client(transport)
        first=api.scan_one_group(self.db,c,self.group,max_pages=1)
        self.assertTrue(first['possibly_incomplete'])
        self.assertEqual(first['next_offset'],100)
        self.assertEqual(api.choose_api_scan_groups(self.db,[self.group],3),[self.group])
        second=api.scan_one_group(self.db,c,self.group,max_pages=2)
        self.assertEqual(seen,[0,100])
        self.assertEqual(second['status'],'complete')
        self.assertEqual(second['recent'],149)
        self.assertEqual(second['fetched'],49)
        self.assertEqual(api.choose_api_scan_groups(self.db,[self.group],3),[])

    def test_changed_total_invalidates_resume(self):
        first=[self.post(i,delta=i*50) for i in range(1,101)]
        second=[self.post(i,delta=i*50) for i in range(101,150)]
        count=[150]
        def transport(p):
            return self.api_page(first if p['offset']==0 else second,count=count[0])
        c=self.client(transport)
        api.scan_one_group(self.db,c,self.group,max_pages=1)
        count[0]=151
        result=api.scan_one_group(self.db,c,self.group,max_pages=2)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['reason'],'total-changed')
        self.assertEqual(result['next_offset'],0)
        r=self.db.execute('SELECT * FROM api_group_scan_progress').fetchone()
        self.assertEqual(r['next_offset'],0)

    def test_repeated_page_id_marks_partial(self):
        first=[self.post(i,delta=i*50) for i in range(1,101)]
        second=[self.post(100,delta=100*50)] + [self.post(i,delta=i*50) for i in range(101,150)]
        def transport(p):
            return self.api_page(first if p['offset']==0 else second,count=150)
        r=api.scan_one_group(self.db,self.client(transport),self.group,max_pages=3)
        self.assertEqual(r['status'],'partial')
        self.assertEqual(r['reason'],'repeated-items')
        self.assertEqual(r['next_offset'],0)

    def test_out_of_order_posts_mark_partial_even_if_old_seen(self):
        data=[self.post(1,delta=8*86400),self.post(2,delta=60)]
        r=api.scan_one_group(self.db,self.client(lambda p:self.api_page(data,count=300)),
                             self.group,max_pages=3)
        self.assertEqual(r['status'],'partial')
        self.assertEqual(r['reason'],'ordering-anomaly')

    def test_v617_api_history_backfilled_not_browser_history(self):
        for pid,source in [(4,'vk-api-wall.get'),(5,'vk-post-json')]:
            engine.add_posts(self.db,101,[pid])
            multi.save_date(self.db,101,pid,self.now-120,source,True)
        api.init_api(self.db)
        posts=self.db.execute('SELECT post_id FROM api_seen_posts ORDER BY post_id').fetchall()
        self.assertEqual([r['post_id'] for r in posts],[4])
        self.assertEqual([r['post_id'] for r in unified.pending_recent_likes(
            self.db,[self.group],7,limit=10)],[4])

    def test_api_provenance_survives_browser_date_metadata(self):
        r=api.scan_one_group(self.db,self.client(lambda p:self.api_page([
            self.post(4,delta=120)],count=1)),self.group)
        self.assertEqual(r['status'],'complete')
        multi.save_date(self.db,101,4,self.now-120,'vk-post-json',True)
        chosen=unified.pending_recent_likes(self.db,[self.group],7,limit=10)
        self.assertEqual([(r['group_id'],r['post_id']) for r in chosen],[(101,4)])

    def test_invalid_payload_does_not_create_action_candidate(self):
        bad={'owner_id':-102,'id':3,'date':self.now-100}
        r=api.scan_one_group(self.db,self.client(lambda p:self.api_page([bad],count=1)),
                             self.group,max_pages=3)
        self.assertEqual(r['status'],'partial')
        self.assertEqual(unified.pending_recent_likes(self.db,[self.group],7),[])

    def test_api_report_counts_full_partial_error_unknown(self):
        api.init_api(self.db)
        api._save_progress(self.db,101,status='complete',fetched=1,recent=1,pages=1,
            offset=0,total=1,cutoff=self.now-7*86400,last_nonpinned=self.now-60,reason='end-of-wall')
        api._save_progress(self.db,102,status='partial',fetched=100,recent=100,pages=1,
            offset=100,total=200,cutoff=self.now-7*86400,last_nonpinned=self.now-60,reason='page-cap')
        third={'id':103,'name':'ошибка'}
        fourth={'id':104,'name':'не проверена'}
        api.mark_api_group_error(self.db,103,'VK API error 15')
        buf=io.StringIO()
        with redirect_stdout(buf):
            counts=api.api_scan_report(self.db,[self.group,self.second,third,fourth],details=True)
        self.assertEqual(counts,{'complete':1,'stale':0,'partial':1,'error':1,'not_checked':1})
        self.assertIn('104 | not_checked',buf.getvalue())

    def test_migration_marks_v617_cache_as_unverified_not_full(self):
        self.db.execute('''CREATE TABLE api_group_scan_progress(
          group_id INTEGER PRIMARY KEY,last_scanned_at TEXT NOT NULL,discovered INTEGER NOT NULL)''')
        self.db.execute('INSERT INTO api_group_scan_progress VALUES(?,?,?)',(101,engine.moment(),100))
        self.db.commit()
        api.init_api(self.db)
        row=self.db.execute('SELECT * FROM api_group_scan_progress').fetchone()
        self.assertEqual(row['status'],'partial')
        self.assertEqual(row['reason'],'legacy-unverified')
        self.assertEqual(api.choose_api_scan_groups(self.db,[self.group],3),[self.group])

    def test_preview_never_opens_browser_or_network_and_only_api_posts(self):
        engine.add_posts(self.db,101,[1,2])
        multi.save_date(self.db,101,1,self.now-100,'vk-post-json',True)
        multi.save_date(self.db,101,2,self.now-200,'vk-api-wall.get',True)
        cfg=multi.load_config()
        with patch.object(engine,'DB_PATH',Path(self.tmp.name)/'posts.sqlite'),\
             patch.object(unified,'sync_playwright') as browser,\
             patch.object(api,'_default_transport') as network,\
             redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(unified.main(['--preview','--only','188206921','--max-posts','5']),0)
        browser.assert_not_called()
        network.assert_not_called()
        self.assertIn('[PREVIEW]',buf.getvalue())
        # DB fixture posts belong to 101, not the selected 188206921.
        self.assertNotIn('wall-101_1',buf.getvalue())

    def test_run_has_no_opportunistic_discover(self):
        from unittest.mock import MagicMock
        with patch.object(engine,'DB_PATH',Path(self.tmp.name)/'posts.sqlite'),\
             patch.object(unified,'discover_api') as discover,\
             patch.object(unified,'run_likes',return_value=0) as runner,\
             patch.object(unified,'exclusive_run') as exclusivity,\
             redirect_stdout(io.StringIO()):
            cm=exclusivity.return_value
            cm.__enter__=MagicMock(return_value=None)
            cm.__exit__=MagicMock(return_value=False)
            self.assertEqual(unified.main(['--run','--confirm','--only','188206921']),0)
        discover.assert_not_called()
        runner.assert_called_once()


if __name__ == '__main__':
    unittest.main()
