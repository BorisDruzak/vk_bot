"""Adaptive panel reads cached plans and actual history without schema writes."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from unittest.mock import patch

import vk_accounts as accounts
import vk_control as control
import vk_schedule_policy as policy
import vk_scheduler_state as ledger
import vk_scheduler_timing as timing


NOW = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
DAY = '2026-10-30'


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


class AdaptiveSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'vk_scheduler.sqlite3'
        self.legacy = policy.validate_config({'end_hour': 22})
        self.config = policy.validate_config({'schedule_mode': 'adaptive', 'start_hour': 0,
                                              'end_hour': 24, 'batch_rest_seconds': 540,
                                              'accounts': {'1': {'personal_reposts': 0}}})
        state = ledger.State(self.path, {}, self.legacy)
        state.db.execute('INSERT INTO schedule_days VALUES(?,?,?)', (DAY, json.dumps(self.legacy),
                         json.dumps(['2026-10-30T09:00:00Z', '2026-10-30T09:10:00Z'])))
        state.db.execute('INSERT INTO scheduler_batches VALUES(?,?,?,?,?)',
                         (DAY, 0, '2026-10-30T09:00:00Z', '2026-10-30T09:05:00Z', 'batch_action_deadline'))
        state.db.execute("""INSERT INTO scheduler_jobs
            (id,day,slot,account,kind,community_id,group_id,post_id,published_at,status,created_at,reason)
            VALUES(1,?,0,1,'like',0,101,5,1,'uncertain',?,'confirmation_unknown')""",
                         (DAY, '2026-10-30T09:01:00Z'))
        state.db.commit()
        state.close()

    def activate(self, running=False):
        self.forecast = {'feasible': True, 'reason': 'fits', 'remaining_attempts': 70,
                         'planned_attempts': 70, 'shortfall': 0, 'batch_count': 7,
                         'next_target': 10, 'next_start': '2026-10-30T10:10:00Z',
                         'completion_at': '2026-10-30T12:00:00Z', 'projected_seconds': 7200,
                         'available_seconds': 32400, 'api_required': 90, 'api_available': 500,
                         'maximum_attempts_fit': 70, 'assumptions': {},
                         'batches': [{'start': '2026-10-30T10:10:00Z',
                                      'end': '2026-10-30T10:20:00Z', 'target': 10}]}
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
              CREATE TABLE scheduler_adaptive_days(day TEXT PRIMARY KEY,activated_at TEXT,base_slot INTEGER);
              CREATE TABLE scheduler_adaptive_forecasts(id INTEGER PRIMARY KEY,day TEXT,at TEXT,
                config TEXT,remaining TEXT,forecast TEXT);
              CREATE TABLE scheduler_adaptive_batches(day TEXT,slot INTEGER,revision INTEGER,
                target_attempts INTEGER,config TEXT,estimated_end TEXT,enabled_accounts TEXT,
                PRIMARY KEY(day,slot));
              CREATE TABLE scheduler_adaptive_recoveries(day TEXT,slot INTEGER,qualified_at TEXT,
                PRIMARY KEY(day,slot));''')
            db.execute('INSERT INTO scheduler_adaptive_days VALUES(?,?,?)',
                       (DAY, '2026-10-30T09:06:00Z', 2))
            db.execute('INSERT INTO scheduler_adaptive_forecasts VALUES(?,?,?,?,?,?)',
                       (1, DAY, '2026-10-30T09:07:00Z', json.dumps(self.config), '{}', json.dumps(self.forecast)))
            if running:
                db.execute('INSERT INTO scheduler_batches VALUES(?,?,?,?,?)',
                           (DAY, 2, '2026-10-30T09:58:00Z', None, 'running'))
                db.execute('INSERT INTO scheduler_adaptive_batches VALUES(?,?,?,?,?,?,?)',
                           (DAY, 2, 1, 12, json.dumps(self.config), '2026-10-30T10:15:00Z', '[1]'))
            db.commit()

    def snapshot(self, config=None):
        config = config or self.config
        with ExitStack() as stack:
            for target, name, kwargs in [
                (accounts, 'ROOT', {'new': self.root}),
                (accounts, 'account_numbers', {'return_value': [1]}),
                (accounts, 'database_for_account', {'return_value': self.root / 'absent.sqlite3'}),
                (accounts, 'load_bindings', {'return_value': {'accounts': {}}}),
                (accounts, 'effective_policy', {'return_value': config}),
                (accounts, 'account_record', {'return_value': {'account': 1, 'enabled': True}}),
                (accounts, 'account_enabled', {'return_value': True}),
                (accounts, 'profile_for_account', {'return_value': self.root / 'absent-profile'}),
                (control, 'login_status', {'return_value': {'state': 'idle'}}),
                (control, 'datetime', {'new': FixedClock}),
                (ledger.State, '__init__', {'side_effect': AssertionError('snapshot must not initialize State')}),
            ]:
                stack.enter_context(patch.object(target, name, **kwargs))
            stack.enter_context(patch('vk_authentication.status', return_value={'state': 'unverified'}))
            stack.enter_context(patch('vk_connections.enabled', return_value=False))
            stack.enter_context(patch('vk_statistics_job.status', return_value={}))
            stack.enter_context(patch('vk_scheduler.load_settings', return_value=(config, {})))
            return control.snapshot()

    def test_adaptive_preactivation_does_not_apply_legacy_saved_policy_or_create_tables(self):
        before = self.path.read_bytes()
        result = self.snapshot()
        self.assertEqual(result['scheduler']['schedule_mode'], 'adaptive')
        self.assertEqual(result['scheduler']['policy_effective_scope'], 'next_batch')
        self.assertEqual(result['scheduler']['work_window'], {'start_hour': 0, 'end_hour': 24})
        self.assertEqual(result['accounts'][0]['effective_policy']['personal_reposts'], 0)
        self.assertEqual(result['accounts'][0]['usage']['like'], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cached_forecast_and_fixed_active_target_are_distinct_and_readonly(self):
        self.activate(running=True)
        before = self.path.read_bytes()
        result = self.snapshot()
        adaptive = result['scheduler']['adaptive']
        self.assertEqual(adaptive['forecast']['next_target'], 10)
        self.assertEqual(adaptive['active_batch']['target_attempts'], 12)
        self.assertEqual(adaptive['historical_cutover_at'], '2026-10-30T09:06:00Z')
        batches = result['scheduler']['batches']
        self.assertEqual(batches[0]['planning_mode'], 'legacy')
        self.assertEqual(batches[0]['status'], 'batch_action_deadline')
        self.assertEqual(batches[1]['planning_mode'], 'adaptive')
        self.assertEqual(batches[1]['target_attempts'], 12)
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_snapshot_remains_saved_day_policy(self):
        result = self.snapshot(self.legacy)
        self.assertNotIn('adaptive', result['scheduler'])
        self.assertEqual(result['accounts'][0]['effective_policy']['personal_reposts'], 10)
        self.assertEqual(result['scheduler']['slots'], ['2026-10-30T09:00:00Z', '2026-10-30T09:10:00Z'])

    def test_adaptive_timing_excludes_abandoned_old_slots_and_never_uses_forecast_boundary(self):
        self.activate(running=True)
        with closing(sqlite3.connect(self.path)) as db:
            before = db.total_changes
            report = timing.report(db, DAY, NOW, schedule_mode='adaptive')
            self.assertEqual(db.total_changes, before)
        self.assertEqual([row['slot'] for row in report['slots']], [0, 2])
        self.assertEqual(report['summary']['missed'], 0)
        legacy, adaptive = report['slots']
        self.assertEqual(legacy['N'], '2026-10-30T09:10:00+00:00')
        self.assertIsNone(adaptive['N'])
        self.assertEqual(adaptive['E'], '2026-10-31T00:00:00+05:00')
        self.assertEqual(adaptive['batch_capacity'], 12)
        self.assertFalse(adaptive['unclaimed_after_grace'])
        self.assertNotIn('late_claim', adaptive['causes'])

    def test_adaptive_timing_before_activation_is_readonly_and_preserves_real_legacy_batch(self):
        with closing(sqlite3.connect(self.path)) as db:
            before = db.total_changes
            report = timing.report(db, DAY, NOW, schedule_mode='adaptive')
            self.assertEqual(db.total_changes, before)
        self.assertEqual(len(report['slots']), 1)
        self.assertEqual(report['slots'][0]['planning_mode'], 'legacy')

    def test_first_adaptive_day_without_old_schedule_has_real_batch_and_midnight_timing(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DELETE FROM schedule_days')
            db.execute('DELETE FROM scheduler_batches')
            db.execute('DELETE FROM scheduler_jobs')
            db.commit()
        self.activate(running=True)
        before = self.path.read_bytes()
        result = self.snapshot()
        self.assertEqual(result['scheduler']['slots'], [])
        self.assertEqual(result['scheduler']['batch_size'], 10)
        self.assertEqual(result['scheduler']['batch_count'], 7)
        report = result['scheduler']['timing']
        self.assertNotIn('unavailable', report)
        self.assertEqual(len(report['slots']), 1)
        self.assertEqual(report['slots'][0]['Dintent'], '2026-10-30T23:59:00+05:00')
        self.assertEqual(self.path.read_bytes(), before)

    def test_adaptive_without_scheduler_file_does_not_create_it(self):
        self.path.unlink()
        result = self.snapshot()
        self.assertEqual(result['scheduler']['schedule_mode'], 'adaptive')
        self.assertIsNone(result['scheduler']['adaptive']['forecast'])
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
