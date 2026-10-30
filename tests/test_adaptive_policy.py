import unittest
from datetime import datetime,timezone,timedelta
import vk_schedule_policy as policy


class AdaptivePolicyTests(unittest.TestCase):
    def test_full_calendar_day_is_opt_in(self):
        cfg=policy.validate_config({'schedule_mode':'adaptive','start_hour':0,'end_hour':24,'batch_rest_seconds':540})
        now=datetime(2026,10,30,18,59,tzinfo=timezone.utc)
        start,end=policy.work_bounds(now,cfg)
        self.assertEqual((end-start).total_seconds(),86400)
        self.assertEqual(end,datetime(2026,10,30,19,tzinfo=timezone.utc))
        self.assertTrue(policy.in_work_window(now,cfg))
        self.assertEqual(policy.local_day(end),policy.local_day(now)+timedelta(days=1))

    def test_dynamic_target_is_not_limited_to_six_or_67(self):
        cfg=policy.validate_config({'schedule_mode':'adaptive','start_hour':0,'end_hour':24,'batch_size':30})
        self.assertEqual(cfg['batch_size'],30)
        self.assertNotIn('planned_batches',cfg)

    def test_legacy_defaults_and_limits_are_retained(self):
        self.assertEqual(policy.validate_config(),policy.DEFAULT_CONFIG)
        with self.assertRaises(ValueError):policy.validate_config({'start_hour':0,'end_hour':24})
        with self.assertRaises(ValueError):policy.validate_config({'batch_size':7})

    def test_invalid_rest_and_mode_rejected(self):
        for change in ({'batch_rest_seconds':539},{'batch_rest_seconds':True},{'schedule_mode':'unknown'},{'end_hour':25}):
            with self.subTest(change=change),self.assertRaises(ValueError):policy.validate_config({'schedule_mode':'adaptive','start_hour':0,'end_hour':24,**change})
