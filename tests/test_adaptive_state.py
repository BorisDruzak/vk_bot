import copy
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vk_batch as engine
import vk_schedule_policy as policy


class AdaptiveStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {i: self.root / f'{i}.sqlite3' for i in (1, 2)}
        for path in self.paths.values():
            with closing(engine.db_open(path)):
                pass
        self.now = datetime(2026, 10, 30, 4, tzinfo=timezone.utc)
        self.cfg = policy.validate_config(dict(schedule_mode='adaptive', start_hour=0, end_hour=24))

    def state(self):
        from vk_adaptive_state import AdaptiveState
        state = AdaptiveState(self.root / 'scheduler.sqlite3', self.paths, self.cfg)
        state.source_path = self.paths[1]
        state.groups_count = 2
        self.addCleanup(state.close)
        return state

    def test_archive_unchanged_and_ids_after_all_old_slots(self):
        state = self.state()
        old = ('2026-10-30', '{"legacy":true}', json.dumps(['old'] * 67))
        state.db.execute('INSERT INTO schedule_days VALUES(?,?,?)', old)
        state.db.commit()
        state.ensure_day(self.now)
        self.assertEqual(tuple(state.db.execute('SELECT * FROM schedule_days').fetchone()), old)
        state.update_forecast(self.now)
        self.assertEqual(state.next_slot(self.now), 67)
        self.assertTrue(state.claim_batch(self.now, 67))
        self.assertFalse(state.claim_batch(self.now, 68))

    def test_rest_measured_after_slow_finish_survives_restart(self):
        state = self.state()
        state.ensure_day(self.now)
        state.update_forecast(self.now)
        slot = state.next_slot(self.now)
        self.assertTrue(state.claim_batch(self.now, slot))
        finish = self.now + timedelta(minutes=40)
        state.finish_batch(finish, slot, 'complete')
        other = self.state()
        self.assertEqual(other.rest_ready_at(finish), finish + timedelta(seconds=540))
        other.update_forecast(finish)
        self.assertFalse(other.claim_batch(finish + timedelta(seconds=539), slot + 1))
        self.assertTrue(other.claim_batch(finish + timedelta(seconds=540), slot + 1))

    def test_policy_frozen_only_for_active_batch(self):
        state = self.state()
        pending = copy.deepcopy(self.cfg)
        state.policy_provider = lambda: pending
        state.ensure_day(self.now)
        state.update_forecast(self.now)
        slot = state.next_slot(self.now)
        self.assertTrue(state.claim_batch(self.now, slot))
        pending['accounts']['1']['likes'] = 12
        state.refresh_policy(self.now)
        self.assertEqual(state.config['accounts']['1']['likes'], 50)
        state.finish_batch(self.now + timedelta(minutes=5), slot, 'complete')
        state.refresh_policy(self.now)
        self.assertEqual(state.config['accounts']['1']['likes'], 12)

    def test_attempt_including_error_consumes_remaining(self):
        state = self.state()
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(1,1,'like',?)", (self.now.isoformat(),))
            db.commit()
        state.ensure_day(self.now)
        forecast = state.update_forecast(self.now)
        self.assertEqual(forecast['remaining_attempts'], 159)

    def test_recovery_keeps_orphan_history_and_rest(self):
        state = self.state()
        state.ensure_day(self.now)
        state.update_forecast(self.now)
        slot = state.next_slot(self.now)
        self.assertTrue(state.claim_batch(self.now, slot))
        state._batch_clock = lambda: self.now + timedelta(minutes=10)
        state.recover()
        row = state.db.execute('SELECT * FROM scheduler_batches').fetchone()
        self.assertIsNone(row['finished_at'])
        self.assertEqual(state.rest_ready_at(self.now + timedelta(minutes=10)), self.now + timedelta(minutes=19))
        self.assertIsNone(state.active_row(self.now))

    def test_midnight_rest_preserved_and_day_tail_closed(self):
        state = self.state()
        end = state.calendar_end(self.now)
        self.assertTrue(state.action_time_closed(end - timedelta(seconds=120)))
        self.assertFalse(state.action_time_closed(end - timedelta(seconds=121)))
        state.db.execute("INSERT INTO scheduler_batches VALUES(?,0,?,?,'complete')", ('2026-10-30', (end - timedelta(minutes=2)).isoformat(), (end - timedelta(minutes=1)).isoformat()))
        state.db.commit()
        self.assertEqual(state.rest_ready_at(end), end + timedelta(minutes=8))

    def test_readonly_view_before_activation_has_no_writes(self):
        state = self.state()
        before = state.db.total_changes
        view = state.adaptive_view(self.now)
        self.assertEqual(view['rest_seconds'], 540)
        self.assertEqual(state.db.total_changes, before)

    def test_automatic_block_expires_next_dynamic_day_without_static_slots(self):
        state = self.state()
        state.pause('OSError', now=self.now)
        state.ensure_day(self.now)
        self.assertFalse(state.expire_automatic_block(self.now))
        later = self.now + timedelta(days=1)
        state.ensure_day(later)
        self.assertTrue(state.expire_automatic_block(later))
        self.assertIsNone(state.get('paused'))
        self.assertGreater(state.get('resume_not_before'), later.timestamp() + 60)
        forecast = state.update_forecast(later)
        self.assertGreaterEqual(datetime.fromisoformat(forecast['next_start']).timestamp(), state.get('resume_not_before'))

    def test_manual_release_uses_dynamic_future_gate_and_keeps_manual_persistence(self):
        state = self.state()
        state.ensure_day(self.now)
        state.pause('manual_block', kind='manual', now=self.now)
        self.assertFalse(state.expire_automatic_block(self.now + timedelta(days=1)))
        self.assertTrue(state.clear_manual_block(now=self.now))
        self.assertGreater(state.get('resume_not_before'), self.now.timestamp() + 60)

    def test_long_resume_gate_never_grants_early_batch(self):
        state = self.state()
        state.ensure_day(self.now)
        state.set('resume_not_before', (self.now + timedelta(hours=2)).timestamp())
        forecast = state.update_forecast(self.now)
        self.assertGreaterEqual(datetime.fromisoformat(forecast['next_start']), self.now + timedelta(hours=2))
        self.assertFalse(state.claim_batch(self.now, state.next_slot(self.now)))

    def test_owned_boundary_does_not_roll_forward_after_midnight(self):
        from vk_adaptive_worker import AdaptiveWorker
        state = self.state()
        end = state.calendar_end(self.now)
        worker = AdaptiveWorker(state, self.paths[1], {'groups': []}, clock=lambda: end + timedelta(seconds=1))
        state._batch_boundary = end
        self.assertEqual(worker.batch_boundary(0), end)
        self.assertEqual(worker.identity_read_deadline(0), end - timedelta(seconds=120))
        self.assertTrue(state.action_time_closed(end + timedelta(seconds=1)))

    def test_forecast_samples_clock_after_concurrent_api_snapshot(self):
        state = self.state()
        state.planning_clock = lambda: self.now + timedelta(seconds=3)
        state._api_attempts = lambda now: [(self.now + timedelta(seconds=2)).isoformat()]
        forecast = state.update_forecast(self.now)
        self.assertEqual(forecast['api_available'], 4999)
        self.assertGreaterEqual(datetime.fromisoformat(forecast['next_start']), self.now + timedelta(seconds=3))


if __name__ == '__main__':
    unittest.main()
