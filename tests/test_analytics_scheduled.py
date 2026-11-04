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

    def advance(self,seconds):self.at+=timedelta(seconds=seconds)

    def checkpoint(self):
        path=next(self.root.rglob('checkpoint.json'))
        return json.loads(path.read_text(encoding='utf8'))

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

    def test_worker_contention_waits_then_collects_both_groups(self):
        original=self.reader.wall_get.side_effect
        remaining=[True]
        def read(*args):
            if remaining:
                remaining.pop();raise Deferred('worker_or_profile_busy')
            return original(*args)
        self.reader.wall_get.side_effect=read
        result=self.run_job(sleeper=self.advance)
        self.assertEqual(result['status'],'saved')
        self.assertEqual(result['posts'],2)
        self.assertEqual(result['attempted_reads'],2)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_reports').fetchone()[0],1)

    def test_repeated_worker_contention_does_not_spend_page_reservations(self):
        original=self.reader.wall_get.side_effect
        remaining=[True]*3
        waits=[]
        def read(*args):
            if remaining:
                remaining.pop();raise Deferred('worker_or_profile_busy')
            return original(*args)
        def sleep(seconds):waits.append(seconds);self.advance(seconds)
        self.reader.wall_get.side_effect=read
        with patch.object(scheduled,'MAX_READS',2):result=self.run_job(sleeper=sleep)
        self.assertEqual((result['status'],result['posts'],result['attempted_reads']),('saved',2,2))
        self.assertEqual(waits,[5,5,5])

    def test_contention_after_first_group_resumes_exact_page(self):
        original=self.reader.wall_get.side_effect
        busy=[True]
        def read(gid,offset,count):
            if gid==2 and busy:
                busy.pop();raise Deferred('worker_or_profile_busy')
            return original(gid,offset,count)
        self.reader.wall_get.side_effect=read
        result=self.run_job(sleeper=self.advance)
        self.assertEqual((result['status'],result['posts'],result['attempted_reads']),('saved',2,2))
        self.assertEqual([c.args for c in self.reader.wall_get.call_args_list],[(1,0,100),(2,0,100),(2,0,100)])
        with self.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT group_id FROM analytics_snapshots ORDER BY group_id')],[1,2])

    def test_continuous_contention_stops_at_saved_cutoff(self):
        self.at=datetime.fromisoformat('2026-10-23T08:14:59+05:00')
        waits=[];calls=[]
        def read(*args):
            calls.append(self.at)
            if len(calls)==1:self.advance(2)
            raise Deferred('worker_or_profile_busy')
        def sleep(seconds):waits.append(seconds);self.advance(seconds)
        self.reader.wall_get.side_effect=read
        result=self.run_job(sleeper=sleep)
        self.assertEqual((result['status'],result['posts']),('deferred_deadline',0))
        self.assertEqual(self.at,datetime.fromisoformat('2026-10-23T08:39:59+05:00'))
        self.assertTrue(all(0<seconds<=5 for seconds in waits));self.assertEqual(waits[-1],3)
        self.assertTrue(all(at<self.at for at in calls))
        self.assertEqual(result['attempted_reads'],1)

    def test_interrupted_contention_preserves_run_and_original_deadline(self):
        self.reader.wall_get.side_effect=Deferred('worker_or_profile_busy')
        def interrupted_sleep(seconds):self.advance(420);raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.run_job(sleeper=interrupted_sleep)
        saved=self.checkpoint()
        self.assertEqual(saved['phase'],'collecting')
        self.assertEqual(saved['deadline'],'2026-10-23T08:25:00+05:00')
        with self.db() as db:
            self.assertIsNone(db.execute('SELECT finished_at FROM analytics_runs').fetchone()[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analytics_reports').fetchone()[0],0)
        result=self.run_job(sleeper=self.advance)
        self.assertEqual(result['run_id'],saved['run_id'])
        self.assertEqual(result['status'],'deferred_deadline')
        self.assertEqual(self.at,datetime.fromisoformat('2026-10-23T08:25:00+05:00'))
        self.assertEqual(self.checkpoint()['deadline'],saved['deadline'])

    def test_read_error_after_contention_is_not_retried(self):
        self.reader.wall_get.side_effect=[Deferred('worker_or_profile_busy'),RuntimeError('private transport text')]
        waits=[]
        def sleep(seconds):waits.append(seconds);self.advance(seconds)
        result=self.run_job(sleeper=sleep)
        self.assertEqual((result['status'],result['posts'],result['attempted_reads']),('read_error',0,1))
        self.assertEqual(waits,[5]);self.assertEqual(self.reader.wall_get.call_count,2)
        self.assertEqual(self.run_job(sleeper=sleep),result)
        self.assertEqual(self.reader.wall_get.call_count,2)

    def test_deadline_callback_after_contention_is_not_treated_as_busy(self):
        before_request=[];waits=[];network=[];busy=[True]
        def factory(root,**kwargs):
            before_request.append(kwargs['before_request']);return self.reader
        def read(*args):
            if busy:
                busy.pop();raise Deferred('worker_or_profile_busy')
            self.advance(1500)
            before_request[0]()
            network.append(args)
            raise AssertionError('expired deadline allowed a request')
        def sleep(seconds):waits.append(seconds);self.advance(seconds)
        self.factory.side_effect=factory;self.reader.wall_get.side_effect=read
        result=self.run_job(sleeper=sleep)
        self.assertEqual(result['status'],'deferred_deadline')
        self.assertEqual(network,[]);self.assertEqual(waits,[5])
        self.assertEqual(self.reader.wall_get.call_count,2)

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
