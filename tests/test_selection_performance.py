"""Call-local selection snapshots: temporary databases and no VK/browser calls."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_community as community
import vk_groups as groups
import vk_scheduler_state as ledger


class SelectionPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,10,27,5,tzinfo=timezone.utc)
        self.paths={a:self.root/f'{a}.sqlite3' for a in (1,2,3)}
        for account,path in self.paths.items():
            with closing(engine.db_open(path)) as db:
                community.init_state(db,1000+account)
                groups.init_dates(db);api.init_api(db)
                engine.add_posts(db,101,list(range(1,13)))
        self.state=ledger.State(self.root/'scheduler.sqlite3',self.paths,{
            'accounts':{str(a):{'likes':50,'personal_reposts':10,'community_reposts':20,
                                'communities':[201,202]} for a in self.paths}})
        self.addCleanup(self.state.close)
        self.groups=[{'id':101,'enabled':True,'like_enabled':True,'repost_enabled':True}]
        self.source=self.paths[1]
        with closing(engine.db_open(self.source)) as db:
            for pid in range(1,13):
                published=int(self.now.timestamp())-pid
                groups.save_date(db,101,pid,published,api.API_SOURCE,True)
                db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',
                           (101,pid,published,self.now.isoformat()))
                db.execute('''INSERT INTO api_post_engagement
                    (group_id,post_id,like_count,repost_count,comment_count,observed_at)
                    VALUES(101,?,?,0,0,?)''',(pid,pid,self.now.isoformat()))
            db.commit()

    def context(self,**kw):
        return self.state.selection_context(self.now,self.source,self.groups,**kw)

    def rows(self,kind='like',target=0,account=1,context=None):
        return self.state.candidates(self.now,account,kind,target,self.source,self.groups,context=context)

    def test_shared_context_reads_own_activity_once_for_twelve_categories(self):
        with patch.object(engagement,'local_activity',wraps=engagement.local_activity) as activity:
            context=self.context()
            for account,kind,target,_ in self.state.categories():
                self.rows(kind,target,account,context)
            self.assertEqual(activity.call_count,1)

    def test_date_skip_excludes_all_categories_only_for_same_actor_and_day(self):
        import vk_action_retry as retry
        self.state.set('action_retry_policy',1)
        job=self.state.create_job(self.now,0,1,'like',0,{'group_id':101,'post_id':7,'published_at':int(self.now.timestamp())-7})
        book=retry.RetryLedger(self.state);book.begin(job,self.now)
        self.assertTrue(book.skip_date(job,self.now))
        context=self.context()
        for kind,target in (('like',0),('personal',0),('community',201)):
            self.assertNotIn(7,[r['post_id'] for r in self.rows(kind,target,1,context)])
        self.assertIn(7,[r['post_id'] for r in self.rows('like',0,2,context)])
        self.now+=timedelta(days=1)
        self.assertIn(7,[r['post_id'] for r in self.rows('like',0,1,self.context())])

    def test_bulk_exclusions_preserve_signed_canonical_and_all_job_statuses(self):
        with closing(engine.db_open(self.source)) as db:
            db.execute("UPDATE posts SET like_status='uncertain' WHERE post_id=1")
            db.execute("UPDATE posts SET repost_status='verified' WHERE post_id=2")
            db.execute("INSERT INTO post_access(group_id,post_id,state,reason,checked_at) VALUES(101,3,'unavailable','test',?)",
                       (self.now.isoformat(),))
            db.executemany('INSERT INTO community_source_refs VALUES(101,?,?,7)',[(4,-99),(5,-99),(6,99)])
            community.write_state(db,201,101,4,'verified',target_post_id=1)
            db.commit()
        self.state.db.execute('''INSERT INTO scheduler_jobs(day,slot,account,kind,community_id,
            group_id,post_id,published_at,status,created_at)
            VALUES(?,0,1,'like',0,101,7,?,'failed',?)''',
            (str(self.now.date()),int(self.now.timestamp())-7,self.now.isoformat()))
        self.state.db.commit()
        context=self.context()
        self.assertEqual([r['post_id'] for r in self.rows(context=context)],[2,4,5,6,8,9,10,11,12])
        self.assertEqual([r['post_id'] for r in self.rows('personal',context=context)],[4,5,6,7,8,9,10,11,12])
        self.assertEqual([r['post_id'] for r in self.rows('community',201,context=context)],[2,6,7,8,9,10,11,12])

    def test_each_new_context_sees_new_verified_overlay_and_exclusion(self):
        before=self.rows(context=self.context())
        with closing(engine.db_open(self.paths[2])) as db:
            db.execute("UPDATE posts SET like_status='verified' WHERE post_id=1")
            db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,1,'like',?)",
                       ((self.now+timedelta(seconds=1)).isoformat(),));db.commit()
        after=self.rows(context=self.context())
        self.assertEqual(before[0]['like_count'],1)
        updated=next(r for r in after if r['post_id']==1)
        self.assertEqual((updated['like_count'],updated['own_like_delta']),(2,1))
        self.assertNotIn(1,[r['post_id'] for r in self.rows(account=2,context=self.context())])

    def test_pool_restricts_rows_and_returned_evidence_does_not_mutate_context(self):
        context=self.context(pool={(101,2),(101,5)})
        rows=self.rows(context=context)
        self.assertEqual([r['post_id'] for r in rows],[2,5])
        rows[0]['like_count']=999
        self.assertEqual(self.rows(context=context)[0]['like_count'],2)
        self.assertEqual(self.rows(context=self.context(pool=set())),[])

    def test_context_rejects_reuse_at_another_time(self):
        with self.assertRaises(ValueError):
            self.state.candidates(self.now+timedelta(seconds=1),1,'like',0,
                                  self.source,self.groups,context=self.context())

    def test_query_count_does_not_scale_with_post_count(self):
        def count():
            statements=[];original=sqlite3.connect
            def connect(*args,**kw):
                db=original(*args,**kw);db.set_trace_callback(statements.append);return db
            self.state.db.set_trace_callback(statements.append)
            try:
                with patch.object(sqlite3,'connect',side_effect=connect):
                    context=self.context()
                    for account,kind,target,_ in self.state.categories():
                        self.rows(kind,target,account,context)
            finally:self.state.db.set_trace_callback(None)
            return len(statements)
        small=count()
        for path in self.paths.values():
            with closing(engine.db_open(path)) as db:engine.add_posts(db,101,list(range(13,113)))
        with closing(engine.db_open(self.source)) as db:
            for pid in range(13,113):
                published=int(self.now.timestamp())-pid
                groups.save_date(db,101,pid,published,api.API_SOURCE,True)
                db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',(101,pid,published,self.now.isoformat()))
                db.execute('''INSERT INTO api_post_engagement
                    (group_id,post_id,like_count,repost_count,comment_count,observed_at)
                    VALUES(101,?,?,0,0,?)''',(pid,pid,self.now.isoformat()))
            db.commit()
        # Bound by actor/schema count, never one query for every post/category.
        self.assertEqual(count(),small)
        self.assertLess(small,100)

    def test_refresh_eligible_union_shares_only_one_call_local_context(self):
        with patch.object(engagement,'local_activity',wraps=engagement.local_activity) as activity:
            selected=engagement._eligible(self.state,self.source,self.groups,self.now)
            self.assertEqual(len(selected),12)
            self.assertEqual(activity.call_count,1)
            engagement._eligible(self.state,self.source,self.groups,self.now)
            self.assertEqual(activity.call_count,2)


if __name__=='__main__':unittest.main()
