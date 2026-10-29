import copy
import json
import sqlite3
import unittest
from datetime import datetime, timedelta

import vk_scheduler_timing as timing


def at(value):
    return datetime.fromisoformat('2026-10-29T'+value+'+05:00')


class EffectiveWindowTests(unittest.TestCase):
    def test_incident_three_attempts_is_explained_without_authorizing_fourth(self):
        marks={'claimed_at':at('09:29:21'),'refresh_finished_at':at('09:29:57'),
               'spacing_ready_at':at('09:34:38'),'first_action_at':at('09:35:26'),
               'finished_at':at('09:38:13')}
        before=copy.deepcopy(marks)
        row=timing.effective_window(at('09:29:21'),at('09:38:56'),at('22:00:00'),
                                    observations=marks,now=at('09:38:13'),phase='batch_action_deadline')
        self.assertEqual(row['Dread'],at('09:36:56').isoformat())
        self.assertEqual(row['Dintent'],at('09:37:56').isoformat())
        self.assertEqual(row['click_deadline'],at('09:38:36').isoformat())
        self.assertEqual(row['intent_seconds_available'],198)
        self.assertIn('spacing_wait',row['causes'])
        self.assertIn('action_deadline',row['causes'])
        self.assertEqual(marks,before)
        self.assertNotIn('allowed',row)

    def test_missing_measurements_are_unknown_and_no_next_slot_uses_end(self):
        row=timing.effective_window(at('21:50:00'),None,at('22:00:00'),now=at('21:50:01'))
        self.assertIsNone(row['intent_seconds_available'])
        self.assertIsNone(row['first_action_at'])
        self.assertEqual(row['Dread'],at('21:58:00').isoformat())
        self.assertEqual(row['click_deadline'],at('21:59:40').isoformat())

    def test_deadline_edges_and_late_claim(self):
        for second,closed in [(59,False),(60,True),(61,True)]:
            now=at('09:09:00')+timedelta(seconds=second-60)
            row=timing.effective_window(at('09:00:00'),at('09:10:00'),at('22:00:00'),
                observations={'claimed_at':at('09:02:00')},now=now)
            self.assertEqual(row['intent_closed'],closed)
            self.assertFalse(row['claim_grace_exceeded'])
        row=timing.effective_window(at('09:00:00'),at('09:10:00'),at('22:00:00'),
            observations={'claimed_at':at('09:02:01')},now=at('09:03:00'))
        self.assertTrue(row['claim_grace_exceeded'])
        self.assertIn('late_claim',row['causes'])

    def test_reason_families_and_day_rollover(self):
        cases={'counter_refresh_deadline':'counter_refresh_deadline','queue_empty':'no_candidate',
               'batch_daily_quota_reached':'quota','attention':'guard_block'}
        for phase,cause in cases.items():
            self.assertIn(cause,timing.effective_window(at('09:00:00'),None,at('22:00:00'),
                now=at('09:10:00'),phase=phase)['causes'])
        row=timing.effective_window(at('21:50:00'),None,at('22:00:00'),
            now=at('22:00:00')+timedelta(hours=3))
        self.assertTrue(row['intent_closed'])

    def test_timing_sanitizer_drops_private_and_invalid_fields(self):
        self.assertEqual(timing.sanitize_window({'S':at('09:00:00').isoformat(),
            'claimed_at':'secret','causes':['quota','secret'], 'cookie':'private'}),
            {'S':at('09:00:00').isoformat(),'causes':['quota']})

    def test_exhausted_quota_and_malformed_stage_samples_are_distinct(self):
        row=timing.effective_window(at('09:00:00'),None,at('22:00:00'),
            now=at('09:01:00'),phase='queue_empty',reason='quota')
        self.assertIn('quota',row['causes'])
        self.assertNotIn('no_candidate',row['causes'])
        from vk_action_timing import summarize
        self.assertIsNone(summarize([{'timing_stage':[],'duration_ms':10}])['click']['p95_ms'])


