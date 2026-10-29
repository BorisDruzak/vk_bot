"""Explicit saved slot counts distribute quotas without changing legacy plans."""
from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
import random
import unittest

import vk_schedule_policy as policy


def configured(total=390, count=67):
    accounts={str(n):{'likes':50,'personal_reposts':10,'community_reposts':20,
                     'communities':[n*10+1,n*10+2]} for n in (1,2,5,6,7)}
    accounts['7']['personal_reposts']=total-390
    return {'start_hour':9,'end_hour':22,'batch_size':6,
            'planned_batches':count,'accounts':accounts}


class PlannedBatchPolicyTests(unittest.TestCase):
    def test_explicit67_distributes390_as55_sixes_and12_fives(self):
        source=configured();before=deepcopy(source)
        cfg=policy.validate_config(source)
        self.assertEqual(policy.batch_count(cfg),67)
        limits=[policy.batch_attempt_limit(cfg,i) for i in range(67)]
        self.assertEqual(Counter(limits),{6:55,5:12})
        self.assertEqual(sum(limits),390)
        self.assertEqual(policy.daily_total(cfg),390)
        self.assertEqual(source,before)
        self.assertEqual(cfg['accounts'],source['accounts'])

    def test_explicit67_distributes400_as65_sixes_and2_fives(self):
        cfg=configured(total=400)
        limits=[policy.batch_attempt_limit(cfg,i) for i in range(67)]
        self.assertEqual(Counter(limits),{6:65,5:2})
        self.assertEqual(sum(limits),400)

    def test_absent_count_retains_default_shape_and_legacy_full_slot_limits(self):
        self.assertNotIn('planned_batches',policy.DEFAULT_CONFIG)
        self.assertEqual(policy.validate_config(),policy.DEFAULT_CONFIG)
        for cfg in (None,{'end_hour':22,'batch_size':6}):
            validated=policy.validate_config(cfg)
            self.assertNotIn('planned_batches',validated)
            count=policy.batch_count(validated)
            self.assertTrue(all(policy.batch_attempt_limit(validated,i)==validated['batch_size']
                                for i in range(count)))
        source=configured();source.pop('planned_batches')
        self.assertEqual(policy.batch_count(source),65)

    def test_invalid_explicit_counts_are_rejected(self):
        for count in (0,-1,True,False,67.0,'67',None,64,391):
            with self.subTest(count=count),self.assertRaises(ValueError):
                policy.validate_config(configured(count=count))
        narrow=configured();narrow['end_hour']=18
        with self.assertRaises(ValueError):policy.validate_config(narrow)
        # 85 starts nominally fit13h but unique540+ gaps and finish reserve do not.
        with self.assertRaises(ValueError):policy.validate_config(configured(count=85))

    def test_slot_index_must_be_an_exact_integer_inside_the_saved_count(self):
        for cfg in (configured(),None):
            for slot in (-1,policy.batch_count(cfg),True,False,0.0,'0',None):
                with self.subTest(cfg=cfg,slot=slot),self.assertRaises(ValueError):
                    policy.batch_attempt_limit(cfg,slot)

    def test_100_seeded67_plans_preserve_unique540_gaps_and_finish_reserve(self):
        cfg=configured()
        for seed in range(100):
            with self.subTest(seed=seed):
                slots=policy.build_schedule(date(2026,10,30),cfg,rng=random.Random(seed))
                start,end=policy.work_bounds(slots[0],cfg)
                gaps=[int((b-a).total_seconds()) for a,b in zip(slots,slots[1:])]
                self.assertEqual(len(slots),67)
                self.assertEqual(len(set(gaps)),66)
                self.assertGreaterEqual(min(gaps),540)
                self.assertGreaterEqual(slots[0],start)
                self.assertLessEqual(slots[-1],end-timedelta(seconds=600))
                self.assertEqual(policy.due_slot(slots[40],slots,config=cfg),40)
                self.assertIsNone(policy.due_slot(slots[40]+timedelta(seconds=121),slots,config=cfg))


if __name__=='__main__':unittest.main()
