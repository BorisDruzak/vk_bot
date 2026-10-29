"""Latest-100 group windows and bounded official-API statistics refresh."""
import importlib.util
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_accounts as accounts
import vk_batch as engine
import vk_groups as public_groups
import vk_statistics as stats
import vk_unified as app


class GroupWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'cache.sqlite3'
        self.db=engine.db_open(self.path)
        self.addCleanup(self.db.close)
        public_groups.init_dates(self.db);api.init_api(self.db)

    def add(self,gid,pid,published,likes=1,views=2,observed='2026-10-23T05:00:00+00:00',valid=1):
        self.db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,?)',(gid,pid,published,observed,valid))
        self.db.execute('''INSERT INTO api_post_engagement
            (group_id,post_id,like_count,repost_count,comment_count,view_count,observed_at)
            VALUES(?,?,?,0,3,?,?)''',(gid,pid,likes,views,observed))

    def test_latest_100_is_per_group_with_stable_ties_and_no_historical_pollution(self):
        # Every timestamp ties: post_id DESC decides which five are excluded.
        for gid in (1,2):
            for pid in range(1,106):
                self.add(gid,pid,1000,likes=pid,views=None if pid<=5 else 0)
        self.add(1,999,1001,likes=999,views=999,valid=0)
        self.db.commit()
        rows={r['id']:r for r in stats.groups(self.path)}
        self.assertEqual(rows[1]['posts'],100)
        self.assertEqual(rows[2]['posts'],100)
        self.assertEqual(rows[1]['like_count'],sum(range(6,106)))
        self.assertEqual((rows[1]['view_count'],rows[1]['view_count_known_posts']),(0,100))

    def test_partial_known_counts_and_periods_remain_explicit(self):
        self.add(1,1,1000,likes=0,views=None,observed='2026-10-23T04:00:00+00:00')
        self.add(1,2,2000,likes=None,views=12,observed='2026-10-23T05:00:00+00:00')
        self.db.commit()
        row=next(r for r in stats.groups(self.path) if r['id']==1)
        self.assertEqual(row.get('coverage_posts'),2)
        self.assertEqual((row['like_count_known_sum'],row['like_count_known_posts']),(0,1))
        self.assertIsNone(row['like_count'])
        self.assertEqual((row['view_count_known_sum'],row['view_count_known_posts']),(12,1))
        self.assertIsNone(row['view_count'])
        self.assertEqual((row['published_from'],row['published_until']),(1000,2000))
        self.assertEqual((row['observed_at'],row['observed_until']),
                         ('2026-10-23T04:00:00+00:00','2026-10-23T05:00:00+00:00'))
        self.assertEqual((row['source'],row['limit']),('vk-api-wall.get',100))

    def test_empty_configured_group_has_unknown_sums_and_empty_period(self):
        with patch.object(stats,'_names',return_value={1:'One'}):
            row=stats.groups(self.path)[0]
        self.assertEqual(row.get('coverage_posts'),0)
        self.assertIsNone(row['like_count_known_sum'])
        self.assertIsNone(row['published_from'])
        self.assertIsNone(row['published_until'])
        self.assertEqual((row['source'],row['limit']),('vk-api-wall.get',100))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'cache.sqlite3'
        self.lock=Path(self.tmp.name)/'running.lock'
        self.now=datetime.now(timezone.utc).timestamp()
        with closing(engine.db_open(self.path)) as db:
            public_groups.init_dates(db);api.init_api(db)
            db.execute("INSERT INTO api_group_scan_progress(group_id,last_scanned_at,discovered,next_offset) VALUES(1,'old',5,100)")
            db.commit()
        self.calls=[]

    def helper(self):
        self.assertIsNotNone(importlib.util.find_spec('vk_statistics_refresh'),
                             'bounded refresh helper required')
        import vk_statistics_refresh
        return vk_statistics_refresh

    def refresh(self,**kwargs):
        helper=self.helper()
        exclusive=app.exclusive_run
        with patch.object(helper.stats,'_names',return_value={1:'One'}),\
             patch.object(helper.api,'configured_token',return_value='fake-private-key'),\
             patch.object(helper.app,'exclusive_run',side_effect=lambda:exclusive(self.lock)):
            return helper.refresh_one(self.path,1,transport=kwargs.pop('transport',self.transport),
                                      clock=lambda:self.now,**kwargs)

    def transport(self,params):
        self.calls.append(dict(params))
        return 200,{'response':{'count':1,'items':[dict(owner_id=-1,id=9,date=int(self.now)-10,
                         likes={'count':3},reposts={'count':0},comments={'count':4},views={'count':12})]}}

    def test_one_head_read_existing_budget_pacing_and_no_catalog_cursor_or_action_changes(self):
        helper=self.helper()
        stamp=datetime.fromtimestamp(self.now,tz=timezone.utc).isoformat(timespec='seconds')
        with closing(engine.db_open(self.path)) as db:
            db.executemany('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(1,?)',[(stamp,)]*4999)
            db.execute('INSERT INTO read_navigation_pacing VALUES(0,?)',(self.now,));db.commit()
        pauses=[]
        result=self.refresh(sleeper=pauses.append)
        self.assertEqual(result['state'],'complete')
        self.assertEqual((result['observed_posts'],result['requested_limit']),(1,100))
        self.assertEqual([(p['count'],p['offset'],p['v'],p['filter']) for p in self.calls],[(100,0,'5.199','owner')])
        self.assertEqual(pauses,[5])
        with closing(engine.db_open(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],5000)
            self.assertEqual(db.execute('SELECT next_offset FROM api_group_scan_progress').fetchone()[0],100)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertNotIn('fake-private-key','\n'.join(db.iterdump()))
        self.assertEqual(stats.posts(self.path,1)[0]['view_count'],12)
        self.assertEqual(self.refresh()['state'],'budget')
        self.assertEqual(len(self.calls),1)

    def test_busy_shared_lock_defers_without_read_or_removing_lock(self):
        helper=self.helper()
        self.lock.write_text('existing login or worker',encoding='utf-8')
        result=self.refresh()
        self.assertEqual((result['state'],result['reason']),('deferred','profile_or_worker_busy'))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.lock.read_text(encoding='utf-8'),'existing login or worker')

    def test_busy_activity_defers_before_profile_lock_or_any_api_reservation(self):
        self.helper()
        with patch.object(accounts,'exclusive_activity',
                          side_effect=app.RunAlreadyActive('activity busy')) as guard:
            result=self.refresh()
        self.assertEqual(result['state'],'deferred')
        guard.assert_called_once_with(self.path.parent)
        self.assertEqual(self.calls,[])
        self.assertFalse(self.lock.exists())
        with closing(engine.db_open(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertEqual(stats.posts(self.path),[])

    def test_group_allowlist_and_malformed_items_never_write_observations(self):
        helper=self.helper()
        with patch.object(helper.stats,'_names',return_value={1:'One'}):
            with self.assertRaises(ValueError):helper.refresh_one(self.path,2)
        with self.assertRaises(api.VkApiFailure):
            self.refresh(transport=lambda params:(200,{'response':{'count':1,'items':[
                dict(owner_id=-2,id=1,date=int(self.now)-10)]}}))
        self.assertEqual(stats.posts(self.path),[])

    def test_mixed_head_preserves_98_valid_observations_and_reports_two_rejections(self):
        self.helper()
        items=[dict(owner_id=-1,id=pid,date=int(self.now)-pid,
                    likes={'count':3},reposts={'count':0},comments={'count':4},views={'count':12})
               for pid in range(1,101)]
        items[98]['owner_id']=-2
        items[99]['date']=0
        try:
            result=self.refresh(transport=lambda params:(200,{'response':{'count':100,'items':items}}))
        except api.VkApiFailure:
            self.fail('A mixed head must preserve valid observations instead of rejecting all 100')
        self.assertEqual((result['state'],result['returned_posts'],result['observed_posts'],result['rejected_posts']),
                         ('partial',100,98,2))
        self.assertEqual({r['post_id'] for r in stats.posts(self.path,1)},set(range(1,99)))
        group=next(r for r in stats.groups(self.path) if r['id']==1)
        self.assertEqual((group['view_count_known_posts'],group['view_count_known_sum']),(98,98*12))
        with closing(engine.db_open(self.path)) as db:
            self.assertEqual(db.execute('SELECT next_offset FROM api_group_scan_progress').fetchone()[0],100)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],1)

    def test_duplicate_id_even_on_invalid_item_still_rejects_entire_page(self):
        self.helper()
        items=[dict(owner_id=-1,id=1,date=int(self.now)-10),
               dict(owner_id=-2,id=1,date=int(self.now)-10)]
        with self.assertRaises(api.VkApiFailure):
            self.refresh(transport=lambda params:(200,{'response':{'count':2,'items':items}}))
        self.assertEqual(stats.posts(self.path),[])

    def test_rate_limit_preserves_lockout_and_single_request_without_retry(self):
        helper=self.helper()
        def limited(params):self.calls.append(params);return 429,{}
        with self.assertRaises(api.VkApiLimited):self.refresh(transport=limited)
        with closing(engine.db_open(self.path)) as db:
            self.assertIsNotNone(engine.safety_lock_state(db,0))
        self.assertEqual(len(self.calls),1)


if __name__=='__main__':unittest.main()
