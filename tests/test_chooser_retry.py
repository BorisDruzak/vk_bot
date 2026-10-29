"""Only temporary ledgers/fake clocks; no browser, profile or VK action."""
import unittest
from unittest.mock import Mock,patch
from contextlib import closing
from datetime import timedelta
import vk_batch as engine
import vk_action_retry as retry
import vk_community_ui as ui
import vk_scheduler as scheduler
from tests import test_action_retry_state as fixtures


class ChooserRetryTests(unittest.TestCase):
    setUp=fixtures.RetryStateTests.setUp
    run_fake=fixtures.RetryStateTests.run_fake

    def pick(self,now):
        self.seq+=1
        return 1,'community',self.state.config['accounts']['1']['communities'][0],{
            'group_id':101,'post_id':self.seq,'published_at':int(now.timestamp())-1}

    def test_initial_wait_has_typed_timeout_and_platform_guard_precedes_it(self):
        page=Mock()
        with patch.object(ui.engine,'ensure_ok'),patch.object(ui.time,'monotonic',side_effect=[0,31]):
            with self.assertRaises(ui.CommunityPreparationTimeout):
                ui._wait(page,lambda:False,'fixed',timeout_ms=30000,timeout_error=ui.CommunityPreparationTimeout)
        page.wait_for_timeout.assert_not_called()
        with patch.object(ui.engine,'ensure_ok',side_effect=engine.StopRun('guard')):
            with self.assertRaises(engine.StopRun):
                ui._wait(page,lambda:False,'fixed',timeout_ms=0,timeout_error=ui.CommunityPreparationTimeout)

    def test_pre_intent_chooser_timeout_gets_one_retry_then_next_action(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id'])
            if row['post_id']==1:raise ui.CommunityPreparationTimeout()
            return {'status':'already','attempt_id':None}
        result=self.run_fake(execute)
        self.assertEqual(self.calls[:3],[1,1,2])
        first=retry.RetryLedger(self.state).job_snapshot(1)
        self.assertEqual(first['passes'],2);self.assertEqual(first['state'],'held')
        self.assertTrue(all(p['attempt_id'] is None for p in first['attempts']))
        self.assertEqual(result['attempted'],0);self.assertIsNone(self.state.get('paused'))
        self.assertEqual(self.state.get('action_failure_streak'),0)  # next success resets

    def test_third_failed_logical_chooser_stops_after_six_preparations_no_attempts(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);raise ui.CommunityPreparationTimeout()
        result=self.run_fake(execute)
        self.assertEqual(self.calls,[1,1,2,2,3,3]);self.assertEqual(result['phase'],'attention')
        self.assertEqual(self.state.get('paused'),'action_failures_repeated')
        self.assertEqual(self.state.get('action_failure_streak'),3)
        self.assertEqual(sum(self.state.usage(self.now).values()),0)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM scheduler_jobs').fetchone()[0],3)

    def test_ordinary_prepare_guard_still_stops_immediately(self):
        import vk_scheduled_action as actions
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id'])
            try:raise engine.StopRun('wrong source or destination')
            except engine.StopRun as cause:raise actions.ScheduledActionStopped('community_prepare','fixed') from cause
        self.assertEqual(self.run_fake(execute)['phase'],'attention')
        self.assertEqual(self.calls,[1]);self.assertEqual(self.state.get('action_failure_streak',0),0)

    def test_timeout_after_durable_intent_cannot_retry(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);kw['before_attempt']();raise ui.CommunityPreparationTimeout()
        self.assertEqual(self.run_fake(execute)['phase'],'attention');self.assertEqual(self.calls,[1])
        self.assertEqual(sum(self.state.usage(self.now).values()),1)

    def test_hidden_pass_intent_is_also_not_retryable(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id'])
            self.state.db.execute('UPDATE scheduler_action_passes SET intent=1');self.state.db.commit()
            raise ui.CommunityPreparationTimeout()
        self.assertEqual(self.run_fake(execute)['phase'],'attention')
        self.assertEqual(self.calls,[1])

    def test_timeout_at_cutoff_keeps_only_remaining_pass_pending_across_restart(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);self.now=self.worker.batch_boundary(1)-timedelta(seconds=59)
            raise ui.CommunityPreparationTimeout()
        result=self.run_fake(execute)
        self.assertEqual(result['phase'],'batch_action_deadline');self.assertEqual(self.calls,[1])
        snap=retry.RetryLedger(self.state).job_snapshot(1)
        self.assertEqual((snap['state'],snap['passes']),('pending',1))
        self.state.recover()
        self.assertEqual(retry.RetryLedger(self.state).job_snapshot(1)['state'],'pending')
        self.assertIsNone(self.state.get('paused'))

    def test_exact_kind_type_stage_and_no_prior_attempt_required(self):
        err=ui.CommunityPreparationTimeout()
        job={'kind':'community','status':'dispatching','attempt_id':None}
        self.assertTrue(retry.retryable(err,job))
        for changed in [dict(job,kind='like'),dict(job,status='attempting'),dict(job,attempt_id=12)]:
            self.assertFalse(retry.retryable(err,changed))
        err.stage='cleanup';self.assertFalse(retry.retryable(err,job))
        class Derived(ui.CommunityPreparationTimeout):pass
        self.assertFalse(retry.retryable(Derived(),job))

    def test_timeout_with_nested_platform_protection_is_not_retryable(self):
        job={'kind':'community','status':'dispatching','attempt_id':None}
        try:
            try:raise engine.PlatformRateLimited('fixed')
            except engine.PlatformRateLimited as cause:raise ui.CommunityPreparationTimeout() from cause
        except ui.CommunityPreparationTimeout as err:self.assertFalse(retry.retryable(err,job))

    def test_policy_off_does_not_gain_legacy_technical_recovery(self):
        self.state.set('action_retry_policy',0)
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);raise ui.CommunityPreparationTimeout()
        self.assertEqual(self.run_fake(execute)['phase'],'attention')
        self.assertEqual(self.calls,[1])

    def test_diagnostic_reason_is_fixed_and_safe(self):
        import vk_diagnostics as diagnostics
        err=ui.CommunityPreparationTimeout()
        self.assertEqual(scheduler.safe_failure_reason(err),'community_prepare_timeout')
        record=diagnostics.make_record('action_failed',reason=scheduler.safe_failure_reason(err),error=err)
        self.assertEqual(record['reason'],'community_prepare_timeout')
        self.assertEqual(record['error_chain'][0]['type'],'CommunityPreparationTimeout')


if __name__=='__main__':unittest.main()
