from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from vk_batch import (db_open, actions_24h, SafetyBudget, StopRun,
                      lock_on_platform_warning, safety_lock_state)

GROUP = 188206921

class SafetyGateTests(unittest.TestCase):
    def test_limits_are_persistent_and_attempt_is_booked_before_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'safe.db'
            with closing(db_open(path)) as db:
                budget=SafetyBudget(db, GROUP, per_run=2, per_24h=3)
                budget.reserve(13376, 'like')
                budget.reserve(13376, 'repost')
                self.assertEqual(actions_24h(db, GROUP),2)
                with self.assertRaisesRegex(StopRun,'за запуск'):
                    budget.reserve(13365,'like')
                self.assertEqual(actions_24h(db,GROUP),2)
            with closing(db_open(path)) as db:
                budget=SafetyBudget(db,GROUP,per_run=2,per_24h=3)
                budget.reserve(13365,'like')
                self.assertEqual(actions_24h(db,GROUP),3)
                with self.assertRaisesRegex(StopRun,'за 24 часа'):
                    SafetyBudget(db,GROUP,per_run=2,per_24h=3).reserve(13364,'like')
                self.assertEqual(actions_24h(db,GROUP),3)

    def test_warning_latches_across_restarts_until_manual_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'safe.db'
            with closing(db_open(path)) as db:
                lock_on_platform_warning(db,GROUP,'VK сообщил об ограничении')
            with closing(db_open(path)) as db:
                self.assertIsNotNone(safety_lock_state(db,GROUP))
                with self.assertRaisesRegex(StopRun,'блокировка'):
                    SafetyBudget(db,GROUP,2,4)
                db.execute('DELETE FROM safety_lock WHERE group_id=0')
                db.commit()
                self.assertIsNone(safety_lock_state(db,GROUP))
                SafetyBudget(db,GROUP,2,4).reserve(13376,'like')
                self.assertEqual(actions_24h(db,GROUP),1)

    def test_budget_never_logs_a_failed_limit_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(db_open(Path(tmp)/'db.sqlite')) as db:
                b=SafetyBudget(db,GROUP,1,1)
                with self.assertRaises(ValueError):
                    b.reserve(13376,'comment')
                self.assertEqual(actions_24h(db,GROUP),0)
                b.reserve(13376,'like')
                with self.assertRaises(StopRun):
                    b.reserve(13376,'repost')
                self.assertEqual(actions_24h(db,GROUP),1)

if __name__=='__main__':
    unittest.main()

class SafetyActionStateTests(unittest.TestCase):
    def test_action_budget_exhausted_before_click_preserves_pending(self):
        from unittest.mock import MagicMock
        from vk_batch import add_posts, target_row, process_one, SafetyLimitReached
        with tempfile.TemporaryDirectory() as tmp:
            with closing(db_open(Path(tmp)/'db.sqlite')) as db:
                add_posts(db,GROUP,[13376])
                budget=SafetyBudget(db,GROUP,per_run=1,per_24h=1)
                budget.reserve(13354,'like')
                row=target_row(db,GROUP,13376)
                source={'liked':False,'reposted':False,'source':'inline',
                        'canLike':True,'canRepost':True}
                with patch('vk_batch.go_to_post'), \
                     patch('vk_batch.refresh_state',return_value=source), \
                     patch('vk_batch.wait_post_footer_info',return_value={'liked':False}), \
                     patch('vk_batch.verified_like_button',return_value=MagicMock()), \
                     patch('vk_batch.ensure_ok'), \
                     patch('vk_batch.click_like') as click:
                    with self.assertRaises(SafetyLimitReached):
                        process_one(MagicMock(),db,GROUP,row,0,safety=budget)
                    click.assert_not_called()
                fresh=target_row(db,GROUP,13376)
                self.assertEqual((fresh['like_status'],fresh['repost_status']),('pending','pending'))
                self.assertEqual(actions_24h(db,GROUP),1)

class CrossGroupSafetyTests(unittest.TestCase):
    def test_account_budget_and_warning_are_global_across_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(db_open(Path(tmp)/'db.sqlite')) as db:
                SafetyBudget(db,GROUP,per_run=2,per_24h=1).reserve(13376,'like')
                with self.assertRaisesRegex(StopRun,'за 24 часа'):
                    SafetyBudget(db,123456789,per_run=2,per_24h=1).reserve(999,'like')
                lock_on_platform_warning(db,GROUP,'VK сообщил об ограничении')
                self.assertIsNotNone(safety_lock_state(db,123456789))
                with self.assertRaisesRegex(StopRun,'блокировка'):
                    SafetyBudget(db,123456789,per_run=2,per_24h=4)
