"""Reserve refresh timing with temporary ledgers and simulated clock/HTTP only."""
import json
import unittest
from contextlib import closing, nullcontext
from datetime import timedelta
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_schedule_policy as policy
import vk_scheduler as scheduler
from tests import test_reserve_fill as fixtures


class ReserveDeadlineTests(unittest.TestCase):
    setUp=fixtures.ReserveFillTests.setUp
    setup_worker=fixtures.ReserveFillTests.setup_worker
    item=fixtures.ReserveFillTests.item
    transport=fixtures.ReserveFillTests.transport
    sleep=fixtures.ReserveFillTests.sleep
    execute=fixtures.ReserveFillTests.execute

    def plan_gap(self,seconds=585):
        self.start=self.now;self.next=self.start+timedelta(seconds=seconds)
        self.state.db.execute('UPDATE schedule_days SET slots=?',
            (json.dumps([self.start.isoformat(),self.next.isoformat()]),));self.state.db.commit()
        self.plan_before=[tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')]

    def run_batch(self,slot=0):
        real=engagement.refresh
        def refresh(*a,**kw):return real(*a,**kw,transport=self.transport,sleeper=self.sleep)
        with patch.object(api,'configured_token',return_value='offline'), \
             patch.object(engagement.app,'exclusive_run',return_value=nullcontext()), \
             patch.object(engagement,'refresh',side_effect=refresh), \
             patch.object(scheduler.actions,'execute_action',side_effect=self.execute):
            result=self.worker.run_batch(slot)
        self.assertEqual([tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')],self.plan_before)
        return result

    def expand_reserve(self):
        for gid in range(103,158):
            self.cfg['groups'].append(dict(id=gid,enabled=True,like_enabled=True,repost_enabled=True))
            self.dates[(gid,21)]=self.dates[(102,21)]-gid;self.counts[(gid,21)]=10
            with closing(engine.db_open(self.paths[1])) as db:
                engagement._observe_page(db,gid,[self.item(gid,21,10)],int(self.now.timestamp()),
                    observed_at=(self.now-timedelta(hours=1)).isoformat())
        self.worker.groups=self.cfg['groups'].copy()

    def test_57_group_reserve_scan_discovers_today_then_four_actions_without_second_scan(self):
        self.setup_worker();self.plan_gap();self.expand_reserve()
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('DELETE FROM api_seen_posts WHERE post_id=11')
            db.execute('DELETE FROM post_publication WHERE post_id=11');db.commit()
        self.on_action=lambda:self.sleep(25)  # 45s per action including existing20s
        result=self.run_batch()
        self.assertEqual(result['attempted'],4)
        self.assertEqual([r[2] for r in self.sent],[11,11,11,21])
        self.assertEqual(len(self.reads),57)
        self.assertLess(self.now,self.next)
        evidence=json.loads(self.state.db.execute('SELECT snapshot FROM scheduler_priority_evidence ORDER BY job_id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(evidence['refresh_started_at'],self.start.isoformat())
        self.assertGreaterEqual(evidence['observed_at'],evidence['refresh_started_at'])
        self.assertEqual(self.state.get('last_counter_refresh')['selected_band'],'mixed')
        self.now=self.next
        self.assertEqual(policy.due_slot(self.now,self.state.day_plan(self.now),config=self.state.config),1)
        second=self.run_batch(1)
        self.assertEqual(second['attempted'],4)
        rows=self.state.db.execute('SELECT slot,finished_at,status FROM scheduler_batches ORDER BY slot').fetchall()
        self.assertEqual([(r[0],r[2]) for r in rows],[(0,'batch_complete'),(1,'batch_complete')])
        for old,new in zip(self.sent,self.sent[1:]):self.assertGreaterEqual((new[3]-old[3]).total_seconds(),20)
        self.assertGreaterEqual((self.sent[4][3]-self.sent[0][3]).total_seconds(),540)

    def test_no_reserve_http_when_next_slot_leaves_no_request_headroom(self):
        self.setup_worker();self.plan_gap()
        def late():
            if len(self.sent)==3:self.now=self.next-timedelta(seconds=130)
        self.on_action=late
        result=self.run_batch()
        self.assertEqual(result['phase'],'reserve_counter_refresh_deadline')
        self.assertEqual(len(self.sent),3);self.assertEqual(len(self.reads),1)
        self.assertLess(self.now,self.next)
        with self.state.account_db(1) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],1)

    def test_deadline_after_pacing_spends_no_next_request_or_partial_pool(self):
        self.setup_worker();self.plan_gap()
        def late():
            if len(self.sent)==3:self.now=self.next-timedelta(seconds=145)
        self.on_action=late
        def read(p):
            if len(self.reads)>1:self.now+=timedelta(seconds=1)
        self.on_read=read
        result=self.run_batch()
        self.assertEqual(result['phase'],'reserve_counter_refresh_deadline')
        self.assertEqual(len(self.sent),3);self.assertEqual(len(self.reads),2)
        self.assertLess(self.now,self.next)
        self.assertEqual(self.state.get('last_counter_refresh')['eligible_posts'],0)
        self.assertIsNone(self.state.get('paused'))
        with self.state.account_db(1) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],2)

    def test_slow_pick_after_refill_deadline_does_not_create_fourth_job(self):
        self.setup_worker();self.plan_gap()
        pick=self.worker.pick
        def slow(now):
            result=pick(now)
            if len(self.sent)==3 and self.worker._batch_refresh.get('selected_band')=='mixed':
                self.now=self.next-timedelta(seconds=59)
            return result
        with patch.object(self.worker,'pick',side_effect=slow):result=self.run_batch()
        self.assertEqual(result['phase'],'batch_action_deadline')
        self.assertEqual(len(self.sent),3)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],3)

    def test_last_slot_uses_work_window_end_for_reserve_cutoff(self):
        self.setup_worker();self.plan_gap()
        self.state.db.execute('UPDATE schedule_days SET slots=?',(json.dumps([self.start.isoformat()]),));self.state.db.commit()
        self.plan_before=[tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')]
        self.assertEqual(self.worker.reserve_refresh_deadline(0),policy.work_bounds(self.now,self.state.config)[1]-timedelta(seconds=120))

    def test_initial_reserve_scan_also_discards_partial_pool_at_cutoff(self):
        self.setup_worker();self.plan_gap()
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('DELETE FROM api_seen_posts WHERE post_id=11')
            db.execute('DELETE FROM post_publication WHERE post_id=11');db.commit()
        def late(p):self.now=self.next-timedelta(seconds=130)
        self.on_read=late
        result=self.run_batch()
        self.assertEqual(result['phase'],'counter_refresh_deadline')
        self.assertEqual(self.sent,[]);self.assertEqual(len(self.reads),1)

    def test_fourth_completed_attempt_stays_complete_after_cutoff(self):
        self.setup_worker();self.plan_gap()
        def late():
            if len(self.sent)==4:self.now=self.next-timedelta(seconds=119)
        self.on_action=late
        result=self.run_batch()
        self.assertEqual(result['attempted'],4);self.assertEqual(result['phase'],'batch_complete')

    def test_immediate_retry_is_pending_when_first_pass_finishes_after_cutoff(self):
        self.setup_worker();self.plan_gap();self.state.set('action_retry_policy',1)
        original=self.execute
        def fail(account,kind,row,**kw):
            original(account,kind,row,**kw)
            with closing(engine.db_open(self.paths[account])) as db:
                db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=? AND post_id=?",
                           (row['group_id'],row['post_id']));db.commit()
            self.now=self.next-timedelta(seconds=59)
            raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
        self.execute=fail
        self.run_batch()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.state.db.execute('SELECT state FROM scheduler_action_recovery').fetchone()[0],'pending')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_action_passes').fetchone()[0],1)

    def test_transport_timeout_preserves_backoff_and_third_failure_attention(self):
        self.setup_worker();self.plan_gap()
        slots=[self.start,self.next,self.next+timedelta(seconds=600)]
        self.state.db.execute('UPDATE schedule_days SET slots=?',(json.dumps([s.isoformat() for s in slots]),));self.state.db.commit()
        self.plan_before=[tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')]
        def timeout(p):raise api.VkApiReadDeadline('offline timeout')
        self.on_read=timeout
        for index,at in enumerate(slots):
            self.now=at;result=self.run_batch(index)
            self.assertEqual(self.state.get('technical_failures')['read'],index+1)
            self.assertEqual(result['phase'],'attention' if index==2 else 'technical_error_deferred')
        self.assertEqual(self.state.get('paused'),'technical_error_repeated')
        self.assertEqual(self.sent,[])

    def test_pre_intent_retry_cutoff_uses_whole_batch_attempt_count(self):
        self.setup_worker();self.plan_gap();self.state.set('action_retry_policy',1)
        original=self.execute
        def late_failure(account,kind,row,**kw):
            if len(self.sent)==3:
                self.now=self.next-timedelta(seconds=59)
                raise OSError('offline failure before intent')
            return original(account,kind,row,**kw)
        self.execute=late_failure
        result=self.run_batch()
        self.assertEqual(result['attempted'],3)
        self.assertEqual(result['phase'],'batch_action_deadline')


if __name__=='__main__':unittest.main()
