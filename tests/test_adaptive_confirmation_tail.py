"""Bounded read-only recovery at an adaptive attempt boundary; no live activity."""
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import vk_api_read as api
import vk_batch as engine
import vk_action_retry as retry
import vk_schedule_policy as policy
import vk_scheduler as scheduler
import vk_scheduler_state as ledger
from vk_adaptive_state import AdaptiveState
from vk_adaptive_worker import AdaptiveWorker


class AdaptiveConfirmationTailTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.actor=self.root/'actor.sqlite3'
        self.now=datetime(2026,10,30,5,tzinfo=timezone.utc)
        with closing(engine.db_open(self.actor)) as db:
            api.init_api(db);engine.add_posts(db,101,[11,99])
        self.cfg=policy.validate_config({'schedule_mode':'adaptive','start_hour':0,'end_hour':24,
            'accounts':{'1':{'likes':1,'personal_reposts':0,'community_reposts':0,'communities':[]},
                        '2':{'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[]}}})
        self.state=AdaptiveState(self.root/'scheduler.sqlite3',{1:self.actor},self.cfg)
        self.addCleanup(self.state.close);self.state.enabled_provider=lambda actor:actor==1
        self.state.set('action_retry_policy',1)
        self.worker=AdaptiveWorker(self.state,self.actor,{'groups':[{'id':101,'enabled':True,'like_enabled':True}]},clock=lambda:self.now)
        self.worker.refresh_batch=Mock(return_value={'state':'ready','pool':None,'report':{}})
        self.row={'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())-1}
        self.worker.pick=lambda now:(1,'like',0,self.row)
        self.calls=[]
    def first_uncertain(self, options):
        options['before_attempt']()
        with closing(engine.db_open(self.actor)) as db:
            aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,11,'like',?)",(self.now.isoformat(),)).lastrowid
            db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=101 AND post_id=11");db.commit()
        options['record_attempt'](aid);self.now+=timedelta(seconds=20)
        raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
    def confirm(self, actor, kind, row, **options):
        self.calls.append(options.get('recovery_attempt_id'))
        options['before_identity_read']()
        if options.get('recovery_attempt_id') is None:self.first_uncertain(options)
        self.assertFalse(options['allow_retry_click'])
        with self.assertRaises(RuntimeError):options['before_attempt']()
        with closing(engine.db_open(self.actor)) as db:
            db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=11");db.commit()
        self.now+=timedelta(seconds=20)
        return {'status':'verified','attempt_id':None}
    def tick(self, execute):
        with patch.object(scheduler.actions,'execute_action',side_effect=execute):
            return self.worker.tick(execute=True)
    def assert_single_attempt(self):
        self.assertEqual(self.state.usage(self.now)[(1,'like',0)],1)
        with self.state.account_db(1) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)

    def test_final_quota_one_confirms_in_same_target_one_without_a_new_attempt(self):
        result=self.tick(self.confirm)
        self.assertEqual(self.calls,[None,1])
        self.assertEqual(result['attempted'],1)
        self.assertEqual(self.state.db.execute('SELECT target_attempts FROM scheduler_adaptive_batches').fetchone()[0],1)
        self.assertEqual(self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'verified')
        self.assertEqual(retry.RetryLedger(self.state).summary()['pending'],0)
        self.assert_single_attempt()
        passes=self.state.db.execute('SELECT pass,intent,attempt_id FROM scheduler_action_passes ORDER BY pass').fetchall()
        self.assertEqual([tuple(row) for row in passes],[(1,1,1),(2,0,None)])

    def test_target_one_forces_confirmation_only_even_with_remaining_daily_quota(self):
        self.state.config['accounts']['1']['likes']=50
        result=self.tick(self.confirm)
        self.assertEqual(self.calls,[None,1])
        self.assertEqual(result['attempted'],1)
        self.assertEqual(self.state.db.execute('SELECT target_attempts FROM scheduler_adaptive_batches').fetchone()[0],1)
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],49)
        self.assert_single_attempt()

    def test_attempt_callback_cannot_create_another_intent_in_confirmation_tail(self):
        self.state.config['accounts']['1']['likes']=50
        def malicious(actor,kind,row,**options):
            self.calls.append(options.get('recovery_attempt_id'))
            if options.get('recovery_attempt_id') is None:self.first_uncertain(options)
            options['before_attempt']()
            self.fail('A full batch granted a second reservation')
        result=self.tick(malicious)
        self.assertEqual(self.calls,[None,1])
        self.assertEqual(result['phase'],'attention')
        self.assertEqual(self.state.db.execute('SELECT intent FROM scheduler_action_passes WHERE pass=2').fetchone()[0],0)
        self.assert_single_attempt()
        self.assertTrue(self.state.blocked())

    def test_unknown_actor_uncertainty_blocks_confirmation_tail(self):
        with closing(engine.db_open(self.actor)) as db:
            db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=101 AND post_id=99");db.commit()
        result=self.tick(self.confirm)
        self.assertEqual(self.calls,[None])
        self.assertEqual(result['phase'],'attention')
        self.assertTrue(self.state.blocked())
        self.assert_single_attempt()

    def test_tail_keeps_original_day_deadline_and_stop_guards(self):
        original=self.first_uncertain
        for stop in (True,False):
            with self.subTest(stop=stop):
                # Each branch gets a separate test fixture to preserve receipts.
                case=AdaptiveConfirmationTailTests('test_unknown_actor_uncertainty_blocks_confirmation_tail');case.setUp()
                try:
                    def first(actor,kind,row,**options):
                        case.calls.append(options.get('recovery_attempt_id'))
                        try:case.first_uncertain(options)
                        except engine.ActionConfirmationError:
                            if stop:case.state.set('stop_requested',True)
                            else:case.now=case.state.calendar_end(case.now)+timedelta(seconds=1)
                            raise
                    result=case.tick(first)
                    self.assertEqual(case.calls,[None])
                    self.assertIn(result['phase'],('batch_stop_requested','batch_action_deadline'))
                    self.assertEqual(case.state.db.execute('SELECT COUNT(*) FROM scheduler_action_passes').fetchone()[0],1)
                finally:case.doCleanups()

    def test_full_capacity_without_a_durable_attempt_keeps_technical_pass_pending(self):
        self.state.ensure_day(self.now);self.state.update_forecast(self.now)
        slot=self.state.next_slot(self.now);self.assertTrue(self.state.claim_batch(self.now,slot))
        job=self.state.create_job(self.now,slot,1,'like',0,self.row)
        with patch.object(scheduler.actions,'execute_action',side_effect=OSError('offline fixture')) as execute:
            result=retry.execute_job(self.worker,job,self.row,slot,0)
        self.assertEqual(execute.call_count,1)
        self.assertEqual(result['attempted'],0)
        self.assertEqual(retry.RetryLedger(self.state).summary()['pending'],1)
        self.assertEqual(self.state.db.execute('SELECT intent FROM scheduler_action_passes').fetchone()[0],0)

    def test_legacy_capacity_exhaustion_still_leaves_retry_pending(self):
        config={key:value for key,value in self.cfg.items() if key not in ('schedule_mode','batch_rest_seconds')}
        config.update(start_hour=9,end_hour=19)
        legacy=ledger.State(self.root/'legacy.sqlite3',{1:self.actor},config);self.addCleanup(legacy.close)
        legacy.enabled_provider=lambda actor:actor==1;legacy.set('action_retry_policy',1)
        worker=scheduler.Worker(legacy,self.actor,{'groups':[]},clock=lambda:self.now)
        original_state=self.state;self.state=legacy
        try:
            job=legacy.create_job(self.now,0,1,'like',0,self.row)
            def execute(actor,kind,row,**options):self.calls.append(options.get('recovery_attempt_id'));self.first_uncertain(options)
            with patch.object(scheduler.actions,'execute_action',side_effect=execute):
                result=retry.execute_job(worker,job,self.row,0,1)
            self.assertEqual(self.calls,[None]);self.assertEqual(result['attempted'],1)
            self.assertEqual(retry.RetryLedger(legacy).summary()['pending'],1)
        finally:self.state=original_state


if __name__=='__main__':unittest.main()
