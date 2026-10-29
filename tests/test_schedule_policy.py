"""Calendar/window and randomized daily timing rules; no I/O or real actions."""
import random
import unittest
from datetime import date, datetime, timedelta, timezone

import vk_schedule_policy as policy


UTC=timezone.utc


class SchedulePolicyTests(unittest.TestCase):
    def at(self,hour=4,minute=0,second=0):
        return datetime(2026,10,21,hour,minute,second,tzinfo=UTC)

    def test_default_daily_limits_and_nominal_spacing(self):
        self.assertEqual(policy.daily_total(),160)
        self.assertEqual(policy.batch_count(),40)
        self.assertEqual(policy.slot_seconds(),900)
        for account in policy.DEFAULT_CONFIG['accounts'].values():
            self.assertEqual((account['likes'],account['personal_reposts'],account['community_reposts']),(50,10,20))
            self.assertEqual(len(account['communities']),2)

    def test_three_full_accounts_fit_sixty_saved_four_action_batches(self):
        config={'accounts':{'5':{'likes':50,'personal_reposts':10,
                                'community_reposts':20,'communities':[501,502]}}}
        self.assertEqual(policy.daily_total(config),240)
        self.assertEqual(policy.batch_count(config),60)
        start,end=policy.work_bounds(self.at(),config)
        for seed in range(1000):
            with self.subTest(seed=seed):
                plan=policy.build_schedule(self.at(),config,rng=random.Random(seed))
                gaps=[int((b-a).total_seconds()) for a,b in zip(plan,plan[1:])]
                self.assertEqual(len(plan),60)
                self.assertGreaterEqual(plan[0],start)
                self.assertLessEqual(plan[-1],end-timedelta(minutes=10))
                self.assertEqual(len(set(gaps)),59)
                self.assertGreaterEqual(min(gaps),540)
                self.assertTrue(all(abs((slot-(start+timedelta(seconds=i*600))).total_seconds())<=120
                                    for i,slot in enumerate(plan)))
                self.assertEqual(policy.due_slot(plan[20],plan,config=config),20)
                self.assertIsNone(policy.due_slot(plan[20]+timedelta(seconds=121),plan,config=config))

    def test_three_accounts_constant_rng_remains_bounded(self):
        class ConstantRng:
            calls=0
            def randint(self,minimum,maximum):self.calls+=1;return 0
        rng=ConstantRng()
        cfg={'accounts':{'5':{'likes':50,'personal_reposts':10,'community_reposts':20,'communities':[501,502]}}}
        plan=policy.build_schedule(self.at(),cfg,rng=rng)
        self.assertEqual(len(plan),60)
        self.assertLessEqual(rng.calls,60*8)

    def test_calendar_day_uses_yekaterinburg_midnight(self):
        self.assertEqual(policy.local_day(self.at(18,59,59)),date(2026,10,21))
        self.assertEqual(policy.local_day(self.at(19)),date(2026,10,22))
        start,end=policy.day_bounds(self.at(4))
        self.assertEqual(datetime.fromtimestamp(start,UTC),datetime(2026,10,20,19,tzinfo=UTC))
        self.assertEqual(datetime.fromtimestamp(end,UTC),self.at(19))

    def test_window_is_nine_inclusive_nineteen_exclusive(self):
        self.assertFalse(policy.in_work_window(self.at(3,59,59)))
        self.assertTrue(policy.in_work_window(self.at(4)))
        self.assertTrue(policy.in_work_window(self.at(13,59,59)))
        self.assertFalse(policy.in_work_window(self.at(14)))
        self.assertEqual(policy.work_bounds(self.at(4)),(self.at(4),self.at(14)))

    def test_weekend_uses_same_daily_window(self):
        self.assertTrue(policy.in_work_window(datetime(2026,10,24,4,tzinfo=UTC)))

    def test_naive_datetimes_are_rejected(self):
        for method in (policy.local_day,policy.day_bounds,policy.in_work_window,policy.batch_slot,policy.next_wake):
            with self.subTest(method=method.__name__),self.assertRaises(ValueError):method(datetime(2026,10,21,9))

    def test_nominal_slots_cover_only_current_slot(self):
        self.assertIsNone(policy.batch_slot(self.at(3,59)))
        self.assertEqual(policy.batch_slot(self.at(4)),0)
        self.assertEqual(policy.batch_slot(self.at(8,47)),19)
        self.assertEqual(policy.batch_slot(self.at(13,59,59)),39)
        self.assertIsNone(policy.batch_slot(self.at(14)))

    def test_next_wake_is_future_slot_or_next_opening(self):
        self.assertEqual(policy.next_wake(self.at(3,59)),self.at(4))
        self.assertEqual(policy.next_wake(self.at(4)),self.at(4,15))
        self.assertEqual(policy.next_wake(self.at(13,59)),datetime(2026,10,22,4,tzinfo=UTC))
        self.assertEqual(policy.next_wake(self.at(14)),datetime(2026,10,22,4,tzinfo=UTC))

    def test_post_classification_current_day_and_previous_seven_days(self):
        now=self.at(8);start,end=policy.day_bounds(now)
        self.assertEqual(policy.classify_post(start,now),'new')
        self.assertEqual(policy.classify_post(int(now.timestamp()),now),'new')
        self.assertEqual(policy.classify_post(start-1,now),'old')
        self.assertEqual(policy.classify_post(start-7*86400,now),'old')
        self.assertIsNone(policy.classify_post(start-7*86400-1,now))
        self.assertIsNone(policy.classify_post(int(now.timestamp())+1,now))
        self.assertIsNone(policy.classify_post(True,now))

    def test_priority_is_newest_today_before_newest_reserve(self):
        now=self.at(8);start,_=policy.day_bounds(now)
        posts=[start-1,start+5,start-300,start+100]
        self.assertEqual(sorted(posts,key=lambda stamp:policy.priority_key(stamp,now)),[start+100,start+5,start-1,start-300])

    def test_low_engagement_orders_within_today_before_reserve(self):
        now=self.at(8);start,_=policy.day_bounds(now)
        posts=[(start+100,100,50),(start+5,1,0),(start-1,0,0)]
        result=sorted(posts,key=lambda row:policy.priority_key(row[0],now,like_count=row[1],repost_count=row[2]))
        self.assertEqual(result,[(start+5,1,0),(start+100,100,50),(start-1,0,0)])

    def test_unknown_engagement_sorts_after_known_in_same_band(self):
        now=self.at(8);start,_=policy.day_bounds(now)
        known=policy.priority_key(start+1,now,like_count=100,repost_count=50)
        unknown=policy.priority_key(start+100,now)
        self.assertLess(known,unknown)
        self.assertLess(unknown,policy.priority_key(start-1,now,like_count=0,repost_count=0))
        self.assertLess(policy.priority_key(start+1,now,like_count=0,repost_count=0),known)

    def test_comments_change_priority_and_unknown_is_not_zero(self):
        now=self.at(8);start,_=policy.day_bounds(now)
        quiet=policy.priority_key(start+1,now,like_count=1,repost_count=1,comment_count=0)
        busy=policy.priority_key(start+2,now,like_count=0,repost_count=0,comment_count=10)
        self.assertLess(quiet,busy)
        self.assertLess(busy,policy.priority_key(start+3,now,like_count=0,repost_count=0,comment_count=None))

    def test_engagement_requires_two_nonnegative_exact_integer_counts(self):
        now=self.at(8);start,_=policy.day_bounds(now)
        missing=policy.priority_key(start,now)
        for likes,reposts in ((True,0),(0,False),(-1,0),(0,-1),(1.0,0),('0',0),(0,None),(None,0)):
            with self.subTest(likes=likes,reposts=reposts):
                self.assertEqual(policy.priority_key(start,now,like_count=likes,repost_count=reposts),missing)
        self.assertLess(policy.priority_key(start,now,like_count=0,repost_count=0),missing)

    def test_configuration_is_copied_and_group_budget_must_split_evenly(self):
        config=policy.validate_config({'accounts':{'1':{'likes':40}}})
        self.assertEqual(config['accounts']['1']['likes'],40)
        config['accounts']['1']['communities'].append(99)
        self.assertEqual(len(policy.DEFAULT_CONFIG['accounts']['1']['communities']),2)
        for override in ({'batch_size':0},{'end_hour':9},{'timezone':'UTC'},
                         {'accounts':{'1':{'community_reposts':21}}},
                         {'accounts':{'2':{'communities':[3,3]}}},
                         {'accounts':{'1':{'likes':True}}}):
            with self.subTest(override=override),self.assertRaises(ValueError):policy.validate_config(override)

    def test_random_schedule_has_forty_distinct_safe_slots(self):
        now=self.at(4);schedule=policy.build_schedule(now,rng=random.Random(44))
        start,end=policy.work_bounds(now)
        self.assertEqual(len(schedule),40)
        self.assertEqual(sorted(set(schedule)),schedule)
        self.assertGreaterEqual(schedule[0],start)
        self.assertLessEqual(schedule[-1],end-timedelta(minutes=10))
        for i,slot in enumerate(schedule):
            self.assertLessEqual(abs((slot-(start+timedelta(seconds=i*900))).total_seconds()),120)
        self.assertTrue(all((b-a).total_seconds()>=600 for a,b in zip(schedule,schedule[1:])))
        self.assertNotEqual(schedule,policy.build_schedule(now,rng=random.Random(45)))

    def test_random_schedule_does_not_repeat_daily_gap_seconds(self):
        for seed in range(25):
            with self.subTest(seed=seed):
                schedule=policy.build_schedule(self.at(4),rng=random.Random(seed))
                gaps=[int((b-a).total_seconds()) for a,b in zip(schedule,schedule[1:])]
                self.assertEqual(len(gaps),len(set(gaps)))

    def test_constant_rng_uses_bounded_fallback_with_unique_gaps(self):
        class ConstantRng:
            calls=0
            def randint(self,minimum,maximum):
                self.calls+=1
                return 0
        rng=ConstantRng()
        schedule=policy.build_schedule(self.at(4),rng=rng)
        gaps=[int((b-a).total_seconds()) for a,b in zip(schedule,schedule[1:])]
        self.assertEqual(len(schedule),40)
        self.assertEqual(len(set(gaps)),39)
        self.assertTrue(all(gap>=600 for gap in gaps))
        self.assertLessEqual(rng.calls,40*8)

    def test_impossible_unique_gap_window_fails_clearly(self):
        config={'batch_size':2,'accounts':{'1':{'likes':36},'2':{'likes':36}}}
        with self.assertRaisesRegex(ValueError,'[Uu]nique'):
            policy.build_schedule(self.at(4),config,rng=random.Random(4))

    def test_persisted_schedule_rejects_repeated_gap_seconds(self):
        slots=[self.at(4),self.at(4,15),self.at(4,30)]
        with self.assertRaises(ValueError):policy.due_slot(self.at(4),slots)

    def test_seeded_schedule_date_and_datetime_agree(self):
        self.assertEqual(policy.build_schedule(date(2026,10,21),rng=random.Random(4)),
                         policy.build_schedule(self.at(8),rng=random.Random(4)))

    def test_due_slot_drops_missed_slots_instead_of_catching_up(self):
        schedule=policy.build_schedule(self.at(4),rng=random.Random(3))
        self.assertEqual(policy.due_slot(schedule[7],schedule),7)
        self.assertEqual(policy.due_slot(schedule[7]+timedelta(seconds=120),schedule),7)
        self.assertIsNone(policy.due_slot(schedule[7]+timedelta(seconds=121),schedule))
        self.assertIsNone(policy.due_slot(schedule[0]-timedelta(seconds=1),schedule))
        self.assertEqual(policy.due_slot(schedule[30],schedule),30)

    def test_persisted_schedule_drives_slot_and_wake(self):
        schedule=policy.build_schedule(self.at(4),rng=random.Random(8))
        now=schedule[5]+timedelta(seconds=30)
        self.assertEqual(policy.batch_slot(now,schedule=schedule),5)
        self.assertEqual(policy.next_wake(now,schedule=schedule),schedule[6])
        self.assertIsNone(policy.batch_slot(self.at(14),schedule=schedule))
        self.assertIsNone(policy.due_slot(self.at(14),schedule))

    def test_malformed_or_wrong_day_schedules_fail_closed(self):
        schedule=policy.build_schedule(self.at(4),rng=random.Random(7))
        for invalid in (list(reversed(schedule)),[schedule[0],schedule[0]],
                        [datetime(2026,10,21,9)],
                        [self.at(3)], [self.at(14)]):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                policy.due_slot(self.at(8),invalid)
        self.assertIsNone(policy.due_slot(self.at(8)+timedelta(days=1),schedule))

    def test_persisted_schedule_cannot_compress_batches_or_remove_finish_reserve(self):
        invalids=([self.at(4),self.at(4,8,59)],
                  [self.at(4),self.at(13,50,1)])
        for schedule in invalids:
            with self.subTest(schedule=schedule),self.assertRaises(ValueError):
                policy.due_slot(self.at(4),schedule)


if __name__=='__main__':unittest.main()
