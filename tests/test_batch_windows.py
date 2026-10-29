"""Independent read/intent windows, owned waits and durable resume gates."""
import json
import unittest
from contextlib import closing,contextmanager
from datetime import datetime,timedelta
from unittest.mock import patch,Mock

import vk_batch as engine
import vk_schedule_policy as policy
from tests import test_reserve_deadline as reserve
from tests import test_batch_spacing_wait as spacing


class PreparedActionWindowTests(unittest.TestCase):
    setUp=reserve.ReserveDeadlineTests.setUp
    setup_worker=reserve.ReserveDeadlineTests.setup_worker
    item=reserve.ReserveDeadlineTests.item
    transport=reserve.ReserveDeadlineTests.transport
    sleep=reserve.ReserveDeadlineTests.sleep
    execute=reserve.ReserveDeadlineTests.execute
    plan_gap=reserve.ReserveDeadlineTests.plan_gap
    run_batch=reserve.ReserveDeadlineTests.run_batch

    def setup_ready(self):
        self.setup_worker();self.plan_gap()
        self.dates[(101,13)]=self.dates[(101,11)]-1;self.counts[(101,13)]=9

    def test_fourth_fresh_action_can_start_after_read_cutoff(self):
        self.setup_ready()
        def late():
            if len(self.sent)==3:self.now=self.next-timedelta(seconds=110)
        self.on_action=late
        result=self.run_batch()
        self.assertEqual(result['attempted'],4);self.assertEqual(result['phase'],'batch_complete')
        self.assertLess(self.now,self.next)
        self.assertEqual(len(self.reads),1)

    def test_slow_ui_before_intent_closes_without_attempt_or_attention(self):
        self.setup_ready();self.state.set('action_retry_policy',1)
        real=self.execute;calls=[]
        def delayed(account,kind,row,**kw):
            calls.append(row['post_id'])
            if len(self.sent)==3:self.now=self.next-timedelta(seconds=59)
            return real(account,kind,row,**kw)
        self.execute=delayed
        def late():
            if len(self.sent)==3:self.now=self.next-timedelta(seconds=110)
        self.on_action=late
        result=self.run_batch()
        self.assertEqual(len(calls),4);self.assertEqual(result['attempted'],3)
        self.assertEqual(result['phase'],'batch_action_deadline')
        self.assertIsNone(self.state.get('paused'))
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],4)
        last=self.state.db.execute('SELECT attempt_id FROM scheduler_jobs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertIsNone(last[0])

    def test_retry_after_read_cutoff_still_respects_action_window_and_capacity(self):
        self.setup_ready();self.state.set('action_retry_policy',1)
        real=self.execute;failed=False
        def retry(account,kind,row,**kw):
            nonlocal failed
            result=real(account,kind,row,**kw)
            if not failed:
                failed=True
                with closing(engine.db_open(self.paths[account])) as db:
                    db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=? AND post_id=?",(row['group_id'],row['post_id']));db.commit()
                self.now=self.next-timedelta(seconds=110)
                raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            return result
        self.execute=retry;result=self.run_batch()
        self.assertEqual(result['attempted'],4)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_action_passes WHERE job_id=1').fetchone()[0],2)
        self.assertEqual(result['phase'],'batch_complete')

    def test_resume_gate_skips_all_due_slots_until_strictly_future_saved_start(self):
        self.setup_worker();self.plan_gap()
        self.state.set('resume_not_before',self.next.timestamp())
        self.worker.scan_step=Mock(return_value=None)
        with patch.object(self.worker,'run_batch',return_value={'phase':'fixture'}) as run:
            self.assertEqual(self.worker.tick(execute=True)['phase'],'waiting_batch');run.assert_not_called()
            self.now=self.next;self.worker.tick(execute=True);run.assert_called_once_with(1)

    def test_slow_baseline_read_cannot_reserve_with_stale_timestamp(self):
        self.setup_ready();self.state._batch_boundary=self.next;self.state._batch_clock=lambda:self.now
        row={'group_id':101,'post_id':11,'published_at':self.dates[(101,11)]}
        job=self.state.create_job(self.now,0,1,'like',0,row)
        self.now=self.next-timedelta(seconds=61)
        original=self.state.account_db;calls=0
        @contextmanager
        def slow(account):
            nonlocal calls
            calls+=1
            with original(account) as db:
                if calls==4:self.now+=timedelta(seconds=30)
                yield db
        with patch.object(self.state,'account_db',side_effect=slow):
            with self.assertRaises(RuntimeError):self.state.before_attempt(job,self.now)
        self.assertEqual(self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'dispatching')
        self.assertIsNone(self.state.get('last_action_batch'))

    def test_slow_guard_read_cannot_grant_click_after_batch_boundary(self):
        self.setup_ready();self.state._batch_boundary=self.next;self.state._batch_clock=lambda:self.now
        job=self.state.create_job(self.now,0,1,'like',0,{'group_id':101,'post_id':11,'published_at':self.dates[(101,11)]})
        self.state.before_attempt(job,self.now);self.now=self.next-timedelta(seconds=21)
        def slow():self.now+=timedelta(seconds=30);return False
        with patch.object(self.state,'blocked',side_effect=slow):
            with self.assertRaises(RuntimeError):self.state.click_timeout(job,self.now)

    def test_claim_rechecks_time_after_database_wait(self):
        self.setup_ready();self.state._batch_clock=lambda:self.now
        advanced=False
        def slow(sql):
            nonlocal advanced
            if not advanced and sql.startswith('SELECT started_at,finished_at'):
                advanced=True;self.now+=timedelta(seconds=121)
        self.state.db.set_trace_callback(slow)
        self.assertFalse(self.state.claim_batch(self.now,0))
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)


