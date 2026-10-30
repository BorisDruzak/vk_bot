"""Date skips preserve attempts and let unrelated work proceed."""
import unittest
from contextlib import closing
from unittest.mock import patch
import vk_batch as engine
import vk_scheduled_action as actions
import vk_action_retry as retry
from tests import test_action_retry_state as fixtures


class DateSkipSchedulerTests(unittest.TestCase):
    setUp=fixtures.RetryStateTests.setUp
    pick=fixtures.RetryStateTests.pick
    reserve=fixtures.RetryStateTests.reserve
    run_fake=fixtures.RetryStateTests.run_fake

    def verified(self,row,kw):
        aid=self.reserve(row,kw)
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
        return {'status':'verified','attempt_id':aid}

    def test_three_date_skips_do_not_pause_or_spend_quota(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id'])
            if row['post_id']<=3:raise actions.SourceDateUnavailable()
            return self.verified(row,kw)
        result=self.run_fake(execute)
        self.assertEqual(result['phase'],'batch_complete')
        self.assertEqual(result['attempted'],4)
        self.assertFalse(self.state.blocked())
        self.assertEqual(self.state.get('action_failure_streak',0),0)
        self.assertEqual(sum(self.state.usage(self.now).values()),4)
        self.assertEqual(self.calls,[1,2,3,4,5,6,7])

    def test_date_failure_after_real_attempt_does_not_resend_or_erase_spend(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);self.reserve(row,kw)
            raise actions.SourceDateUnavailable()
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],4)
        self.assertEqual(self.calls,[1,2,3,4])
        self.assertFalse(self.state.blocked())
        self.assertEqual(sum(self.state.usage(self.now).values()),4)
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM scheduler_jobs WHERE status='uncertain'").fetchone()[0],4)
        self.assertTrue(retry.actor_uncertainties_safe(self.state))
        self.state.recover();self.assertFalse(self.state.blocked())

    def test_date_failure_on_confirmation_pass_preserves_first_attempt(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id'])
            if kw.get('recovery_attempt_id') is None:
                self.reserve(row,kw);raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            raise actions.SourceDateUnavailable()
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],4)
        self.assertFalse(self.state.blocked())
        self.assertEqual(self.state.get('action_failure_streak',0),0)
        self.assertEqual(sum(self.state.usage(self.now).values()),4)

    def test_typed_skip_with_hidden_protection_is_not_exempted(self):
        def execute(a,k,row,**kw):
            try:raise engine.PlatformRateLimited('synthetic')
            except engine.PlatformRateLimited as cause:raise actions.SourceDateUnavailable() from cause
        self.assertEqual(self.run_fake(execute)['phase'],'attention')
        self.assertTrue(self.state.blocked())

    def test_modified_date_receipt_does_not_exempt_unrelated_uncertainty(self):
        def execute(a,k,row,**kw):self.reserve(row,kw);raise actions.SourceDateUnavailable()
        self.run_fake(execute);self.assertFalse(self.state.blocked())
        self.state.db.execute("UPDATE scheduler_jobs SET reason='different' WHERE id=1");self.state.db.commit()
        self.assertEqual(self.state.active_uncertain_count(),1)

    def test_extra_unreported_actor_attempt_blocks_instead_of_gaining_date_exemption(self):
        def execute(a,k,row,**kw):
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,29,'like',?)",(self.now.isoformat(),));db.commit()
            raise actions.SourceDateUnavailable()
        self.assertEqual(self.run_fake(execute)['phase'],'attention')
        self.assertTrue(self.state.blocked())

    def test_legacy_scheduler_also_skips_missing_dates_without_expanding_batch(self):
        self.state.set('action_retry_policy',0)
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);self.reserve(row,kw)
            raise actions.SourceDateUnavailable()
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],4)
        self.assertEqual(self.calls,[1,2,3,4])
        self.assertFalse(self.state.blocked())
        self.assertTrue(retry.actor_uncertainties_safe(self.state))

    def test_durable_intent_without_actor_attempt_still_spends_slot_and_quota(self):
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);kw['before_attempt']()
            raise actions.SourceDateUnavailable()
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],4)
        self.assertEqual(sum(self.state.usage(self.now).values()),4)
        self.assertFalse(self.state.blocked())
