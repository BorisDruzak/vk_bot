"""Alias lookup belongs before intent; deadline never excuses an existing intent."""
import unittest
from contextlib import closing
from unittest.mock import patch
from datetime import datetime,timezone

import vk_accounts as accounts
import vk_batch as engine
import vk_scheduled_action as actions
from tests import test_action_retry_state as fixture


class AliasPreparationTests(unittest.TestCase):
    def test_numeric_header_does_not_spend_api_budget(self):
        with patch.object(accounts,'inspect_context',return_value=123), \
                patch.object(accounts,'prepare_context_identity') as prepare:
            actions.prime_action_identity(object())
        prepare.assert_not_called()

    def test_missing_numeric_header_prepares_once_with_deadline_and_existing_lock(self):
        context=object();deadline=datetime(2026,10,30,5,tzinfo=timezone.utc)
        before=lambda:None
        with patch.object(accounts,'inspect_context',return_value=None), \
                patch.object(accounts,'prepare_context_identity') as prepare:
            actions.prime_action_identity(context,deadline=deadline,before_request=before)
        prepare.assert_called_once_with(context,primary_lock_held=True,deadline=deadline,before_request=before)


class AliasRetryDeadlineTests(unittest.TestCase):
    setUp=fixture.RetryStateTests.setUp
    pick=fixture.RetryStateTests.pick
    reserve=fixture.RetryStateTests.reserve
    run_fake=fixture.RetryStateTests.run_fake

    def test_exhausted_quota_allows_identity_for_confirmation_but_not_another_attempt(self):
        self.state.config['accounts']['1']['likes']=1
        def execute(account,kind,row,**kw):
            kw['before_identity_read']()
            if kw.get('recovery_attempt_id') is None:
                self.reserve(row,kw)
                raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            self.assertFalse(kw['allow_retry_click'])
            with self.assertRaises(RuntimeError):kw['before_attempt']()
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
            return {'status':'verified','attempt_id':None}
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],1)
        job=self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()
        self.assertEqual(job[0],'verified')
        self.assertIsNone(self.state.get('paused'))
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],0)

    def test_identity_deadline_before_intent_defers_without_quota_or_pause(self):
        def execute(account,kind,row,**kw):
            self.assertEqual(kw['identity_read_deadline'],self.worker.batch_boundary(1)-__import__('datetime').timedelta(seconds=60))
            self.assertTrue(callable(kw['before_identity_read']))
            kw['before_identity_read']()
            raise accounts.IdentityPreparationDeadline('identity_preparation_deadline')
        result=self.run_fake(execute)
        self.assertEqual((result['attempted'],result['phase']),(0,'batch_action_deadline'))
        self.assertIsNone(self.state.get('paused'))
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],50)
        with self.state.account_db(1) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertFalse(self.state.blocked())

    def test_exception_after_intent_is_still_uncertain_and_attention(self):
        def execute(account,kind,row,**kw):
            self.reserve(row,kw)
            raise accounts.IdentityPreparationDeadline('identity_preparation_deadline')
        result=self.run_fake(execute)
        self.assertEqual((result['attempted'],result['phase']),(1,'attention'))
        self.assertIsNotNone(self.state.get('paused'))
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],49)


if __name__=='__main__':unittest.main()
