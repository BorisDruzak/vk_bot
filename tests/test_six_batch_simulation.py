"""Future six-place simulations exercise real Worker/State on disposable DBs."""
import copy
from datetime import datetime
import io
import json
import unittest
from unittest.mock import patch

import vk_schedule_policy as policy
import vk_schedule_simulation as sim


class SixBatchSimulationTests(unittest.TestCase):
    def short(self, **overrides):
        scenario = sim.six_batch_scenario(seed=29)
        scenario['last_slot'] = 1
        scenario.update(overrides)
        return scenario

    def test_future_factory_uses_real_seeded_schedule_and_explicit_320_policy(self):
        self.assertTrue(callable(getattr(sim, 'six_batch_scenario', None)),
                        'future six-batch scenario factory is missing')
        scenario = sim.six_batch_scenario(seed=29)
        self.assertEqual(scenario, sim.six_batch_scenario(seed=29))
        self.assertNotEqual(scenario['slots'], sim.six_batch_scenario(seed=30)['slots'])
        self.assertEqual(scenario['day'], '2026-10-30')
        self.assertEqual(len(scenario['slots']), 54)
        cfg = policy.validate_config(scenario['policy'])
        self.assertEqual((cfg['batch_size'], cfg['start_hour'], cfg['end_hour']), (6, 9, 22))
        self.assertEqual(policy.daily_total(cfg), 320)
        self.assertEqual(len(cfg['accounts']), 4)
        for account in cfg['accounts'].values():
            self.assertEqual([account[k] for k in ('likes', 'personal_reposts', 'community_reposts')], [50, 10, 20])
            self.assertEqual(len(set(account['communities'])), 2)
        self.assertEqual(scenario['measurement_basis']['successful_actions'], 105)
        self.assertEqual({k: len(v) for k, v in scenario['duration_samples'].items()},
                         {'community': 29, 'like': 62, 'personal': 14})

    def test_full54_measured_model_respects_six_and_all_actor_category_quotas(self):
        scenario = sim.six_batch_scenario(seed=29)
        before = copy.deepcopy(scenario)
        result = sim.simulate(scenario)
        self.assertEqual(scenario, before)
        self.assertEqual(len(result['batches']), 54)
        self.assertEqual(result['summary']['simulated_slot_capacity'], 324)
        self.assertEqual(result['summary']['theoretical_attempt_capacity'], 320)
        self.assertTrue(all(b['attempts'] <= 6 for b in result['batches']))
        self.assertEqual(result['summary']['attempts'], 320)
        self.assertEqual(result['summary']['verified'], 320)
        self.assertEqual(sorted(result['summary']['actor_attempts'].values()), [80] * 4)
        for kinds in result['summary']['actor_kind_attempts'].values():
            self.assertEqual(kinds, {'like': 50, 'personal': 10, 'community': 20})
        for targets in result['summary']['community_target_attempts'].values():
            self.assertEqual(sorted(targets.values()), [10, 10])
        self.assertEqual(result['plan']['unique_gaps'], 53)
        self.assertGreaterEqual(result['plan']['min_gap_seconds'], 540)
        self.assertTrue(result['plan']['unchanged'])
        self.assertEqual(result['retry']['active'], 0)

    def test_short_seeded_run_is_repeatable(self):
        scenario = self.short()
        self.assertEqual(sim.simulate(scenario), sim.simulate(scenario))

    def test_fast_six_batch_never_reserves_seventh(self):
        scenario = self.short(last_slot=0, refresh_seconds=0, duration_samples={},
                              stages={'preparation': [0], 'cooldown': [20], 'confirmation_cleanup': [0]})
        result = sim.simulate(scenario)
        self.assertEqual(result['summary']['attempts'], 6)
        self.assertEqual(len(result['batches'][0]['actions']), 6)

    def test_time_limited_six_batch_stops_at_real_intent_cutoff(self):
        scenario = self.short(slots=['09:00:00', '09:10:00'], last_slot=0,
                              refresh_seconds=0, duration_samples={},
                              stages={'preparation': [100], 'cooldown': [20], 'confirmation_cleanup': [0]})
        result = sim.simulate(scenario)
        batch = result['batches'][0]
        self.assertEqual((batch['attempts'], batch['phase']), (4, 'batch_action_deadline'))
        cutoff = datetime.fromisoformat(batch['intent_deadline'])
        self.assertTrue(all(datetime.fromisoformat(a['intent_at']) < cutoff
                            for a in batch['actions'] if a['attempt_id'] is not None))

    def test_failed_or_uncertain_action_is_not_resent_by_six_capacity(self):
        for outcome, attempts in [('fail_before_intent', 0), ('fail_after_intent', 1)]:
            with self.subTest(outcome=outcome):
                result = sim.simulate(self.short(outcomes=[outcome]))
                self.assertEqual(result['summary']['attempts'], attempts)
                self.assertEqual(len(result['batches'][0]['actions']), 1)
                self.assertEqual(result['batches'][1]['reason'], 'paused_or_uncertain')

    def test_six_refresh_budget_and_stop_guards_remain_fail_closed(self):
        result = sim.simulate(self.short(api_budget=1, refresh_reads=[
            {'seconds': 20, 'outcome': 'network'}, {'seconds': 1, 'outcome': 'ok'}]))
        self.assertEqual(result['summary']['attempts'], 0)
        self.assertEqual(result['batches'][0]['phase'], 'counter_refresh_budget')
        result = sim.simulate(self.short(stop_at='09:00:30'))
        self.assertEqual(result['summary']['attempts'], 0)

    def test_legacy_saved80_and_incident_three_of_four_are_preserved(self):
        self.assertEqual(len(sim.default_scenario()['slots']), 80)
        self.assertEqual(sim._config(sim.default_scenario())['batch_size'], 4)
        result = sim.simulate(sim.incident_scenario())
        self.assertEqual(result['summary']['attempts'], 3)
        self.assertEqual(result['summary']['simulated_slot_capacity'], 4)

    def test_cli_six_flag_selects_future_scenario_without_changing_defaults(self):
        with patch.object(sim, 'simulate', return_value={'offline': True}) as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(sim.main(['--six-batches']), 0)
            self.assertEqual(run.call_args.args[0], sim.six_batch_scenario())
            self.assertTrue(json.loads(output.getvalue())['offline'])
        with patch.object(sim, 'simulate', return_value={}) as run, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(sim.main([]), 0)
            self.assertEqual(run.call_args.args[0], sim.default_scenario())


if __name__ == '__main__':
    unittest.main()
