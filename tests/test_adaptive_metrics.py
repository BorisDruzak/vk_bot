import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from vk_adaptive_metrics import estimate_costs


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
            CREATE TABLE scheduler_jobs(id INTEGER PRIMARY KEY,kind TEXT);
            CREATE TABLE scheduler_action_passes(job_id INTEGER, pass INTEGER,
                attempt_id INTEGER,outcome TEXT,started_at TEXT,finished_at TEXT);
            CREATE TABLE scheduler_diagnostics(id INTEGER PRIMARY KEY,at TEXT,event TEXT,snapshot TEXT);
            CREATE TABLE scheduler_meta(key TEXT PRIMARY KEY,value TEXT);
        ''')

    def tearDown(self):
        self.db.close()

    def action(self, job, kind, seconds, *, attempt=1, finish=True, start=None):
        self.db.execute('INSERT OR IGNORE INTO scheduler_jobs VALUES(?,?)',(job,kind))
        start = start or NOW-timedelta(hours=1)
        self.db.execute('INSERT INTO scheduler_action_passes VALUES(?,1,?,?,?,?)',
            (job,attempt,'verified',start.isoformat(),(start+timedelta(seconds=seconds)).isoformat() if finish else None))

    def event(self, event, data, at=None):
        at=at or NOW-timedelta(minutes=5)
        self.db.execute('INSERT INTO scheduler_diagnostics(at,event,snapshot) VALUES(?,?,?)',
            (at.isoformat(),event,json.dumps({'at':at.isoformat(),'event':event,**data})))

    def test_empty_database_has_conservative_finite_private_free_fallback(self):
        result=estimate_costs(self.db,NOW,groups_count=64)
        self.assertEqual(result['sample_counts']['actions'],{'like':0,'personal':0,'community':0})
        self.assertTrue(all(x>=20 for x in result['action_seconds'].values()))
        self.assertGreaterEqual(result['refresh_seconds'],5*(result['refresh_requests']-1))
        self.assertIn('fallback',result['model_reason'])
        json.dumps(result,allow_nan=False)

    def test_pass_elapsed_is_counted_once_and_uses_upper_percentile(self):
        for n in range(1,21):self.action(n,'community',n*10)
        self.event('stage_timing',{'kind':'community','duration_ms':99999999})
        result=estimate_costs(self.db,NOW)
        self.assertEqual(result['sample_counts']['actions']['community'],20)
        self.assertAlmostEqual(result['observed_p95_seconds']['community'],190)
        self.assertAlmostEqual(result['action_seconds']['community'],218.5)

    def test_incomplete_unattempted_malformed_stale_and_future_samples_are_excluded(self):
        self.action(1,'like',60)
        self.action(2,'like',1000,finish=False)
        self.action(3,'like',1000,attempt=None)
        self.action(4,'like',1000,start=NOW-timedelta(days=8))
        self.action(5,'like',1000,start=NOW+timedelta(hours=1))
        self.action(6,'like',-100)
        self.db.execute("INSERT INTO scheduler_action_passes VALUES(1,2,2,'failed','broken','also broken')")
        result=estimate_costs(self.db,NOW)
        self.assertEqual(result['sample_counts']['actions']['like'],1)
        self.assertEqual(result['action_seconds']['like'],69)

    def test_refresh_uses_claim_to_finished_and_never_underestimates_pacing(self):
        start=NOW-timedelta(minutes=15)
        self.event('batch_timing',{'slot':3,'effective_window':{
            'claimed_at':start.isoformat(),'refresh_finished_at':(start+timedelta(seconds=300)).isoformat()}})
        self.event('counter_progress',{'requests':90},at=start+timedelta(seconds=299))
        result=estimate_costs(self.db,NOW)
        self.assertEqual(result['sample_counts']['refreshes'],1)
        self.assertGreaterEqual(result['refresh_requests'],90)
        self.assertGreaterEqual(result['refresh_seconds'],5*(result['refresh_requests']-1))

    def test_completed_refresh_metadata_is_usable_and_duplicate_timing_not_double_counted(self):
        start=NOW-timedelta(minutes=10)
        end=start+timedelta(seconds=320)
        timing={'claimed_at':start.isoformat(),'refresh_finished_at':end.isoformat()}
        for _ in range(2):self.event('batch_timing',{'slot':4,'effective_window':timing})
        self.db.execute('INSERT INTO scheduler_meta VALUES(?,?)',('last_counter_refresh',json.dumps({
            'started_at':start.isoformat(),'finished_at':end.isoformat(),'requests':64})))
        result=estimate_costs(self.db,NOW)
        self.assertEqual(result['sample_counts']['refreshes'],1)
        self.assertGreaterEqual(result['refresh_requests'],64)
        self.assertNotIn('slot',json.dumps(result))

    def test_read_only_connection_and_missing_optional_tables_do_not_write(self):
        self.db.execute('DROP TABLE scheduler_action_passes')
        self.db.execute('DROP TABLE scheduler_meta')
        self.db.commit()
        self.db.execute('PRAGMA query_only=ON')
        before=self.db.total_changes
        result=estimate_costs(self.db,NOW)
        self.assertEqual(self.db.total_changes,before)
        self.assertGreater(result['refresh_requests'],0)


if __name__=='__main__':unittest.main()