class ReportTests(unittest.TestCase):
    def make_report_db(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.executescript('CREATE TABLE schedule_days(day,config,slots);'
          'CREATE TABLE scheduler_batches(day,slot,started_at,finished_at,status);'
          'CREATE TABLE scheduler_jobs(id,day,slot,status,attempt_id);'
          'CREATE TABLE scheduler_diagnostics(id,at,event,snapshot);')
        db.execute('INSERT INTO schedule_days VALUES(?,?,?)',('2026-10-29',json.dumps({'end_hour':22,'batch_size':4}),json.dumps([at('09:00:00').isoformat()])))
        return db

    def test_malformed_or_nullable_telemetry_does_not_break_read_only_report(self):
        db=self.make_report_db()
        for i,value in enumerate([[],None,{'at':at('09:10:00').isoformat(),'event':'stage_timing','job':[],
                                         'timing_stage':'click','duration_ms':10},
                          {'at':at('09:10:00').isoformat(),'event':'batch_timing','slot':0,
                         'effective_window':{'attempts':None,'verified':False}}]):
            db.execute('INSERT INTO scheduler_diagnostics VALUES(?,?,?,?)',
                (i,at('09:10:00').isoformat(),'batch_timing',json.dumps(value)))
        result=timing.report(db,'2026-10-29',at('10:00:00'))
        self.assertEqual(result['summary']['attempts'],0)
        self.assertEqual(result['summary']['verified'],0)

    def test_completed_batch_current_deadline_is_not_reported_as_still_open(self):
        db=self.make_report_db()
        db.execute('INSERT INTO scheduler_batches VALUES(?,?,?,?,?)',('2026-10-29',0,
            at('09:00:00').isoformat(),at('09:04:00').isoformat(),'batch_complete'))
        row=timing.report(db,'2026-10-29',at('22:01:00'))['slots'][0]
        self.assertTrue(row['intent_closed'])
        self.assertEqual(row['remaining_before_intent_seconds'],0)

    def test_read_only_report_retains_all_slots_and_missing_telemetry(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.executescript('CREATE TABLE schedule_days(day,config,slots);'
          'CREATE TABLE scheduler_batches(day,slot,started_at,finished_at,status);'
          'CREATE TABLE scheduler_jobs(id,day,slot,status,attempt_id);'
          'CREATE TABLE scheduler_diagnostics(id,at,event,snapshot);')
        slots=[at('09:00:00').isoformat(),at('09:10:00').isoformat(),at('21:50:00').isoformat()]
        db.execute('INSERT INTO schedule_days VALUES(?,?,?)',('2026-10-29',json.dumps({'end_hour':22,'batch_size':4}),json.dumps(slots)))
        db.execute('INSERT INTO scheduler_batches VALUES(?,?,?,?,?)',('2026-10-29',0,slots[0],at('09:08:00').isoformat(),'batch_action_deadline'))
        db.executemany('INSERT INTO scheduler_jobs VALUES(?,?,?,?,?)',[(1,'2026-10-29',0,'verified',10),(2,'2026-10-29',0,'uncertain',11)])
        db.commit();before=db.total_changes
        report=timing.report(db,'2026-10-29',at('10:00:00'))
        self.assertEqual(len(report['slots']),3)
        self.assertEqual(report['summary']['theoretical_attempts'],12)
        self.assertEqual(report['summary']['verified'],1)
        self.assertEqual(report['slots'][0]['attempts'],2)
        self.assertFalse(report['slots'][0]['telemetry_available'])
        self.assertIsNone(report['slots'][0]['refresh_finished_at'])
        self.assertEqual(report['slots'][1]['status'],'missed')
        self.assertEqual(report['slots'][2]['status'],'scheduled')
        self.assertEqual(db.total_changes,before)


class WorkerTimingTests(unittest.TestCase):
    from tests.test_batch_spacing_wait import BatchSpacingWaitTests as Fixtures
    setUp=Fixtures.setUp
    setup_batch=Fixtures.setup_batch
    run_real_callbacks=Fixtures.run_real_callbacks

    def test_real_callbacks_observe_claim_refresh_spacing_and_intent_without_plan_change(self):
        worker=self.setup_batch()
        result,executed=self.run_real_callbacks(worker)
        self.assertEqual(result['attempted'],4)
        report=timing.report(self.state.db,'2026-10-24',self.now)
        row=report['slots'][1]
        self.assertTrue(row['telemetry_available'])
        self.assertEqual(row['attempts'],4)
        self.assertEqual(row['verified'],4)
        self.assertEqual(row['spacing_wait_seconds'],41)
        self.assertEqual(row['first_action_at'],executed[0].isoformat())
        self.assertEqual(row['claimed_at'],datetime.fromisoformat('2026-10-24T05:00:30+00:00').isoformat())

    def test_broken_timing_report_preserves_action_result_and_all_four_reservations(self):
        from unittest.mock import patch
        worker=self.setup_batch()
        with patch.object(timing,'effective_window',side_effect=RuntimeError('private')):
            result,executed=self.run_real_callbacks(worker)
        self.assertEqual(len(executed),4)
        self.assertEqual(result['phase'],'batch_complete')
        self.assertFalse(self.state.blocked())

    def test_failed_refresh_has_measured_finish_without_fabricated_spacing_ready(self):
        from unittest.mock import Mock
        worker=self.setup_batch()
        def failed_refresh():
            self.now+=timedelta(seconds=70)
            return {'state':'deadline','pool':set(),'report':{}}
        worker.refresh_batch=Mock(side_effect=failed_refresh)
        result=worker.run_batch(1)
        self.assertEqual(result['attempted'],0)
        row=timing.report(self.state.db,'2026-10-24',self.now)['slots'][1]
        self.assertEqual(row['refresh_finished_at'],self.now.isoformat())
        self.assertIsNone(row['spacing_ready_at'])
