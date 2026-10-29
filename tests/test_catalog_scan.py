"""Whole-catalog refresh fixtures with no network or real credentials."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import vk_api_read as api
import vk_batch as engine
import vk_groups as groups


class CatalogScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=engine.db_open(Path(self.tmp.name)/'catalog.sqlite3');self.addCleanup(self.db.close)
        groups.init_dates(self.db)
        self.now=int(datetime.now(timezone.utc).timestamp());self.clock_value=float(self.now)
        self.groups=[dict(id=gid,name=f'Group{gid}',enabled=True) for gid in range(1,58)]
        self.calls=[]
        self.quiet=redirect_stdout(io.StringIO());self.quiet.__enter__();self.addCleanup(self.quiet.__exit__,None,None,None)

    def transport(self,params):
        self.calls.append((abs(params['owner_id']),params['offset']))
        return 200,{'response':{'count':1,'items':[dict(owner_id=params['owner_id'],id=1,date=self.now-60)]}}

    def step(self,cycle,transport=None,**kwargs):
        import vk_catalog_scan as catalog
        return catalog.step(self.db,'fake-secret',cycle,transport=transport or self.transport,
            sleeper=lambda seconds:setattr(self,'clock_value',self.clock_value+seconds),
            clock=lambda:self.clock_value,**kwargs)

    def test_all_57_heads_are_scanned_without_three_group_cap(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups)
        for _ in range(57):self.step(cycle)
        report=c.coverage(self.db,cycle)
        self.assertEqual(report['complete'],57);self.assertEqual(report['state'],'complete')
        self.assertEqual(self.calls,[(gid,0) for gid in range(1,58)])
        self.assertIsNone(self.step(cycle));self.assertEqual(len(self.calls),57)
        self.assertNotIn('fake-secret','\n'.join(self.db.iterdump()))

    def test_partial_pages_resume_after_every_group_head_and_survive_restart(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:2])
        def transport(p):
            gid=abs(p['owner_id']);self.calls.append((gid,p['offset']))
            if gid==1 and p['offset']==0:
                items=[dict(owner_id=-gid,id=200-i,date=self.now-100-i) for i in range(100)]
                return 200,{'response':{'count':101,'items':items}}
            return 200,{'response':{'count':101 if gid==1 else 1,'items':[dict(owner_id=-gid,id=1,date=self.now-300)]}}
        self.step(cycle,transport);self.step(cycle,transport)
        self.assertEqual(c.start_cycle(self.db,self.groups[:2]),cycle)
        self.step(cycle,transport)
        self.assertEqual(self.calls,[(1,0),(2,0),(1,100)])
        self.assertEqual(c.coverage(self.db,cycle)['complete'],2)

    def test_new_cycle_forces_fresh_head_instead_of_legacy_partial_offset(self):
        import vk_catalog_scan as c
        client=api.VKReadClient(self.db,'fake-secret',transport=lambda p:(200,{'response':{'count':150,
            'items':[dict(owner_id=-1,id=200-i,date=self.now-100-i) for i in range(100)]}}))
        api.scan_one_group(self.db,client,self.groups[0])
        self.assertEqual(api._progress(self.db,1)['next_offset'],100)
        cycle=c.start_cycle(self.db,self.groups[:1]);self.step(cycle)
        self.assertEqual(self.calls,[(1,0)])
        second=c.start_cycle(self.db,self.groups[:1]);self.assertNotEqual(cycle,second)
        self.step(second);self.assertEqual(self.calls,[(1,0),(1,0)])

    def test_group_denial_is_reported_without_hiding_remaining_groups(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:2])
        self.step(cycle,lambda p:(200,{'error':{'error_code':7,'error_msg':'private text'}}))
        self.step(cycle)
        report=c.coverage(self.db,cycle)
        self.assertEqual((report['error'],report['complete'],report['state']),(1,1,'partial'))
        self.assertNotIn('private text','\n'.join(self.db.iterdump()))

    def test_invalid_key_stops_cycle_and_cannot_dispatch_next_group(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:2])
        with self.assertRaises(api.VkApiAuthFailure):
            self.step(cycle,lambda p:(200,{'error':{'error_code':5,'error_msg':'secret'}}))
        self.assertEqual(c.coverage(self.db,cycle)['state'],'stopped')
        self.assertIsNone(self.step(cycle));self.assertEqual(len(self.calls),0)

    def test_rate_limit_latches_and_is_not_retried(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:2])
        with self.assertRaises(api.VkApiLimited):self.step(cycle,lambda p:(429,{}))
        self.assertEqual(c.coverage(self.db,cycle)['state'],'stopped')
        self.assertIsNotNone(engine.safety_lock_state(self.db,0))
        self.assertIsNone(self.step(cycle))

    def test_page_limit_marks_incomplete_instead_of_looping_forever(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:1],max_pages_per_group=1)
        self.step(cycle,lambda p:(200,{'response':{'count':200,'items':[
            dict(owner_id=-1,id=200-i,date=self.now-100-i) for i in range(100)]}}))
        report=c.coverage(self.db,cycle)
        self.assertEqual((report['partial'],report['state']),(1,'partial'))
        self.assertIsNone(self.step(cycle))

    def test_finite_daily_budget_counts_existing_requests(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:2]);self.step(cycle,daily_limit=1)
        result=self.step(cycle,daily_limit=1)
        self.assertEqual(result['status'],'budget');self.assertEqual(len(self.calls),1)
        self.assertEqual(c.coverage(self.db,cycle)['pending'],1)
        with self.assertRaises(ValueError):self.step(cycle,daily_limit=5001)

    def test_increased_api_budget_uses_existing_rolling_24h_ledger_and_still_stops(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:2])
        stamp=datetime.fromtimestamp(self.clock_value,tz=timezone.utc).isoformat(timespec='seconds')
        self.db.executemany('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(?,?)',[(57,stamp)]*4999)
        self.db.commit()
        self.step(cycle,daily_limit=5000,pause_seconds=5)
        result=self.step(cycle,daily_limit=5000,pause_seconds=5)
        self.assertEqual(result['status'],'budget');self.assertEqual(self.calls,[(1,0)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],5000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertEqual(c.coverage(self.db,cycle)['pending'],1)

    def test_exact_api_counts_preserve_real_zero_and_sum(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:1])
        self.step(cycle,lambda p:(200,{'response':{'count':2,'items':[
            dict(owner_id=-1,id=2,date=self.now-10,likes={'count':0},reposts={'count':0}),
            dict(owner_id=-1,id=1,date=self.now-20,likes={'count':7},reposts={'count':3})]}}))
        rows=self.db.execute('SELECT * FROM api_post_engagement ORDER BY post_id').fetchall()
        self.assertEqual([(r['like_count'],r['repost_count']) for r in rows],[(7,3),(0,0)])
        self.assertTrue(all(r['observed_at'] for r in rows))
        report=api.engagement_report(self.db,self.groups[:1])
        self.assertEqual((report['posts'],report['known_total_posts'],report['unknown_total_posts']),(2,2,0))
        self.assertEqual((report['like_count'],report['repost_count'],report['engagement_total']),(7,3,10))

    def test_missing_or_malformed_counts_are_unknown_instead_of_zero(self):
        import vk_catalog_scan as c
        invalid=[None,{},True,-1,1.5,'3']
        items=[dict(owner_id=-1,id=i+1,date=self.now-10-i,
            likes={'count':count},reposts={'count':2}) for i,count in enumerate(invalid)]
        items.append(dict(owner_id=-1,id=7,date=self.now-30,likes={'count':1},reposts={'count':False}))
        cycle=c.start_cycle(self.db,self.groups[:1])
        self.step(cycle,lambda p:(200,{'response':{'count':len(items),'items':items}}))
        rows=self.db.execute('SELECT * FROM api_post_engagement ORDER BY post_id').fetchall()
        self.assertEqual([r['like_count'] for r in rows],[None]*6+[1])
        self.assertEqual([r['repost_count'] for r in rows],[2]*6+[None])
        report=api.engagement_report(self.db,self.groups[:1])
        self.assertEqual((report['known_total_posts'],report['unknown_total_posts']),(0,7))
        self.assertIsNone(report['engagement_total'])

    def test_refresh_missing_counts_invalidates_stale_values(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:1])
        self.step(cycle,lambda p:(200,{'response':{'count':1,'items':[
            dict(owner_id=-1,id=1,date=self.now-60,likes={'count':5},reposts={'count':4})]}}))
        self.db.execute("UPDATE api_post_engagement SET observed_at='stale'");self.db.commit()
        second=c.start_cycle(self.db,self.groups[:1]);self.step(second)
        row=self.db.execute('SELECT * FROM api_post_engagement').fetchone()
        self.assertIsNone(row['like_count']);self.assertIsNone(row['repost_count'])
        self.assertNotEqual(row['observed_at'],'stale')
        self.assertEqual(self.db.execute('SELECT valid FROM api_seen_posts').fetchone()[0],1)

    def test_comments_are_counted_and_missing_is_unknown(self):
        import vk_catalog_scan as c
        cycle=c.start_cycle(self.db,self.groups[:1])
        self.step(cycle,lambda p:(200,{'response':{'count':2,'items':[
            dict(owner_id=-1,id=2,date=self.now-10,likes={'count':1},reposts={'count':2},comments={'count':3}),
            dict(owner_id=-1,id=1,date=self.now-20,likes={'count':0},reposts={'count':0})]}}))
        row=self.db.execute('SELECT * FROM api_post_engagement WHERE post_id=2').fetchone()
        self.assertIn('comment_count',row.keys())
        self.assertEqual(row['comment_count'],3)
        self.assertIsNone(self.db.execute('SELECT comment_count FROM api_post_engagement WHERE post_id=1').fetchone()[0])
        report=api.engagement_report(self.db,self.groups[:1])
        self.assertEqual((report['comment_count'],report['known_activity_posts'],report['activity_total']),(3,1,6))

    def test_engagement_ignores_wrong_owner_and_report_filters_day_and_validity(self):
        import vk_catalog_scan as c
        items=[dict(owner_id=-1,id=3,date=self.now-5,likes={'count':10},reposts={'count':2}),
               dict(owner_id=-1,id=2,date=self.now-60,likes={'count':4},reposts={'count':1}),
               dict(owner_id=-999,id=1,date=self.now-120,likes={'count':999},reposts={'count':999})]
        cycle=c.start_cycle(self.db,self.groups[:1])
        self.step(cycle,lambda p:(200,{'response':{'count':len(items),'items':items}}))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_post_engagement').fetchone()[0],2)
        report=api.engagement_report(self.db,self.groups[:1],since=self.now-30,until=self.now)
        self.assertEqual((report['posts'],report['engagement_total']),(1,12))
        self.db.execute('UPDATE api_seen_posts SET valid=0 WHERE post_id=3');self.db.commit()
        report=api.engagement_report(self.db,self.groups[:1])
        self.assertEqual((report['posts'],report['engagement_total']),(1,5))
        report=api.engagement_report(self.db,self.groups[1:2])
        self.assertEqual(report['posts'],0);self.assertIsNone(report['engagement_total'])


if __name__=='__main__':unittest.main()