class ClaimedSpacingTests(unittest.TestCase):
    setUp=spacing.BatchSpacingWaitTests.setUp
    setup_batch=spacing.BatchSpacingWaitTests.setup_batch
    run_real_callbacks=spacing.BatchSpacingWaitTests.run_real_callbacks

    def test_owned_on_time_batch_waits_540_seconds_past_claim_grace(self):
        worker=self.setup_batch(at='2026-10-25T04:10:19+00:00',previous='2026-10-25T04:06:05+00:00',planned='2026-10-25T04:09:51+00:00')
        result,sent=self.run_real_callbacks(worker)
        self.assertEqual(result['attempted'],4)
        self.assertEqual(sent[0],datetime.fromisoformat('2026-10-25T04:15:05+00:00'))
        self.assertEqual(sum(self.sleeps),286)
        self.assertFalse(self.state.claim_batch(self.now+timedelta(minutes=15),1))

    def test_late_claim_cannot_turn_waiting_into_catchup(self):
        worker=self.setup_batch(at='2026-10-25T04:12:30+00:00',previous='2026-10-25T04:06:05+00:00',planned='2026-10-25T04:09:51+00:00')
        result,sent=self.run_real_callbacks(worker)
        self.assertEqual(sent,[]);self.assertEqual(result['phase'],'batch_already_claimed')

    def test_late_claim_is_rejected_even_when_spacing_is_already_ready(self):
        worker=self.setup_batch(at='2026-10-25T04:12:30+00:00',previous='2026-10-25T03:50:00+00:00',planned='2026-10-25T04:09:51+00:00')
        result,sent=self.run_real_callbacks(worker)
        self.assertEqual(sent,[]);self.assertEqual(result['phase'],'batch_already_claimed')

    def test_spacing_beyond_next_action_window_never_starts(self):
        worker=self.setup_batch(at='2026-10-25T04:10:19+00:00',previous='2026-10-25T04:10:10+00:00',planned='2026-10-25T04:09:51+00:00')
        result,sent=self.run_real_callbacks(worker)
        self.assertEqual(sent,[]);self.assertEqual(result['phase'],'batch_spacing_deadline')
