"""Explicit operator exceptions retain uncertain evidence and never replay it."""
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

import vk_batch as engine
import vk_api_read as api
import vk_groups as groups
from vk_scheduler_state import State


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.paths={a:root/f'{a}.sqlite3' for a in (1,2)}
        self.now=datetime(2026,10,22,5,tzinfo=timezone.utc)
        for path in self.paths.values():
            with closing(engine.db_open(path)) as db:
                api.init_api(db);groups.init_dates(db)
                engine.add_posts(db,101,[11,12])
                for pid in (11,12):
                    stamp=int(self.now.timestamp())-10
                    groups.save_date(db,101,pid,stamp,api.API_SOURCE,True)
                    db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',(101,pid,stamp,self.now.isoformat()))
                db.commit()
        self.state=State(root/'scheduler.sqlite3',self.paths);self.addCleanup(self.state.close)
        self.row={'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())-10}
        self.job=self.state.create_job(self.now,1,2,'like',0,self.row)
        self.state.before_attempt(self.job,self.now)
        with closing(engine.db_open(self.paths[2])) as db:
            self.aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,11,'like',?)",(self.now.isoformat(),)).lastrowid
            db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=101 AND post_id=11");db.commit()
        self.state.record_attempt(self.job,self.aid)
        self.state.finish_job(self.job,'uncertain','action_stopped_personal_action')
        self.state.pause('action_stopped_personal_action')

    def isolate(self):
        self.state.isolate_uncertain_like(self.job,self.now,authorization='explicit_user_post_isolation')

    def test_isolation_preserves_attempt_quota_plan_and_uncertain_after_restart(self):
        plan=self.state.day_plan(self.now);remaining=self.state.remaining(self.now)
        self.isolate()
        self.assertTrue(self.state.blocked())  # Never clear the pause implicitly.
        self.state.set('paused',None)
        self.assertFalse(self.state.blocked())
        restarted=State(self.state.paths[1].parent/'scheduler.sqlite3',self.paths);self.addCleanup(restarted.close)
        restarted.recover()
        self.assertFalse(restarted.blocked())
        self.assertEqual(restarted.day_plan(self.now),plan)
        self.assertEqual(restarted.remaining(self.now),remaining)
        self.assertEqual(restarted.remaining(self.now)[(2,'like',0)],49)
        self.assertEqual(tuple(restarted.db.execute('SELECT status,attempt_id FROM scheduler_jobs WHERE id=?',(self.job,)).fetchone()),('uncertain',self.aid))
        with restarted.account_db(2) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT like_status FROM posts WHERE post_id=11').fetchone()[0],'uncertain')

    def test_post_excluded_all_kinds_and_future_days_even_if_actor_status_changes(self):
        self.isolate();self.state.set('paused',None)
        # The explicit permanent exclusion must not rely on today's uncertain flag.
        with closing(engine.db_open(self.paths[2])) as db:
            db.execute("UPDATE posts SET like_status='pending'");db.commit()
        selected=[{'id':101,'enabled':True,'like_enabled':True,'repost_enabled':True}]
        for now in (self.now,self.now+timedelta(days=1)):
            for kind,target in (('like',0),('personal',0),('community',242479129)):
                rows=self.state.candidates(now,2,kind,target,self.paths[1],selected)
                self.assertEqual([r['post_id'] for r in rows],[12])
                with self.assertRaisesRegex(RuntimeError,'isolated'):
                    self.state.create_job(now,2,2,kind,target,self.row)
        self.assertIn(11,[r['post_id'] for r in self.state.candidates(self.now,1,'like',0,self.paths[1],selected)])

    def test_other_uncertain_still_blocks_and_exception_cannot_be_reused(self):
        self.isolate();self.state.set('paused',None)
        other=self.state.create_job(self.now,2,1,'like',0,self.row)
        self.state.finish_job(other,'uncertain','another_failure')
        self.assertTrue(self.state.blocked())
        with self.assertRaises(RuntimeError):self.state.guard(self.now,1,'like',0)
        self.state.finish_job(other,'failed')
        self.state.db.execute("UPDATE scheduler_jobs SET reason='different_failure' WHERE id=?",(self.job,));self.state.db.commit()
        self.assertTrue(self.state.blocked())

    def test_missing_authority_or_mismatched_attempt_cannot_create_exception(self):
        with self.assertRaises(ValueError):
            self.state.isolate_uncertain_like(self.job,self.now,authorization='')
        with closing(engine.db_open(self.paths[2])) as db:
            db.execute("UPDATE action_attempts SET action='repost'");db.commit()
        with self.assertRaises(RuntimeError):self.isolate()
        self.state.set('paused',None)
        self.assertTrue(self.state.blocked())

    def test_preexisting_dispatch_cannot_reserve_or_click_isolated_post(self):
        self.state.db.execute("UPDATE scheduler_jobs SET status='failed' WHERE id=?",(self.job,));self.state.db.commit()
        self.state.set('paused',None)
        pending=self.state.create_job(self.now,2,2,'personal',0,self.row)
        self.state.db.execute("UPDATE scheduler_jobs SET status='uncertain' WHERE id=?",(self.job,));self.state.db.commit()
        self.isolate()
        with self.assertRaisesRegex(RuntimeError,'isolated'):self.state.before_attempt(pending,self.now)
        self.state.db.execute("UPDATE scheduler_jobs SET status='attempting' WHERE id=?",(pending,));self.state.db.commit()
        with self.assertRaisesRegex(RuntimeError,'isolated'):self.state.click_timeout(pending,self.now)


if __name__=='__main__':unittest.main()
