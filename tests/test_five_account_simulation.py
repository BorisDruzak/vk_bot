"""Five synthetic accounts run through the real Worker/State timing guards."""
import copy
from collections import Counter
from datetime import datetime
import io
import json
import unittest
from unittest.mock import patch

import vk_schedule_policy as policy
import vk_schedule_simulation as sim


class FiveAccountSimulationTests(unittest.TestCase):
    def short(self, **overrides):
        scenario = sim.five_account_scenario(seed=29)
        scenario['last_slot'] = 1
        scenario.update(overrides)
        return scenario

    def test_factory_builds_seeded67_plan_with_five_equal_quotas(self):
        self.assertTrue(callable(getattr(sim, 'five_account_scenario', None)),
                        'five-account scenario factory is missing')
        before = sim.six_batch_scenario(seed=29)
        scenario = sim.five_account_scenario(seed=29)
        self.assertEqual(scenario, sim.five_account_scenario(seed=29))
        self.assertNotEqual(scenario['slots'], sim.five_account_scenario(seed=30)['slots'])
        self.assertEqual(scenario['day'], '2026-10-30')
        self.assertEqual(len(scenario['slots']), 67)
        self.assertEqual(set(scenario['policy']['accounts']), {'1', '2', '5', '6', '7'})
        self.assertEqual(policy.daily_total(scenario['policy']), 400)
        self.assertEqual(scenario['policy']['batch_size'], 6)
        self.assertEqual((scenario['policy']['start_hour'], scenario['policy']['end_hour']), (9, 22))
        for account in scenario['policy']['accounts'].values():
            self.assertEqual([account[k] for k in ('likes', 'personal_reposts', 'community_reposts')], [50, 10, 20])
            self.assertEqual(len(set(account['communities'])), 2)
        self.assertEqual(scenario['duration_samples'], before['duration_samples'])
        self.assertEqual(scenario['measurement_basis']['successful_actions'], 105)
        self.assertEqual(before, sim.six_batch_scenario(seed=29))
        self.assertEqual(len(before['slots']), 54)
        self.assertEqual(policy.daily_total(before['policy']), 320)
        self.assertEqual(len(sim.default_scenario()['slots']), 80)

    def test_full67_measured_model_respects_400_and_each_actor_category_destination(self):
        scenario = sim.five_account_scenario(seed=29)
        before = copy.deepcopy(scenario)
        result = sim.simulate(scenario)
        self.assertEqual(scenario, before)
        self.assertEqual(len(result['batches']), 67)
        self.assertEqual(result['summary']['simulated_slot_capacity'], 402)
        self.assertEqual(result['summary']['theoretical_attempt_capacity'], 400)
        self.assertEqual(result['summary']['attempts'], 400)
        self.assertEqual(result['summary']['verified'], 400)
        self.assertTrue(all(batch['attempts'] <= 6 for batch in result['batches']))
        self.assertEqual(sorted(result['summary']['actor_attempts'].values()), [80] * 5)
        for kinds in result['summary']['actor_kind_attempts'].values():
            self.assertEqual(kinds, {'like': 50, 'personal': 10, 'community': 20})
        for targets in result['summary']['community_target_attempts'].values():
            self.assertEqual(sorted(targets.values()), [10, 10])
        self.assertEqual(result['plan']['unique_gaps'], 66)
        self.assertGreaterEqual(result['plan']['min_gap_seconds'], 540)
        self.assertTrue(result['plan']['unchanged'])
        self.assertEqual(result['retry']['active'], 0)

    def test_short_seeded_run_is_repeatable(self):
        scenario = self.short()
        self.assertEqual(sim.simulate(scenario), sim.simulate(scenario))

    def test_six_fast_reservations_never_create_a_seventh(self):
        result = sim.simulate(self.short(last_slot=0, refresh_seconds=0, duration_samples={},
            stages={'preparation': [0], 'cooldown': [20], 'confirmation_cleanup': [0]}))
        self.assertEqual(result['summary']['attempts'], 6)
        self.assertEqual(len(result['batches'][0]['actions']), 6)

    def test_slow_preparation_keeps_real_deadline_instead_of_filling_six(self):
        result = sim.simulate(self.short(slots=['09:00:00', '09:10:00'], last_slot=0,
            refresh_seconds=0, duration_samples={},
            stages={'preparation': [100], 'cooldown': [20], 'confirmation_cleanup': [0]}))
        batch = result['batches'][0]
        self.assertEqual((batch['attempts'], batch['phase']), (4, 'batch_action_deadline'))
        deadline = datetime.fromisoformat(batch['intent_deadline'])
        self.assertTrue(all(datetime.fromisoformat(action['intent_at']) < deadline
                            for action in batch['actions'] if action['attempt_id'] is not None))

    def test_five_cli_flag_keeps_historical_flags_and_default(self):
        cases = [(['--five-accounts'], sim.five_account_scenario()),
                 (['--six-batches'], sim.six_batch_scenario()),
                 (['--incident'], sim.incident_scenario()), ([], sim.default_scenario())]
        for argv, expected in cases:
            with self.subTest(argv=argv), patch.object(sim, 'simulate', return_value={'offline': True}) as run, \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(sim.main(argv), 0)
                self.assertEqual(run.call_args.args[0], expected)
                self.assertTrue(json.loads(output.getvalue())['offline'])
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
            sim.main(['--five-accounts', '--six-batches'])
        self.assertEqual(raised.exception.code, 2)


