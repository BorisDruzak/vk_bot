import unittest
from datetime import datetime, timedelta, timezone

from vk_adaptive_simulation import simulate_day


START=datetime(2026,10,29,19,tzinfo=timezone.utc)  # 30 October midnight YEKT
COSTS={'like':60,'personal':75,'community':100}


class SimulationTests(unittest.TestCase):
    def simulate(self, remaining=None, **kwargs):
        return simulate_day(START,START+timedelta(days=1),remaining or {(1,'like',0):50},
                            COSTS,320,64,[],**kwargs)

    def test_real_finish_controls_next_start_even_when_pack_is_slow(self):
        result=self.simulate(slow_batches={0:3})
        self.assertGreater(len(result['batches']),1)
        for previous,current in zip(result['batches'],result['batches'][1:]):
            finished=datetime.fromisoformat(previous['finished_at'])
            start=datetime.fromisoformat(current['started_at'])
            self.assertGreaterEqual((start-finished).total_seconds(),540)
        self.assertEqual(result['attempted'],50)
        self.assertEqual(result['remaining_attempts'],0)

    def test_midday_actor_enters_next_boundary_without_resetting_existing_spend(self):
        result=self.simulate(changes=[{'at':START+timedelta(hours=12),
                                      'quotas':{(1,'like',0):50,(2,'like',0):50}}])
        self.assertEqual(result['attempted'],100)
        self.assertEqual(result['actor_count'],2)
        self.assertEqual(result['remaining_attempts'],0)
        self.assertTrue(any(datetime.fromisoformat(b['started_at'])>=START+timedelta(hours=12)
                            for b in result['batches']))

    def test_error_and_uncertainty_spend_quota(self):
        result=self.simulate(outcomes=('verified','failed','uncertain'))
        self.assertEqual(result['attempted'],50)
        self.assertEqual(sum(result['outcomes'].values()),50)
        self.assertGreater(result['outcomes']['uncertain'],0)
        self.assertEqual(result['remaining_attempts'],0)

    def test_restart_carries_rest_across_calendar_boundary(self):
        result=self.simulate(last_finished_at=START-timedelta(seconds=100))
        first=datetime.fromisoformat(result['batches'][0]['started_at'])
        self.assertGreaterEqual((first-START).total_seconds(),440)

    def test_midnight_and_rolling_api_limits_hold_for_slow_actions(self):
        result=simulate_day(START,START+timedelta(minutes=20),{(1,'community',1):20},
                            COSTS,320,64,[],slow_batches={0:100})
        self.assertLess(result['attempted'],20)
        for batch in result['batches']:
            self.assertLessEqual(datetime.fromisoformat(batch['finished_at']),START+timedelta(minutes=20))
        self.assertLessEqual(result['peak_rolling_api_reads'],5000)
        self.assertGreater(result['remaining_attempts'],0)

    def test_saturated_existing_api_ledger_is_reported_without_new_attempts(self):
        reads=[START-timedelta(seconds=5*i) for i in range(4998)]
        result=simulate_day(START,START+timedelta(hours=1),{(1,'like',0):50},
                            COSTS,320,64,reads)
        self.assertEqual(result['attempted'],0)
        self.assertEqual(result['peak_rolling_api_reads'],4998)

    def test_disable_then_reenable_cannot_refund_calendar_attempts(self):
        result=self.simulate(changes=[
            {'at':START+timedelta(hours=2),'quotas':{(1,'like',0):0}},
            {'at':START+timedelta(hours=12),'quotas':{(1,'like',0):50}}])
        self.assertEqual(result['attempted'],50)
        self.assertEqual(result['remaining_attempts'],0)


if __name__=='__main__':unittest.main()
