"""Repeated refreshes must not consume the whole shared daily read budget."""
import unittest
from datetime import datetime, timedelta, timezone

from vk_adaptive_planner import plan_day
from vk_adaptive_simulation import simulate_day


START = datetime(2026, 11, 1, 19, tzinfo=timezone.utc)
COSTS = {'like': 67.581, 'personal': 61.989, 'community': 98.142}
REFRESH_SECONDS = 384.341
REFRESH_REQUESTS = 66


def quotas():
    return {(actor, kind, target): count
            for actor in range(1, 6)
            for kind, target, count in (
                ('like', 0, 50), ('personal', 0, 0 if actor == 5 else 10),
                ('community', 101, 10), ('community', 102, 10))}


class AdaptiveApiEconomyTests(unittest.TestCase):
    def plan(self, **changes):
        args = dict(now=START, end=START + timedelta(days=1), remaining=quotas(),
                    action_seconds=COSTS, refresh_seconds=REFRESH_SECONDS,
                    refresh_requests=REFRESH_REQUESTS, api_attempts=[], reserved_reads=128)
        args.update(changes)
        return plan_day(**args)

    def test_real_cost_full_day_amortizes_all_group_refreshes(self):
        result = self.plan()
        self.assertTrue(result['feasible'])
        self.assertEqual(result['planned_attempts'], 390)
        self.assertLessEqual(result['batch_count'], 38)
        self.assertLessEqual(result['api_required'], 3026)
        self.assertGreaterEqual(result['next_target'], 10)
        self.assertEqual(result['assumptions']['refresh_work_ratio'], 2.0)

    def test_late_32_likes_need_two_refreshes_instead_of_nine(self):
        result = self.plan(now=START + timedelta(hours=21),
                           remaining={(1, 'like', 0): 32}, reserved_reads=0)
        self.assertTrue(result['feasible'])
        self.assertEqual(result['batch_count'], 2)
        self.assertEqual(result['next_target'], 16)
        self.assertEqual(result['api_required'], 164)

    def test_small_remainder_still_gets_one_fresh_pack(self):
        result = self.plan(remaining={(1, 'like', 0): 1})
        self.assertEqual(result['batch_count'], 1)
        self.assertEqual(result['api_required'], 66 + 1 + 128)

    def test_zero_refresh_cost_keeps_finite_time_limited_cadence(self):
        result = self.plan(remaining={(1, 'like', 0): 5}, refresh_seconds=0,
                           refresh_requests=0)
        self.assertEqual(result['batch_count'], 5)

    def test_live_replanning_keeps_all_390_with_read_headroom(self):
        result = simulate_day(START, START + timedelta(days=1), quotas(), COSTS,
                              REFRESH_SECONDS, REFRESH_REQUESTS, [], reserved_reads=128)
        self.assertEqual(result['attempted'], 390)
        self.assertEqual(result['remaining_attempts'], 0)
        self.assertLess(result['peak_rolling_api_reads'] + 128, 3300)
        for previous, current in zip(result['batches'], result['batches'][1:]):
            rest = (datetime.fromisoformat(current['started_at']) -
                    datetime.fromisoformat(previous['finished_at'])).total_seconds()
            self.assertGreaterEqual(rest, 540)

    def test_three_consecutive_days_fit_the_same_rolling_ledger(self):
        reads = []
        last_finish = None
        for day in range(3):
            start = START + timedelta(days=day)
            result = simulate_day(start, start + timedelta(days=1), quotas(), COSTS,
                                  REFRESH_SECONDS, REFRESH_REQUESTS, reads,
                                  last_finished_at=last_finish, reserved_reads=128)
            self.assertEqual(result['attempted'], 390)
            self.assertEqual(result['remaining_attempts'], 0)
            self.assertLess(result['peak_rolling_api_reads'] + 128, 3300)
            # Carry actual virtual reservations forward instead of clearing the
            # rolling ledger at midnight. Kinds retain simulator execution order.
            for batch in result['batches']:
                begin = datetime.fromisoformat(batch['started_at'])
                reads.extend(begin + timedelta(seconds=5 * i) for i in range(66))
                cursor = begin + timedelta(seconds=max(REFRESH_SECONDS, 66 * 5))
                for kind, count in batch['kinds'].items():
                    for _ in range(count):
                        reads.append(cursor)
                        cursor += timedelta(seconds=COSTS[kind])
            reads.sort()
            last_finish = datetime.fromisoformat(result['batches'][-1]['finished_at'])


if __name__ == '__main__':
    unittest.main()
