"""Pure forecasts: none of these tests opens browser/state/production files."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from vk_adaptive_planner import plan_day

UTC = timezone.utc
DAY = datetime(2026, 10, 29, 19, tzinfo=UTC)  # YEKT 30 October 00:00
END = DAY + timedelta(days=1)
COSTS = {"like": 30, "personal": 50, "community": 70}


def quotas(actors=5):
    result = {}
    for actor in range(1, actors + 1):
        result[(actor, "like", None)] = 50
        result[(actor, "personal", None)] = 0 if actor == 5 else 10
        result[(actor, "community", 100)] = 10
        result[(actor, "community", 200)] = 10
    return result


class AdaptivePlannerTests(unittest.TestCase):
    def plan(self, **overrides):
        values = dict(now=DAY, end=END, remaining=quotas(), action_seconds=COSTS,
                      refresh_seconds=60, refresh_requests=10, api_attempts=[])
        values.update(overrides)
        return plan_day(**values)

    def assert_safe(self, result, end=END):
        json.dumps(result, allow_nan=False)
        previous = None
        for batch in result["batches"]:
            start, finish = map(datetime.fromisoformat, (batch["start"], batch["end"]))
            self.assertGreaterEqual(finish, start)
            self.assertLessEqual(finish, end)
            if previous is not None:
                self.assertGreaterEqual((start - previous).total_seconds(), 540)
            previous = finish
        self.assertEqual(sum(b["target"] for b in result["batches"]), result["planned_attempts"])
        self.assertEqual(result["remaining_attempts"] - result["planned_attempts"], result["shortfall"])
        targets = [b["target"] for b in result["batches"]]
        if targets:
            self.assertLessEqual(max(targets) - min(targets), 1)
            self.assertEqual(result["next_target"], targets[0])

    def test_complete_390_chooses_maximum_batches_not_fixed_67(self):
        result = self.plan()
        self.assertTrue(result["feasible"])
        self.assertEqual(result["remaining_attempts"], 390)
        # Weighted action total = 250*30 + 40*50 + 100*70 = 16500.
        # N batches cost 16500 + N*60 + (N-1)*540 <= 86400 -> N<=117.
        self.assertEqual(result["batch_count"], 117)
        self.assertEqual(result["next_target"], 4)
        self.assertEqual(result["maximum_attempts_fit"], 390)
        self.assert_safe(result)

    def test_midday_245_and_new_actor_recompute_remaining(self):
        now = DAY + timedelta(hours=16)
        old = {(1, "like", None): 145}
        before = self.plan(now=now, remaining=old)
        old[(2, "community", 100)] = 100
        after = self.plan(now=now, remaining=old)
        self.assertEqual(after["remaining_attempts"], 245)
        self.assertTrue(after["feasible"])
        self.assertGreater(after["next_target"], before["next_target"])
        self.assert_safe(after)

    def test_slow_measurement_increases_target_and_reduces_batch_count(self):
        fast = self.plan()
        slow = self.plan(action_seconds={kind: value * 3 for kind, value in COSTS.items()})
        self.assertTrue(slow["feasible"])
        self.assertLess(slow["batch_count"], fast["batch_count"])
        self.assertGreater(slow["next_target"], fast["next_target"])
        self.assert_safe(slow)

    def test_rest_after_previous_day_finish_survives_midnight(self):
        result = self.plan(last_finished_at=DAY - timedelta(seconds=10))
        self.assertEqual(datetime.fromisoformat(result["next_start"]), DAY + timedelta(seconds=530))
        self.assert_safe(result)

    def test_existing_api_calls_expire_and_pacing_is_honored(self):
        reads = [DAY - timedelta(hours=24) + timedelta(seconds=60)] * 5
        result = self.plan(remaining={(1, "like", None): 1}, refresh_seconds=0,
                           refresh_requests=0, api_limit=5, api_attempts=reads)
        self.assertTrue(result["feasible"])
        # Whole batch can be delayed safely to the first expiration.
        self.assertGreaterEqual(datetime.fromisoformat(result["completion_at"]), DAY + timedelta(seconds=90))
        self.assertEqual(result["api_available"], 0)
        self.assert_safe(result)
        paced = self.plan(remaining={(1, "like", None): 1}, refresh_seconds=0,
                          refresh_requests=0, api_attempts=[DAY - timedelta(seconds=1)])
        self.assertGreaterEqual(datetime.fromisoformat(paced["completion_at"]), DAY + timedelta(seconds=34))

    def test_shared_budget_and_analytics_reservation_reduce_batches(self):
        reads = [DAY - timedelta(seconds=10)] * 4950
        result = self.plan(end=DAY + timedelta(hours=1), remaining={(1, "like", None): 20},
                           refresh_seconds=0, refresh_requests=2, api_attempts=reads,
                           reserved_reads=20)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["batch_count"], 5)  # 20 identities + 5*2 refresh + 20 reserve.
        self.assertEqual(result["api_required"], 50)
        self.assertEqual(result["api_available"], 50)
        self.assert_safe(result, DAY + timedelta(hours=1))

    def test_refresh_duration_has_pacing_floor(self):
        result = self.plan(remaining={(1, "like", None): 1}, refresh_seconds=1,
                           refresh_requests=5, identity_reads_per_attempt=0)
        self.assertEqual(datetime.fromisoformat(result["completion_at"]), DAY + timedelta(seconds=50))

    def test_impossible_reports_shortfall_and_finite_best_effort(self):
        result = self.plan(now=END - timedelta(seconds=100), remaining={(1, "like", None): 390},
                           refresh_seconds=0, refresh_requests=0)
        self.assertFalse(result["feasible"])
        self.assertEqual(result["maximum_attempts_fit"], 3)
        self.assertEqual(result["planned_attempts"], 3)
        self.assertEqual(result["shortfall"], 387)
        self.assertEqual(result["next_target"], 3)
        self.assert_safe(result)

    def test_zero_quota_and_no_time_do_not_create_empty_batches(self):
        zero = self.plan(remaining={})
        self.assertTrue(zero["feasible"])
        self.assertEqual(zero["batch_count"], 0)
        self.assertEqual(zero["next_target"], 0)
        self.assertIsNone(zero["next_start"])
        closed = self.plan(now=END)
        self.assertFalse(closed["feasible"])
        self.assertEqual(closed["maximum_attempts_fit"], 0)
        self.assertEqual(closed["batches"], [])

    def test_budget_with_no_free_capacity_never_becomes_infinite(self):
        result = self.plan(reserved_reads=5000)
        self.assertFalse(result["feasible"])
        self.assertEqual(result["maximum_attempts_fit"], 0)
        self.assert_safe(result)

    def test_fractional_cost_exact_day_capacity_is_not_lost_to_epoch_rounding(self):
        result = self.plan(end=DAY + timedelta(seconds=20100),
                           remaining={(1, "like", None): 1000}, action_seconds={"like": 20.1},
                           refresh_seconds=0, refresh_requests=0)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["planned_attempts"], 1000)
        self.assertEqual(datetime.fromisoformat(result["completion_at"]), DAY + timedelta(seconds=20100))

    def test_iso_ledger_and_no_read_work_with_fully_reserved_api(self):
        result = self.plan(remaining={(1, "like", None): 1}, api_attempts=[DAY.isoformat()],
                           reserved_reads=5000, refresh_seconds=0, refresh_requests=0,
                           identity_reads_per_attempt=0)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["api_required"], 5000)

    def test_existing_state_zero_destination_for_like_and_personal(self):
        result = self.plan(remaining={(1, "like", 0): 1, (1, "personal", 0): 1})
        self.assertTrue(result["feasible"])
        self.assertEqual(result["remaining_attempts"], 2)

    def test_not_before_gate_keeps_current_rolling_api_time(self):
        result = self.plan(remaining={(1, "like", 0): 1}, refresh_seconds=0, refresh_requests=0,
                           api_attempts=[DAY]*5000, not_before=DAY+timedelta(hours=1))
        self.assertFalse(result['feasible'])
        self.assertEqual(result['api_available'],0)
        self.assertEqual(result['available_seconds'],86400)
        open_gate = self.plan(remaining={(1, "like", 0): 1}, not_before=DAY+timedelta(hours=1))
        self.assertEqual(datetime.fromisoformat(open_gate['next_start']),DAY+timedelta(hours=1))

    def test_not_before_after_midnight_cannot_carry_work_to_next_day(self):
        for gate in (END,END+timedelta(hours=2)):
            result=self.plan(not_before=gate)
            self.assertFalse(result['feasible'])
            self.assertEqual(result['batches'],[])
            self.assertEqual(result['shortfall'],390)

    def test_invalid_inputs_rejected(self):
        for values in [dict(remaining={(1, "like", None): True}),
                       dict(remaining={(1, "like", None): -1}),
                       dict(remaining={(1, "invalid", None): 1}),
                       dict(action_seconds={"like": float("nan"), "personal": 50, "community": 70}),
                       dict(action_seconds={"like": 19, "personal": 50, "community": 70}),
                       dict(now=DAY.replace(tzinfo=None)), dict(end=END + timedelta(seconds=1)),
                       dict(not_before=DAY.replace(tzinfo=None)), dict(refresh_requests=True), dict(refresh_seconds=-1), dict(rest_seconds=539),
                       dict(api_pause_seconds=4), dict(api_limit=0),
                       dict(api_attempts=[DAY + timedelta(seconds=1)]),
                       dict(action_seconds={**COSTS, ("like",): 30}),
                       dict(remaining={(1, "community", 0): 1})]:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                self.plan(**values)

    def test_huge_remaining_is_bounded_by_day_capacity(self):
        result = self.plan(remaining={(1, "like", None): 10**12}, refresh_requests=0,
                           refresh_seconds=0, identity_reads_per_attempt=0)
        self.assertFalse(result["feasible"])
        self.assertEqual(result["maximum_attempts_fit"], 2880)
        self.assert_safe(result)


if __name__ == "__main__":
    unittest.main()
