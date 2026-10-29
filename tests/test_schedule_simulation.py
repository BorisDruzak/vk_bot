"""Offline integration checks: real Worker/State, synthetic external work."""
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('vk_schedule_simulation'),
                             'offline simulator has not been implemented')
        self.sim = importlib.import_module('vk_schedule_simulation')

    def short(self, **overrides):
        scenario = {'day': '2026-10-29', 'slots': ['09:00:00', '09:10:00'],
                    'refresh_seconds': 0,
                    'stages': {'preparation': [47], 'cooldown': [20],
                               'confirmation_cleanup': [5]}}
        scenario.update(overrides)
        return scenario

    def test_incident_has_three_attempts_with_real_spacing_and_intent_cutoff(self):
        result = self.sim.simulate(self.sim.incident_scenario())
        batch = result['batches'][0]
        self.assertEqual((batch['attempts'], batch['verified']), (3, 3))
        self.assertEqual(batch['phase'], 'batch_action_deadline')
        self.assertEqual(batch['spacing_ready_at'], '2026-10-29T09:34:38.071000+05:00')
        self.assertEqual(batch['refresh_finished_at'], '2026-10-29T09:29:57.947000+05:00')
        self.assertEqual(batch['intent_deadline'], '2026-10-29T09:37:56+05:00')
        self.assertEqual(batch['finished_at'], '2026-10-29T09:38:14.315000+05:00')
        self.assertEqual(batch['classification'], 'partial')
        self.assertLess(batch['next_slot_offset_seconds'], 0)

    def test_saved80_is_repeatable_without_mutating_input(self):
        scenario = self.sim.default_scenario()
        before = copy.deepcopy(scenario)
        a = self.sim.simulate(scenario)
        b = self.sim.simulate(scenario)
        self.assertEqual(scenario, before)
        self.assertEqual(a, b)
        self.assertEqual(len(a['batches']), 80)
        self.assertEqual(a['plan']['min_gap_seconds'], 545)
        self.assertEqual(a['plan']['max_gap_seconds'], 623)
        self.assertEqual(a['plan']['unique_gaps'], 79)
        self.assertEqual(a['summary']['theoretical_attempt_capacity'], 320)
        self.assertLessEqual(a['summary']['attempts'], 320)
        self.assertTrue(all(b['attempts'] <= 4 for b in a['batches']))
        self.assertTrue(all(v <= 80 for v in a['summary']['actor_attempts'].values()))

    def test_late_claim_is_inclusive_at120_and_missed_after(self):
        for late, classification in [(120, 'complete'), (120.001, 'missed')]:
            with self.subTest(late=late):
                result = self.sim.simulate(self.short(claim_offsets={'0': late}))
                self.assertEqual(result['batches'][0]['classification'], classification)
        self.assertEqual(result['batches'][0]['reason'], 'claim_grace_expired')

    def test_long_confirmation_misses_following_slot_without_catchup(self):
        result = self.sim.simulate(self.short(
            stages={'preparation': [47], 'cooldown': [20], 'confirmation_cleanup': [700]}))
        first, second = result['batches']
        self.assertEqual(first['attempts'], 1)
        self.assertGreater(first['next_slot_offset_seconds'], 120)
        self.assertEqual(second['classification'], 'missed')
        self.assertEqual(second['reason'], 'claim_grace_expired')
        self.assertEqual(result['summary']['attempts'], 1)

    def test_final_slot_uses_end_of_window_and_no_next_slot_offset(self):
        result = self.sim.simulate(self.short(slots=['21:48:56']))
        batch = result['batches'][0]
        self.assertEqual(batch['boundary_at'], '2026-10-29T22:00:00+05:00')
        self.assertEqual(batch['intent_deadline'], '2026-10-29T21:59:00+05:00')
        self.assertIsNone(batch['next_slot_offset_seconds'])

    def test_crossing_calendar_day_after_send_stops_remaining_work(self):
        result = self.sim.simulate(self.short(slots=['21:48:56'],
            stages={'preparation': [47], 'cooldown': [20], 'confirmation_cleanup': [8000]}))
        batch = result['batches'][0]
        self.assertEqual((batch['attempts'], batch['phase']), (1, 'batch_day_changed'))

    def test_before_intent_failure_is_failed_without_attempt_or_success(self):
        result = self.sim.simulate(self.short(outcomes=['fail_before_intent']))
        batch = result['batches'][0]
        self.assertEqual((batch['attempts'], batch['verified'], batch['failed_without_attempt']), (0, 0, 1))
        self.assertEqual(batch['phase'], 'attention')
        self.assertEqual(result['batches'][1]['reason'], 'paused_or_uncertain')

    def test_after_intent_failure_keeps_attempt_uncertain_and_cooldown(self):
        result = self.sim.simulate(self.short(outcomes=['fail_after_intent']))
        batch = result['batches'][0]
        self.assertEqual((batch['attempts'], batch['verified'], batch['uncertain']), (1, 0, 1))
        self.assertEqual(batch['actions'][0]['cooldown_seconds'], 20)
        self.assertEqual(result['summary']['attempts'], 1)
        self.assertEqual(result['batches'][1]['reason'], 'paused_or_uncertain')

    def test_stop_during_preparation_preserves_zero_attempts(self):
        result = self.sim.simulate(self.short(stop_at='09:00:30'))
        self.assertEqual(result['summary']['attempts'], 0)
        self.assertEqual(result['batches'][0]['phase'], 'batch_stop_requested')

    def test_modeled_http20_backoffs5_10_and_pacing_are_explicit(self):
        result = self.sim.simulate(self.short(refresh_reads=[
            {'seconds': 20, 'outcome': 'network'},
            {'seconds': 20, 'outcome': 'network'},
            {'seconds': 1, 'outcome': 'ok'}]))
        batch = result['batches'][0]
        self.assertEqual(batch['refresh_finished_at'], '2026-10-29T09:00:56+05:00')
        self.assertEqual(batch['read_model']['attempts'], 3)
        self.assertEqual(batch['read_model']['retry_wait_seconds'], [5, 10])
        self.assertTrue(batch['read_model']['modeled'])
        result = self.sim.simulate(self.short(refresh_reads=[{'seconds': 21, 'outcome': 'ok'}]))
        self.assertEqual(result['batches'][0]['read_model']['transports'][0]['seconds'], 20)
        self.assertEqual(result['batches'][0]['attempts'], 0)

    def test_refresh_deadline_and_shared_budget_do_not_permit_stale_pool(self):
        result = self.sim.simulate(self.short(refresh_seconds=481))
        self.assertEqual(result['batches'][0]['phase'], 'counter_refresh_deadline')
        result = self.sim.simulate(self.short(api_budget=1, refresh_reads=[
            {'seconds': 20, 'outcome': 'network'}, {'seconds': 1, 'outcome': 'ok'}]))
        self.assertEqual(result['summary']['attempts'], 0)
        self.assertEqual(result['batches'][0]['phase'], 'counter_refresh_budget')

    def test_cli_accepts_json_and_emits_same_offline_report(self):
        completed = subprocess.run([sys.executable, str(Path(self.sim.__file__)), '--input', '-'],
                                   input=json.dumps(self.short()), text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(len(report['batches']), 2)
        self.assertTrue(report['offline'])

    def test_admission_already_is_not_a_new_attempt_or_verified(self):
        result = self.sim.simulate(self.short(outcomes=['already'] * 16))
        self.assertEqual(result['summary']['attempts'], 0)
        self.assertEqual(result['summary']['verified'], 0)
        self.assertGreater(result['summary']['already'], 0)

    def test_admission_exact_http20_reserve_at_refresh_deadline_is_denied(self):
        result = self.sim.simulate(self.short(claim_offsets={'0': 120},
            refresh_reads=[{'seconds': 20, 'outcome': 'network'}] * 20))
        self.assertEqual(result['batches'][0]['attempts'], 0)
        # At09:08:00 minus20 exactly, real refresh requires strictly more room.
        scenario = self.short(claim_offsets={'0': 120}, refresh_pages=[
            [{'seconds': 20, 'outcome': 'ok'}] for _ in range(18)])
        result = self.sim.simulate(scenario)
        self.assertEqual(result['batches'][0]['read_model']['attempts'], 17)
        self.assertEqual(result['batches'][0]['phase'], 'counter_refresh_deadline')

    def test_admission_expired_spacing_never_starts_preparation(self):
        result = self.sim.simulate(self.short(previous_action_at='09:01:00', previous_action_slot=-1))
        self.assertEqual(result['batches'][0]['phase'], 'batch_spacing_deadline')
        self.assertEqual(result['batches'][0]['actions'], [])

    def test_admission_day_end_preparation_does_not_reserve(self):
        result = self.sim.simulate(self.short(slots=['21:48:56'],
            stages={'preparation': [650], 'cooldown': [20], 'confirmation_cleanup': [5]}))
        self.assertEqual(result['batches'][0]['phase'], 'batch_work_window_closed')
        self.assertEqual(result['summary']['attempts'], 0)

    def test_admission_invalid_sample_and_empty_scenario_fail_cleanly(self):
        for scenario in (self.short(stages={'preparation': []}), self.short(slots=[]),
                         self.short(refresh_seconds=[]), self.short(refresh_reads='invalid')):
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                self.sim.simulate(scenario)

    def test_admission_seeded_sampling_is_repeatable(self):
        scenario = self.short(sampling='seeded', seed=19,
            stages={'preparation': [47, 48], 'cooldown': [20, 20.1],
                    'confirmation_cleanup': [3, 4, 5]})
        self.assertEqual(self.sim.simulate(scenario), self.sim.simulate(scenario))


if __name__ == '__main__':
    unittest.main()
