"""Manual blocks persist; automatic blocks expire only into future saved slots."""
import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
import vk_scheduler_state as ledger


class BlockLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'scheduler.sqlite3'
        self.state=ledger.State(self.path,{});self.addCleanup(self.state.close)
        self.now=datetime(2026,10,30,5,tzinfo=timezone.utc)

    def plan(self,now):
        slots=[now-timedelta(minutes=5),now+timedelta(seconds=10),now+timedelta(minutes=10)]
        from vk_schedule_policy import local_day
        self.state.db.execute('INSERT INTO schedule_days VALUES(?,?,?)',(str(local_day(now)),json.dumps(self.state.config),json.dumps([x.isoformat() for x in slots])));self.state.db.commit()
        return slots

    def test_state_exposes_typed_block_status(self):
        self.assertTrue(callable(getattr(self.state,'block_status',None)))

    def test_manual_never_expires_and_automatic_error_cannot_replace_it(self):
        self.state.pause('manual_block',kind='manual',now=self.now)
        self.state.pause('OSError',now=self.now+timedelta(days=1))
        self.plan(self.now+timedelta(days=1))
        self.assertFalse(self.state.expire_automatic_block(self.now+timedelta(days=1)))
        self.assertEqual(self.state.get('paused'),'manual_block')
        self.assertEqual(self.state.block_status()['kind'],'manual')

    def test_auto_expires_next_saved_day_with_future_gate_and_no_plan_changes(self):
        self.state.pause('OSError',now=self.now);self.plan(self.now)
        self.assertFalse(self.state.expire_automatic_block(self.now))
        later=self.now+timedelta(days=1);slots=self.plan(later)
        before=[tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')]
        self.assertTrue(self.state.expire_automatic_block(later))
        self.assertIsNone(self.state.get('paused'))
        self.assertEqual(self.state.get('resume_not_before'),slots[-1].timestamp())
        self.assertEqual([tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')],before)

    def test_auto_does_not_expire_without_saved_plan_or_remaining_slot(self):
        self.state.pause('OSError',now=self.now)
        self.assertFalse(self.state.expire_automatic_block(self.now+timedelta(days=1)))
        self.plan(self.now+timedelta(days=1))
        self.assertFalse(self.state.expire_automatic_block(self.now+timedelta(days=1,hours=1)))
        self.assertEqual(self.state.get('paused'),'OSError')

    def test_legacy_or_mismatched_metadata_is_manual(self):
        self.state.set('paused','old_pause');self.plan(self.now)
        self.assertEqual(self.state.block_status()['kind'],'manual')
        self.assertFalse(self.state.expire_automatic_block(self.now))
        self.state.pause('OSError',now=self.now);self.state.set('paused','different')
        self.assertEqual(self.state.block_status()['kind'],'manual')

    def test_restart_retains_automatic_origin_day(self):
        self.state.pause('OSError',now=self.now)
        other=ledger.State(self.path,{});self.addCleanup(other.close)
        self.assertEqual(other.block_status()['day'],'2026-10-30')
        self.assertEqual(other.block_status()['kind'],'automatic')

    def test_stop_request_and_running_work_prevent_automatic_release(self):
        self.state.pause('OSError',now=self.now);later=self.now+timedelta(days=1);self.plan(later)
        self.state.set('stop_requested',True)
        self.assertFalse(self.state.expire_automatic_block(later))
        self.state.set('stop_requested',False)
        self.state.db.execute('INSERT INTO scheduler_batches VALUES(?,?,?,NULL,?)',('2026-10-31',0,later.isoformat(),'running'));self.state.db.commit()
        self.assertFalse(self.state.expire_automatic_block(later))

    def test_only_explicit_manual_operator_block_can_be_cleared_by_operator_path(self):
        self.state.pause('manual_block',kind='manual',now=self.now)
        slots=self.plan(self.now)
        self.assertTrue(self.state.clear_manual_block(now=self.now))
        self.assertIsNone(self.state.get('paused'))
        self.assertEqual(self.state.get('resume_not_before'),slots[-1].timestamp())
        self.state.set('paused','legacy_protection')
        self.assertFalse(self.state.clear_manual_block(now=self.now))

    def test_manual_release_does_not_erase_an_existing_automatic_block(self):
        self.plan(self.now);self.state.pause('OSError',now=self.now)
        self.state.pause('manual_block',kind='manual',now=self.now)
        self.assertTrue(self.state.clear_manual_block(now=self.now))
        self.assertEqual(self.state.get('paused'),'OSError')
        self.assertEqual(self.state.block_status()['kind'],'automatic')

    def test_failures_under_manual_switch_cannot_replace_suspended_original_block(self):
        self.plan(self.now);self.state.pause('OSError',now=self.now)
        self.state.pause('manual_block',kind='manual',now=self.now)
        self.state.pause('RuntimeError',now=self.now)
        self.assertTrue(self.state.clear_manual_block(now=self.now))
        self.assertEqual(self.state.get('paused'),'OSError')

    def test_failure_due_to_manual_guard_does_not_create_an_extra_automatic_block(self):
        self.plan(self.now);self.state.pause('manual_block',kind='manual',now=self.now)
        self.state.pause('RuntimeError',now=self.now)
        self.assertTrue(self.state.clear_manual_block(now=self.now))
        self.assertIsNone(self.state.get('paused'))

    def test_expiration_resets_failure_streak_without_changing_history(self):
        self.state.pause('OSError',now=self.now)
        self.state.set('action_failure_streak',3);self.state.set('technical_failures',{'read':3,'action':2})
        later=self.now+timedelta(days=1);self.plan(later)
        self.assertTrue(self.state.expire_automatic_block(later))
        self.assertEqual(self.state.get('action_failure_streak'),0)
        self.assertEqual(self.state.get('technical_failures'),{'read':0,'action':0})

    def test_manual_switch_cannot_overwrite_and_clear_a_legacy_guard(self):
        self.state.set('paused','legacy_protection');self.plan(self.now)
        self.assertFalse(self.state.pause('manual_block',kind='manual',now=self.now))
        self.assertFalse(self.state.clear_manual_block(now=self.now))
        self.assertEqual(self.state.get('paused'),'legacy_protection')

    def test_historical_empty_orphan_batch_does_not_block_next_day_forever(self):
        self.state.pause('OSError',now=self.now)
        self.state.db.execute('INSERT INTO scheduler_batches VALUES(?,?,?,NULL,?)',('2026-10-30',1,self.now.isoformat(),'running'));self.state.db.commit()
        before=tuple(self.state.db.execute('SELECT * FROM scheduler_batches').fetchone())
        later=self.now+timedelta(days=1);self.plan(later);self.state.recover()
        self.assertTrue(self.state.expire_automatic_block(later))
        self.assertEqual(tuple(self.state.db.execute('SELECT * FROM scheduler_batches').fetchone()),before)

    def test_automatic_expiration_never_acknowledges_unrelated_uncertainty(self):
        self.state.pause('OSError',now=self.now)
        self.state.db.execute("""INSERT INTO scheduler_jobs(day,slot,account,kind,community_id,group_id,post_id,published_at,status,created_at,attempt_id)
            VALUES('2026-10-30',0,1,'like',0,101,1,1,'uncertain',?,1)""",(self.now.isoformat(),));self.state.db.commit()
        later=self.now+timedelta(days=1);self.plan(later)
        self.assertTrue(self.state.expire_automatic_block(later))
        self.assertIsNone(self.state.get('paused'))
        self.assertTrue(self.state.blocked())
        self.assertEqual(self.state.active_uncertain_count(),1)