class CongregationSimulationTests(unittest.TestCase):
    def short(self, **overrides):
        scenario = sim.congregation_scenario(seed=29)
        scenario['last_slot'] = 1
        scenario.update(overrides)
        return scenario

    def test_factory_keeps67_batches_with390_quota_and_zero_fifth_personal(self):
        self.assertTrue(callable(getattr(sim, 'congregation_scenario', None)),
                        '390-attempt congregation scenario factory is missing')
        historical = sim.five_account_scenario(seed=29)
        scenario = sim.congregation_scenario(seed=29)
        self.assertEqual(scenario, sim.congregation_scenario(seed=29))
        cfg = scenario['policy']
        self.assertEqual((policy.daily_total(cfg), len(scenario['slots'])), (390, 67))
        self.assertEqual(cfg['planned_batches'], 67)
        self.assertEqual(cfg['accounts']['7']['personal_reposts'], 0)
        self.assertEqual(cfg['accounts']['7']['likes'], 50)
        self.assertEqual(cfg['accounts']['7']['community_reposts'], 20)
        for actor in ('1', '2', '5', '6'):
            self.assertEqual(cfg['accounts'][actor], historical['policy']['accounts'][actor])
        self.assertEqual(Counter(policy.batch_attempt_limit(cfg, i) for i in range(67)), {6: 55, 5: 12})
        self.assertEqual(scenario['duration_samples'], historical['duration_samples'])
        self.assertEqual(historical, sim.five_account_scenario(seed=29))
        self.assertEqual(policy.daily_total(historical['policy']), 400)
        self.assertNotIn('planned_batches', historical['policy'])

    def test_full_measured67_reports390_allocations_and_actual_guarded_usage(self):
        scenario = sim.congregation_scenario(seed=29)
        before = copy.deepcopy(scenario)
        result = sim.simulate(scenario)
        self.assertEqual(scenario, before)
        self.assertEqual(len(result['batches']), 67)
        self.assertEqual(result['summary']['simulated_slot_capacity'], 402)
        self.assertEqual(result['summary']['planned_attempt_capacity'], 390)
        self.assertEqual(result['summary']['theoretical_attempt_capacity'], 390)
        self.assertEqual(Counter(result['plan']['attempt_limits']), {6: 55, 5: 12})
        self.assertEqual(sum(result['plan']['attempt_limits']), 390)
        # This deterministic successful timing fixture fills its390 allocations;
        # the separate slow scenarios demonstrate that this is no live guarantee.
        self.assertEqual(result['summary']['attempts'], 390)
        self.assertEqual(result['summary']['verified'], 390)
        self.assertEqual(result['summary']['complete'], 67)
        for batch in result['batches']:
            self.assertEqual(batch['attempt_limit'], result['plan']['attempt_limits'][batch['slot']])
            self.assertLessEqual(batch['attempts'], batch['attempt_limit'])
            if batch['attempts'] == batch['attempt_limit']:
                self.assertEqual(batch['classification'], 'complete')
            self.assertFalse(any(action['account'] == 7 and action['kind'] == 'personal'
                                 for action in batch['actions']))
        for actor, kinds in result['summary']['actor_kind_attempts'].items():
            self.assertLessEqual(kinds['like'], 50)
            self.assertLessEqual(kinds['personal'], 0 if actor == '7' else 10)
            self.assertLessEqual(kinds['community'], 20)
            self.assertLessEqual(result['summary']['actor_attempts'][actor], 70 if actor == '7' else 80)
        for targets in result['summary']['community_target_attempts'].values():
            self.assertTrue(all(used <= 10 for used in targets.values()))
        self.assertEqual(result['summary']['actor_kind_attempts']['7']['personal'], 0)
        self.assertTrue(result['plan']['unchanged'])
        self.assertEqual(result['plan']['unique_gaps'], 66)
        self.assertGreaterEqual(result['plan']['min_gap_seconds'], 540)

    def test_fast_five_and_six_targets_are_complete_without_extra_attempt(self):
        result = sim.simulate(self.short(refresh_seconds=0, duration_samples={},
            stages={'preparation': [0], 'cooldown': [20], 'confirmation_cleanup': [0]}))
        self.assertEqual([batch['attempt_limit'] for batch in result['batches']], [5, 6])
        self.assertEqual([batch['attempts'] for batch in result['batches']], [5, 6])
        self.assertEqual([batch['classification'] for batch in result['batches']], ['complete', 'complete'])
        self.assertEqual(result['summary']['planned_attempt_capacity'], 11)

    def test_slow_five_and_six_targets_preserve_intent_deadline(self):
        for slot in (0, 1):
            with self.subTest(slot=slot):
                result = sim.simulate(self.short(slots=['09:00:00', '09:10:00', '09:19:59'],
                    first_slot=slot, last_slot=slot, refresh_seconds=0, duration_samples={},
                    stages={'preparation': [100], 'cooldown': [20], 'confirmation_cleanup': [0]}))
                batch = result['batches'][0]
                self.assertEqual((batch['attempts'], batch['phase']), (4, 'batch_action_deadline'))
                self.assertEqual(batch['attempt_limit'], 5 if slot == 0 else 6)
                deadline = datetime.fromisoformat(batch['intent_deadline'])
                self.assertTrue(all(datetime.fromisoformat(action['intent_at']) < deadline
                                    for action in batch['actions'] if action['attempt_id'] is not None))

    def test_short_congregation_model_is_repeatable(self):
        scenario = self.short()
        self.assertEqual(sim.simulate(scenario), sim.simulate(scenario))

    def test_cli_congregation_is_mutually_exclusive_and_legacy_unchanged(self):
        with patch.object(sim, 'simulate', return_value={'offline': True}) as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(sim.main(['--congregation']), 0)
            self.assertEqual(run.call_args.args[0], sim.congregation_scenario())
            self.assertTrue(json.loads(output.getvalue())['offline'])
        for legacy in ('--five-accounts', '--six-batches', '--incident'):
            with self.subTest(legacy=legacy), patch('sys.stderr', new_callable=io.StringIO), \
                    self.assertRaises(SystemExit) as raised:
                sim.main(['--congregation', legacy])
            self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
