import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock,patch

import vk_analytics as calc
import vk_analytics_store as store
import vk_analytics_scheduled as scheduled
from vk_analytics_collect import Deferred


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.groups=[dict(id=1,name='one',included=True,business_card=False),dict(id=2,name='two',included=True,business_card=False)]
        self.registry=self.root/'registry.json';self.registry.write_text(json.dumps(self.groups),encoding='utf8')
        self.manifest=self.root/'manifest.json';self.manifest.write_text(json.dumps(dict(period_start='2026-10-14',
            communities=2,registry_sha256=hashlib.sha256(self.registry.read_bytes()).hexdigest())),encoding='utf8')
        self.at=datetime.fromisoformat('2026-10-23T08:00:00+05:00')
        self.reader=Mock();self.reader.used=0
        self.reader.catalog_references.return_value=[]
        self.reader.wall_get.side_effect=lambda gid,*a:dict(count=1,items=[dict(owner_id=-gid,id=1,date=calc.stamp('2026-10-15')+3600,
            likes={'count':1},comments={'count':0},reposts={'count':0},views={'count':10},post_type='post')])
        self.factory=Mock(return_value=self.reader)

    def tearDown(self):self.tmp.cleanup()

    def run_job(self,**kwargs):
        return scheduled.run_job(self.root,self.manifest,self.registry,clock=lambda:self.at,reader_factory=self.factory,**kwargs)

    def db(self):return closing(store.open_db(self.root/'vk_analytics.sqlite3'))

    def test_time_slots_no_late_catchup(self):
        for time in ('07:59:59','08:15:00','09:00:00','16:00:00','19:59:59','20:15:00'):
            self.at=datetime.fromisoformat('2026-10-23T'+time+'+05:00')
            self.assertEqual(self.run_job()['status'],'skipped_outside_slot')
        self.factory.assert_not_called();self.assertFalse((self.root/'vk_analytics.sqlite3').exists())
        self.assertIsNotNone(scheduled.select_slot(datetime.fromisoformat('2026-10-23T20:14:59+05:00')))

    def test_duplicate_slot_reuses_finished_run_and_frozen_report(self):
        first=self.run_job();second=self.run_job()
        self.assertEqual(first['run_id'],second['run_id']);self.assertEqual(first['report_id'],second['report_id'])
        self.assertEqual(self.factory.call_count,1)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_runs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_reports').fetchone()[0],1)

    def test_registry_mismatch_never_calls_reader(self):
        self.registry.write_text('[]',encoding='utf8')
        with self.assertRaises(ValueError):self.run_job()
        self.factory.assert_not_called()

    def test_scheduled_pages_match_shared_queue_and_statistics_page_size(self):
        self.run_job()
        self.assertEqual([call.args[2] for call in self.reader.wall_get.call_args_list],[100,100])

    def test_worker_busy_yields_without_retry(self):
        self.reader.wall_get.side_effect=Deferred('busy')
        r=self.run_job()
        self.assertEqual(r['status'],'deferred_worker');self.assertEqual(self.reader.wall_get.call_count,1)
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],0)

    def test_deadline_before_next_request(self):
        original=self.reader.wall_get.side_effect
        def read(*args):
            value=original(*args);self.at+=timedelta(minutes=26);return value
        self.reader.wall_get.side_effect=read
        r=self.run_job()
        self.assertEqual(r['status'],'deferred_deadline');self.assertEqual(self.reader.wall_get.call_count,1)

    def test_interrupted_slot_resumes_same_run_without_duplicate_snapshots(self):
        original=self.reader.wall_get.side_effect
        self.reader.wall_get.side_effect=lambda gid,*args:original(gid,*args) if gid==1 else (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):self.run_job()
        with self.db() as db:rid=db.execute('SELECT id FROM analytics_runs').fetchone()[0]
        self.reader.wall_get.side_effect=original
        r=self.run_job();self.assertEqual(r['run_id'],rid)
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],2)

    def test_api_error_no_retry_and_fixed_code_only(self):
        self.reader.wall_get.side_effect=RuntimeError('SECRET sentinel must not be logged')
        r=self.run_job();self.assertEqual(r['status'],'read_error')
        self.assertEqual(self.reader.wall_get.call_count,1)
        for path in self.root.rglob('*.json*'):
            self.assertNotIn('SECRET',path.read_text(encoding='utf8'))

    def test_shared_analytics_guard_prevents_parallel_process(self):
        from vk_scheduler import singleton
        with singleton(self.root/'.vk-analytics.guard'):
            self.assertEqual(self.run_job()['status'],'skipped_analytics_busy')
        self.factory.assert_not_called()

    def test_null_counter_remains_empty_in_raw_csv(self):
        original=self.reader.wall_get.side_effect
        def read(*args):
            r=original(*args);r['items'][0].pop('views');return r
        self.reader.wall_get.side_effect=read;r=self.run_job()
        self.assertEqual(r['completeness'],'partial')
        import csv
        with (Path(r['output'])/'posts.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f,delimiter=';'))
        self.assertEqual([row['views'] for row in rows],['',''])

    def test_crash_after_api_error_must_not_retry_failed_request(self):
        original=scheduled.atomic_json
        def crash(path,value):
            if value.get('phase')=='collected':raise KeyboardInterrupt()
            return original(path,value)
        self.reader.wall_get.side_effect=RuntimeError('temporary read error')
        with patch.object(scheduled,'atomic_json',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.run_job()
        result=self.run_job()
        self.assertEqual(result['status'],'read_error');self.assertEqual(self.reader.wall_get.call_count,1)

    def test_crash_after_freeze_reuses_frozen_report_without_api(self):
        with patch.object(scheduled,'export_report',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):self.run_job()
        self.run_job();self.assertEqual(self.factory.call_count,1)
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_reports').fetchone()[0],1)

    def test_slot_read_budget_survives_interruption(self):
        self.reader.wall_get.side_effect=KeyboardInterrupt()
        with patch.object(scheduled,'MAX_READS',1):
            with self.assertRaises(KeyboardInterrupt):self.run_job()
            result=self.run_job()
        self.assertEqual(result['status'],'deferred_budget');self.assertEqual(self.reader.wall_get.call_count,1)

    def test_crash_before_initial_checkpoint_keeps_one_slot_run(self):
        with patch.object(scheduled,'atomic_json',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):self.run_job()
        self.run_job()
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_runs').fetchone()[0],1)


if __name__=='__main__':unittest.main()
