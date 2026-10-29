"""Daily scheduler ledger tests use only temporary databases and fake clocks."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

import vk_batch as engine


class SchedulerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.paths={1:self.root/'one.sqlite3',2:self.root/'two.sqlite3'}
        for path in self.paths.values():
            with closing(engine.db_open(path)):pass
        self.now=datetime(2026,10,22,5,0,tzinfo=timezone.utc)

    def state(self):
        import vk_scheduler_state as s
        state=s.State(self.root/'scheduler.sqlite3',self.paths)
        self.addCleanup(state.close);return state

    def add_attempt(self,account,action,at,community=None):
        with closing(engine.db_open(self.paths[account])) as db:
            cur=db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,11,?,?)',(action,at))
            if community:
                import vk_community as c
                c.init_state(db,1000+account)
                run=c.begin_run(db,community,0,1,1)
                db.execute('INSERT INTO community_run_attempts VALUES(?,?)',(run,cur.lastrowid))
            db.commit();return cur.lastrowid

    def test_usage_includes_manual_attempts_with_local_midnight_and_destination(self):
        state=self.state()
        self.add_attempt(1,'like','2026-10-21T18:59:59+00:00')
        self.add_attempt(1,'like','2026-10-21T19:00:00+00:00')
        self.add_attempt(1,'repost','2026-10-22T04:30:00+00:00')
        self.add_attempt(1,'repost','2026-10-22T04:30:01+00:00',242481741)
        usage=state.usage(self.now)
        self.assertEqual(usage[(1,'like',0)],1)
        self.assertEqual(usage[(1,'personal',0)],1)
        self.assertEqual(usage[(1,'community',242481741)],1)
        self.assertEqual(usage.get((2,'like',0),0),0)

    def test_vk_skip_refuses_reserved_intent_and_preserves_its_quota(self):
        state=self.state()
        with closing(engine.db_open(self.paths[1])) as db:engine.add_posts(db,101,[11])
        job=state.create_job(self.now,1,1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        state.before_attempt(job,self.now)
        with self.assertRaises(RuntimeError):state.finish_vk_skip(job,{'reason':'vk_like_denied','permission':'likes.can_like','value':False,'observations':2},self.now)
        self.assertEqual(state.usage(self.now)[(1,'like',0)],1)
        self.assertEqual(state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'attempting')
        self.assertEqual(state.db.execute('SELECT COUNT(*) FROM scheduler_skip_evidence').fetchone()[0],0)

    def test_vk_skip_rejects_unknown_evidence_and_uncertain_actor(self):
        state=self.state()
        with closing(engine.db_open(self.paths[1])) as db:engine.add_posts(db,101,[11])
        job=state.create_job(self.now,1,1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        valid={'reason':'vk_like_denied','permission':'likes.can_like','value':False,'observations':2}
        for bad in (None,{},dict(valid,value=True),dict(valid,observations=1),dict(valid,reason='selector_missing')):
            with self.assertRaises(RuntimeError):state.finish_vk_skip(job,bad,self.now)
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute("UPDATE posts SET like_status='uncertain'");db.commit()
        with self.assertRaises(RuntimeError):state.finish_vk_skip(job,valid,self.now)
        self.assertEqual(state.db.execute('SELECT COUNT(*) FROM scheduler_skip_evidence').fetchone()[0],0)

    def test_daily_schedule_persists_across_restart_and_batch_claim_is_once(self):
        state=self.state();plan=state.day_plan(self.now)
        self.assertEqual(len(plan),40)
        other=self.state();self.assertEqual(other.day_plan(self.now),plan)
        self.assertTrue(state.claim_batch(plan[2],2))
        self.assertFalse(other.claim_batch(plan[2],2))

    def test_actual_batch_gap_is_at_least_nine_minutes(self):
        from datetime import timedelta
        state=self.state()
        self.assertTrue(state.claim_batch(self.now,1))
        state.finish_batch(self.now,1,'complete')
        self.assertFalse(state.claim_batch(self.now+timedelta(seconds=539),2))
        self.assertTrue(state.claim_batch(self.now+timedelta(seconds=540),2))

    def test_stop_and_expired_final_click_are_blocked_after_reservation(self):
        state=self.state();job=state.create_job(self.now,1,1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        state.before_attempt(job,self.now)
        self.assertEqual(state.click_timeout(job,self.now),9000)
        with self.assertRaises(RuntimeError):
            state.click_timeout(job,datetime(2026,10,22,14,tzinfo=timezone.utc))
        state.set('stop_requested',True)
        with self.assertRaises(RuntimeError):state.click_timeout(job,self.now)

    def test_actual_spacing_survives_restart_and_is_checked_at_reservation(self):
        from datetime import timedelta
        from vk_scheduler_state import BatchSpacingWait
        state=self.state()
        row={'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())}
        first=state.create_job(self.now,1,1,'like',0,row)
        state.before_attempt(first,self.now);state.finish_job(first,'failed')
        restarted=self.state()
        next_job=restarted.create_job(self.now,2,2,'like',0,row)
        with self.assertRaises(BatchSpacingWait):
            restarted.before_attempt(next_job,self.now+timedelta(seconds=539))
        self.assertEqual(restarted.db.execute('SELECT status FROM scheduler_jobs WHERE id=?',(next_job,)).fetchone()[0],'dispatching')
        restarted.before_attempt(next_job,self.now+timedelta(seconds=540))
        self.assertEqual(restarted.get('last_action_batch')['slot'],2)

    def test_nine_minute_spacing_at_claim_and_reservation_after_restart(self):
        from datetime import timedelta
        from vk_scheduler_state import BatchSpacingWait
        state=self.state()
        self.assertTrue(state.claim_batch(self.now,1))
        row={'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())}
        first=state.create_job(self.now,1,1,'like',0,row)
        state.before_attempt(first,self.now);state.finish_job(first,'failed')
        state.finish_batch(self.now,1,'complete')
        restarted=self.state()
        self.assertFalse(restarted.claim_batch(self.now+timedelta(seconds=539,microseconds=999999),2))
        self.assertTrue(restarted.claim_batch(self.now+timedelta(seconds=540),2))
        second=restarted.create_job(self.now,2,2,'like',0,row)
        with self.assertRaises(BatchSpacingWait):
            restarted.before_attempt(second,self.now+timedelta(seconds=539,microseconds=999999))
        restarted.before_attempt(second,self.now+timedelta(seconds=540))

    def test_new_third_quota_preserves_existing_day_and_builds_next_day_sixty_slots(self):
        import vk_schedule_policy as policy
        from datetime import timedelta
        state=self.state();old=state.day_plan(self.now)
        before=state.db.execute('SELECT config,slots FROM schedule_days').fetchone()
        cfg=policy.validate_config({'accounts':{'5':{'likes':50,'personal_reposts':10,'community_reposts':20,'communities':[501,502]}}})
        state.policy_provider=lambda:cfg
        state.refresh_policy(self.now)
        self.assertEqual(state.day_plan(self.now),old)
        self.assertEqual(tuple(state.db.execute('SELECT config,slots FROM schedule_days').fetchone()),tuple(before))
        state.refresh_policy(self.now+timedelta(days=1))
        self.assertEqual(len(state.day_plan(self.now+timedelta(days=1))),60)

    def test_quota_guard_counts_existing_manual_actions_and_rejects_off_hours(self):
        state=self.state()
        for _ in range(50):self.add_attempt(1,'like','2026-10-22T04:00:00+00:00')
        with self.assertRaises(RuntimeError):state.guard(self.now,1,'like',0)
        with self.assertRaises(RuntimeError):state.guard(datetime(2026,10,22,14,0,tzinfo=timezone.utc),2,'like',0)
        state.guard(self.now,2,'like',0)

    def test_crash_intent_without_actual_attempt_consumes_quota_and_blocks_resume(self):
        state=self.state();job=state.create_job(self.now,1,1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        state.before_attempt(job,self.now)
        state.recover()
        self.assertEqual(state.usage(self.now)[(1,'like',0)],1)
        self.assertTrue(state.blocked())
        with self.assertRaises(RuntimeError):state.guard(self.now,2,'like',0)

    def test_recorded_attempt_is_not_double_counted_and_cannot_reserve_twice(self):
        state=self.state();job=state.create_job(self.now,1,1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        state.before_attempt(job,self.now)
        attempt=self.add_attempt(1,'like','2026-10-22T05:00:00+00:00')
        state.record_attempt(job,attempt)
        self.assertEqual(state.usage(self.now)[(1,'like',0)],1)
        with self.assertRaises(RuntimeError):state.before_attempt(job,self.now)
        state.finish_job(job,'verified')
        self.assertFalse(state.blocked())

    def test_candidate_priority_today_then_old_and_excludes_future_and_uncertain(self):
        state=self.state()
        rows=[{'group_id':101,'post_id':1,'published_at':int(self.now.timestamp())-86400},
              {'group_id':101,'post_id':2,'published_at':int(self.now.timestamp())-10},
              {'group_id':101,'post_id':3,'published_at':int(self.now.timestamp())+10}]
        self.assertEqual([r['post_id'] for r in state.prioritize(rows,self.now)],[2,1])

    def test_lower_known_engagement_first_but_old_zero_never_beats_today(self):
        state=self.state();stamp=int(self.now.timestamp())
        rows=[{'post_id':1,'published_at':stamp-1,'like_count':10,'repost_count':2},
              {'post_id':2,'published_at':stamp-200,'like_count':0,'repost_count':0},
              {'post_id':3,'published_at':stamp-86400,'like_count':0,'repost_count':0},
              {'post_id':4,'published_at':stamp-2}]
        self.assertEqual([r['post_id'] for r in state.prioritize(rows,self.now)],[2,1,4,3])

    def test_community_global_total_includes_manual_other_destination(self):
        state=self.state()
        for _ in range(20):self.add_attempt(1,'repost','2026-10-22T04:00:00+00:00',999)
        self.assertEqual(state.remaining(self.now)[(1,'community',242481741)],0)
        with self.assertRaises(RuntimeError):state.guard(self.now,1,'community',242481741)


if __name__=='__main__':unittest.main()
