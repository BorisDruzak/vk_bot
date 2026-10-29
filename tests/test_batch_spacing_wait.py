"""Timing regressions on temporary ledgers only; no VK or Windows tasks."""
import json
import unittest
from contextlib import closing,contextmanager
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch

import vk_batch as engine
import vk_scheduler as scheduler
import vk_schedule_policy as policy
from tests import test_scheduler_runner as fixtures


class BatchSpacingWaitTests(unittest.TestCase):
    setUp=fixtures.RunnerTests.setUp

    def setup_batch(self, *, at='2026-10-24T05:00:30+00:00', previous='2026-10-24T04:52:11+00:00', planned='2026-10-24T05:00:00+00:00'):
        self.now=datetime.fromisoformat(at)
        day=str(policy.local_day(self.now));stamp=datetime.fromisoformat(planned)
        slots=[(stamp-timedelta(minutes=10)).isoformat(),stamp.isoformat(),(stamp+timedelta(minutes=10)).isoformat()]
        self.state.db.execute('INSERT INTO schedule_days VALUES(?,?,?)',(day,json.dumps(self.state.config,sort_keys=True),json.dumps(slots)));self.state.db.commit()
        self.state.set('last_action_batch',{'day':day,'slot':0,'at':previous})
        self.plan=self.state.db.execute('SELECT * FROM schedule_days').fetchall()
        self.sleeps=[];self.picks=[]
        def sleep(seconds):
            self.assertLessEqual(seconds,5)
            self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)
            self.sleeps.append(seconds);self.now+=timedelta(seconds=seconds)
            if self.on_sleep:self.on_sleep()
        self.on_sleep=None
        worker=scheduler.Worker(self.state,self.paths[1],self.cfg,clock=lambda:self.now)
        worker.sleeper=sleep
        worker.refresh_batch=Mock(return_value={'state':'ready','pool':None,'report':{}})
        return worker

    def run_real_callbacks(self,worker):
        executed=[]
        def pick(now):
            self.picks.append(now)
            return (1,'like',0,{'group_id':101,'post_id':len(executed)+1,'published_at':int(now.timestamp())-1})
        def execute(account,kind,row,*,before_attempt,record_attempt,**kw):
            before_attempt();executed.append(self.now)
            with closing(engine.db_open(self.paths[1])) as db:
                aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",(row['post_id'],self.now.isoformat())).lastrowid;db.commit()
            record_attempt(aid);return {'status':'verified','attempt_id':aid}
        with patch.object(worker,'pick',side_effect=pick),patch('vk_scheduler.actions.execute_action',side_effect=execute):result=worker.run_batch(1)
        self.assertEqual(self.state.db.execute('SELECT * FROM schedule_days').fetchall(),self.plan)
        return result,executed

    def test_morning_41_seconds_wait_before_any_job_then_four_attempts(self):
        worker=self.setup_batch();result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'batch_complete');self.assertEqual(len(executed),4)
        self.assertEqual(sum(self.sleeps),41)
        self.assertGreaterEqual(executed[0],datetime.fromisoformat('2026-10-24T05:01:11+00:00'))
        self.assertEqual(worker.refresh_batch.call_count,1)

    def test_morning_31_seconds_wait(self):
        worker=self.setup_batch(at='2026-10-24T05:50:18+00:00',previous='2026-10-24T05:41:49+00:00',planned='2026-10-24T05:49:42+00:00')
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(len(executed),4);self.assertEqual(sum(self.sleeps),31)

    def test_on_time_claim_can_wait_beyond_grace_without_reclaiming(self):
        worker=self.setup_batch(at='2026-10-24T04:11:28+00:00',previous='2026-10-24T04:06:08+00:00',planned='2026-10-24T04:10:04+00:00')
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'batch_complete');self.assertEqual(len(executed),4);self.assertEqual(sum(self.sleeps),220)
        self.assertFalse(self.state.claim_batch(self.now+timedelta(minutes=20),1))

    def test_stop_during_wait_never_creates_job_or_attempt(self):
        worker=self.setup_batch();self.on_sleep=lambda:self.state.set('stop_requested',True)
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'batch_stop_requested');self.assertEqual(executed,[])
        self.assertEqual(len(self.sleeps),1)

    def test_pause_during_wait_is_not_cleared(self):
        worker=self.setup_batch();self.on_sleep=lambda:self.state.pause('StopRun')
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'attention');self.assertEqual(executed,[]);self.assertEqual(self.state.get('paused'),'StopRun')

    def test_new_uncertain_during_wait_stops_without_creating_own_intent(self):
        worker=self.setup_batch()
        with patch.object(self.state,'active_uncertain_count',side_effect=lambda:1 if self.sleeps else 0):result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'attention');self.assertEqual(executed,[])

    def test_platform_lock_during_wait_is_honored(self):
        worker=self.setup_batch()
        def latch():
            with closing(engine.db_open(self.paths[2])) as db:
                db.execute("INSERT OR IGNORE INTO safety_lock VALUES(0,'warning','2026-10-24T05:00:35+00:00')");db.commit()
        self.on_sleep=latch
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'attention');self.assertEqual(executed,[]);self.assertEqual(self.state.get('paused'),'StopRun')

    def test_window_jump_during_wait_does_not_dispatch(self):
        worker=self.setup_batch();self.on_sleep=lambda:setattr(self,'now',datetime.fromisoformat('2026-10-24T14:00:00+00:00'))
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'batch_work_window_closed');self.assertEqual(executed,[])

    def test_day_jump_cancels_wait_without_intent_or_new_plan(self):
        worker=self.setup_batch();self.on_sleep=lambda:setattr(self,'now',datetime.fromisoformat('2026-10-25T05:00:00+00:00'))
        self.assertTrue(self.state.claim_batch(self.now,1))
        self.assertEqual(worker.wait_batch_spacing(1),'batch_day_changed')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)
        self.assertEqual(self.state.db.execute('SELECT * FROM schedule_days').fetchall(),self.plan)

    def test_clock_that_stops_advancing_cannot_wait_without_bound(self):
        worker=self.setup_batch()
        def frozen(seconds):self.sleeps.append(seconds)
        worker.sleeper=frozen
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'batch_spacing_deadline');self.assertEqual(executed,[])
        self.assertLessEqual(sum(self.sleeps),policy.MIN_BATCH_GAP_SECONDS)

    def test_already_ready_after_slow_refresh_does_not_require_new_claim(self):
        worker=self.setup_batch(previous='2026-10-24T04:52:11+00:00')
        def slow_refresh():
            self.now=datetime.fromisoformat('2026-10-24T05:06:00+00:00')
            return {'state':'ready','pool':None,'report':{}}
        worker.refresh_batch.side_effect=slow_refresh
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(len(executed),4);self.assertEqual(self.sleeps,[])

    def test_refresh_budget_failure_does_not_wait_or_pick(self):
        worker=self.setup_batch();worker.refresh_batch.return_value={'state':'budget','pool':set(),'report':{}}
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['phase'],'counter_refresh_budget');self.assertEqual(executed,[]);self.assertEqual(self.sleeps,[])

    def test_slow_safety_read_cannot_authorize_after_action_boundary(self):
        worker=self.setup_batch(previous='2026-10-24T05:00:00+00:00')
        self.assertTrue(self.state.claim_batch(self.now,1))
        self.now=datetime.fromisoformat('2026-10-24T05:08:55+00:00')
        original=self.state.account_db
        @contextmanager
        def slow(account):
            if self.now>=datetime.fromisoformat('2026-10-24T05:09:00+00:00'):self.now+=timedelta(seconds=2)
            with original(account) as db:yield db
        with patch.object(self.state,'account_db',side_effect=slow):result=worker.wait_batch_spacing(1)
        self.assertEqual(result,'batch_spacing_deadline')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)


if __name__=='__main__':unittest.main()
