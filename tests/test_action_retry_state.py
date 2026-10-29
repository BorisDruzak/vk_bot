"""Recovery uses temporary actor/scheduler ledgers; no browser or VK calls."""
import io
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import vk_batch as engine
import vk_scheduler as scheduler
import vk_scheduler_state as ledger


class RetryStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.paths={n:self.root/f'{n}.sqlite3' for n in (1,2)}
        self.now=datetime(2026,10,23,5,tzinfo=timezone.utc)
        for path in self.paths.values():
            with closing(engine.db_open(path)) as db:engine.add_posts(db,101,list(range(1,30)))
        self.state=ledger.State(self.root/'scheduler.sqlite3',self.paths);self.addCleanup(self.state.close)
        self.state.set('action_retry_policy',1)
        self.worker=scheduler.Worker(self.state,self.paths[1],{'groups':[{'id':101,'enabled':True}]},clock=lambda:self.now)
        self.worker.refresh_batch=Mock(return_value={'state':'ready','pool':None,'report':{}})
        self.output=redirect_stdout(io.StringIO());self.output.__enter__();self.addCleanup(self.output.__exit__,None,None,None)
        self.seq=0;self.calls=[]

    def pick(self,now):
        self.seq+=1
        return 1,'like',0,{'group_id':101,'post_id':self.seq,'published_at':int(now.timestamp())-1}

    def reserve(self,row,kw):
        kw['before_attempt']()
        with closing(engine.db_open(self.paths[1])) as db:
            aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",
                (row['post_id'],self.now.isoformat())).lastrowid
            db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
        kw['record_attempt'](aid)
        return aid

    def run_fake(self,execute,slot=1):
        from tests.batch_fixtures import save_plan
        if not self.state.db.execute('SELECT 1 FROM schedule_days').fetchone():save_plan(self.state,self.now)
        with patch.object(self.worker,'pick',side_effect=self.pick),patch.object(scheduler.actions,'execute_action',side_effect=execute):
            return self.worker.run_batch(slot)

    def test_one_retry_counts_both_attempts_in_four_and_quota(self):
        def execute(a,k,row,**kw):
            retry=kw.get('recovery_attempt_id');self.calls.append((row['post_id'],retry))
            aid=self.reserve(row,kw)
            if retry is None:raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
            return {'status':'verified','attempt_id':aid}
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],4)
        self.assertEqual([p for p,_ in self.calls],[1,1,2,2])
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],46)
        self.assertFalse(self.state.blocked())
        import vk_action_retry as retry
        snap=retry.RetryLedger(self.state).job_snapshot(1)
        self.assertEqual([r['attempt_id'] for r in snap['attempts']],[1,2])
        self.assertEqual([r['outcome'] for r in snap['attempts']],['uncertain','verified'])

    def test_failed_retry_continues_then_third_failed_logical_action_stops(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);self.reserve(row,kw)
            raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
        first=self.run_fake(execute)
        self.assertEqual(first['attempted'],4);self.assertEqual(self.calls,[1,1,2,2])
        self.assertFalse(self.state.blocked())
        self.now+=timedelta(minutes=20)
        second=self.run_fake(execute,2)
        self.assertEqual(second['attempted'],2);self.assertEqual(self.calls,[1,1,2,2,3,3])
        self.assertEqual(second['phase'],'attention')
        self.assertEqual(self.state.get('paused'),'action_failures_repeated')
        self.assertEqual(self.state.get('action_failure_streak'),3)
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM scheduler_jobs WHERE status='uncertain'").fetchone()[0],3)

    def test_success_resets_logical_streak_only(self):
        self.state.set('action_failure_streak',2);self.state.set('technical_failures',{'read':2})
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id'])
            return {'status':'already','attempt_id':None}
        self.run_fake(execute)
        self.assertEqual(self.state.get('action_failure_streak'),0)
        self.assertEqual(self.state.get('technical_failures')['read'],2)

    def test_pending_retry_survives_restart_and_moves_to_next_normal_batch(self):
        def execute(a,k,row,**kw):
            aid=self.reserve(row,kw)
            if row['post_id']==4 and kw.get('recovery_attempt_id') is None:
                raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
            return {'status':'verified','attempt_id':aid}
        self.assertEqual(self.run_fake(execute)['attempted'],4)
        import vk_action_retry as retry
        self.assertEqual(retry.RetryLedger(self.state).job_snapshot(4)['state'],'pending')
        self.state.recover();self.assertFalse(self.state.blocked())
        self.now+=timedelta(minutes=20)
        seen=[]
        def recovering(a,k,row,**kw):
            seen.append((row['post_id'],kw.get('recovery_attempt_id')))
            return execute(a,k,row,**kw)
        self.run_fake(recovering,2)
        self.assertEqual(seen[0],(4,4));self.assertEqual(retry.RetryLedger(self.state).job_snapshot(4)['passes'],2)

    def test_critical_guard_has_no_retry_and_stops_immediately(self):
        def execute(a,k,row,**kw):
            self.calls.append(1);raise engine.PlatformRateLimited('fixed')
        self.assertEqual(self.run_fake(execute)['phase'],'attention')
        self.assertEqual(len(self.calls),1);self.assertTrue(self.state.blocked())

    def test_intent_without_actor_attempt_is_not_retryable(self):
        def execute(a,k,row,**kw):
            self.calls.append(1);kw['before_attempt']();raise OSError('fixed')
        self.assertEqual(self.run_fake(execute)['phase'],'attention');self.assertEqual(self.calls,[1])
        self.assertEqual(sum(self.state.usage(self.now).values()),1)

    def test_pre_intent_technical_error_gets_exactly_one_retry(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);raise OSError('fixed')
        self.run_fake(execute)
        self.assertEqual(self.calls,[1,1,2,2,3,3]);self.assertEqual(self.state.get('action_failure_streak'),3)
        self.assertEqual(sum(self.state.usage(self.now).values()),0)

    def test_held_record_only_exempts_exact_matching_job(self):
        def execute(a,k,row,**kw):
            self.reserve(row,kw);raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
        self.run_fake(execute)
        self.assertEqual(self.state.active_uncertain_count(),0)
        self.state.db.execute("UPDATE scheduler_jobs SET reason='different' WHERE id=1");self.state.db.commit()
        self.assertEqual(self.state.active_uncertain_count(),1)

    def test_recovery_does_not_resume_running_retry_after_crash(self):
        import vk_action_retry as retry
        job=self.state.create_job(self.now,1,*self.pick(self.now))
        book=retry.RetryLedger(self.state);book.begin(job,self.now)
        book.before_attempt(job,self.now,1)
        with closing(engine.db_open(self.paths[1])) as db:
            aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,1,'like',?)",(self.now.isoformat(),)).lastrowid;db.commit()
        book.record_attempt(job,aid);book.fail_pass(job,'like_confirmation_timeout',self.now);book.pending(job,self.now)
        book.begin_retry(job,self.now)
        self.state.recover()
        self.assertEqual(book.job_snapshot(job)['state'],'interrupted')
        self.assertTrue(self.state.blocked())

    def test_readonly_summary_without_new_schema_never_writes(self):
        import vk_action_retry as retry
        self.state.db.execute('DROP TABLE scheduler_action_passes');self.state.db.execute('DROP TABLE scheduler_action_recovery');self.state.db.commit()
        self.state.db.execute('PRAGMA query_only=ON')
        self.assertEqual(retry.RetryLedger(self.state).summary(),{'enabled':True,'consecutive_failures':0,'held':0,'pending':0})
        self.assertIsNone(retry.RetryLedger(self.state).job_snapshot(1))

    def test_confirm_only_recovery_timeout_is_held_without_send(self):
        def execute(a,k,row,**kw):
            self.calls.append(kw.get('recovery_attempt_id'))
            if kw.get('recovery_attempt_id') is None:self.reserve(row,kw)
            raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
        result=self.run_fake(execute)
        self.assertEqual(result['phase'],'attention')
        self.assertEqual(self.state.get('action_failure_streak'),3)
        self.assertEqual(len(self.calls),6)

    def test_quota_exhausted_by_first_attempt_still_allows_readonly_recovery(self):
        self.state.config['accounts']['1']['likes']=1
        def execute(a,k,row,**kw):
            self.calls.append((kw.get('recovery_attempt_id'),kw.get('allow_retry_click')))
            if kw.get('recovery_attempt_id') is None:
                self.reserve(row,kw);raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            self.assertFalse(kw['allow_retry_click'])
            return {'status':'uncertain','attempt_id':None}
        self.run_fake(execute)
        self.assertEqual(self.calls,[(None,None),(1,False)])
        self.assertFalse(self.state.blocked());self.assertEqual(self.state.get('action_failure_streak'),1)

    def test_normal_stop_before_reservation_does_not_create_attention_pause(self):
        def execute(a,k,row,**kw):
            self.state.set('stop_requested',True);kw['before_attempt']()
        result=self.run_fake(execute)
        self.assertEqual(result['phase'],'batch_stop_requested')
        self.assertFalse(self.state.get('paused'));self.assertEqual(sum(self.state.usage(self.now).values()),0)

    def test_read_technical_recovery_accepts_exact_held_actions(self):
        def execute(a,k,row,**kw):
            self.reserve(row,kw);raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
        self.run_fake(execute)
        import vk_api_read as api
        self.assertEqual(self.worker.defer_technical(api.VkApiTransientRead('fixed'),'read'),'technical_error_deferred')

    def test_second_intent_without_actor_reservation_still_spends_second_quota(self):
        import vk_action_retry as retry
        job=self.state.create_job(self.now,1,*self.pick(self.now));book=retry.RetryLedger(self.state)
        book.begin(job,self.now);book.before_attempt(job,self.now,1)
        with closing(engine.db_open(self.paths[1])) as db:
            aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,1,'like',?)",(self.now.isoformat(),)).lastrowid;db.commit()
        book.record_attempt(job,aid);book.fail_pass(job,'like_confirmation_timeout',self.now);book.pending(job,self.now)
        book.begin_retry(job,self.now);book.before_attempt(job,self.now,1)
        self.state.recover()
        self.assertEqual(sum(self.state.usage(self.now).values()),2)
        self.assertTrue(self.state.blocked())

    def test_stop_during_pending_retry_keeps_exact_unused_second_pass_resumable(self):
        def execute(a,k,row,**kw):
            if kw.get('recovery_attempt_id') is None:
                self.reserve(row,kw);raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            self.state.set('stop_requested',True);kw['before_attempt']()
        self.assertEqual(self.run_fake(execute)['phase'],'batch_stop_requested')
        self.assertFalse(self.state.blocked())
        self.state.recover();self.state.set('stop_requested',False)
        self.assertFalse(self.state.blocked());self.now+=timedelta(minutes=20)
        seen=[]
        def success(a,k,row,**kw):
            seen.append(kw.get('recovery_attempt_id'))
            return {'status':'verified','attempt_id':None}
        self.run_fake(success,2)
        self.assertEqual(seen[0],1)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_action_passes WHERE job_id=1').fetchone()[0],2)

    def test_vk_denial_on_pre_intent_second_pass_is_a_skip_without_failure(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id'])
            if self.calls.count(row['post_id'])==1:raise OSError('fixed')
            return {'status':'skipped_vk','attempt_id':None,'skip_evidence':{
                'reason':'vk_like_denied','permission':'likes.can_like','value':False,'observations':2}}
        result=self.run_fake(execute)
        self.assertEqual(result['skipped_vk'],8)
        self.assertFalse(self.state.blocked());self.assertFalse(self.state.get('action_failure_streak',0))

    def test_typed_transport_only_recovers_matching_action_and_direct_browser_cause(self):
        import vk_action_retry as retry
        error=engine.ActionTransportError('like');error.stage='personal_action'
        error.__cause__=scheduler.actions.PlaywrightError('fixed')
        self.assertTrue(retry.retryable(error,{'status':'attempting','attempt_id':1,'kind':'like'}))
        self.assertFalse(retry.retryable(error,{'status':'attempting','attempt_id':1,'kind':'personal'}))
        error.__cause__=engine.StopRun('fixed')
        self.assertFalse(retry.retryable(error,{'status':'attempting','attempt_id':1,'kind':'like'}))

    def test_typed_chooser_transport_before_intent_is_retryable(self):
        import vk_action_retry as retry
        error=engine.ActionTransportError('repost');error.stage='personal_action'
        error.__cause__=scheduler.actions.PlaywrightError('fixed')
        self.assertTrue(retry.retryable(error,{'status':'dispatching','attempt_id':None,'kind':'personal'}))

    def test_expired_pending_is_counted_once_even_without_a_fresh_action_pool(self):
        import vk_action_retry as retry
        job=self.state.create_job(self.now,1,*self.pick(self.now));book=retry.RetryLedger(self.state)
        book.begin(job,self.now);book.fail_pass(job,'OSError',self.now);book.pending(job,self.now)
        self.now+=timedelta(days=1)
        with patch.object(self.worker,'scan_step',return_value=None),patch.object(self.worker,'pick',return_value=None):
            self.worker.tick(execute=True);self.worker.tick(execute=True)
        self.assertEqual(book.job_snapshot(job)['state'],'held')
        self.assertEqual(self.state.get('action_failure_streak'),1)


if __name__=='__main__':unittest.main()
