import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

import vk_accounts as accounts


class StatisticsJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        p=patch.object(accounts,'ROOT',self.root);p.start();self.addCleanup(p.stop)
        p=patch.object(accounts,'REGISTRY_PATH',self.root/'registry.json');p.start();self.addCleanup(p.stop)

    def test_stale_unowned_job_is_reported_without_rewriting_status_and_can_restart(self):
        import vk_statistics_job as jobs
        for state in ('queued','running','waiting_lock'):
            with self.subTest(state=state):
                accounts.atomic_json(jobs.path(),{'job_id':'abandoned','state':state,'group_ids':[10],
                    'at':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()})
                saved=jobs.path().read_bytes()
                report=jobs.status()
                self.assertEqual((report['state'],report.get('reason')),
                                 ('error','statistics_process_not_reporting'))
                self.assertEqual(jobs.path().read_bytes(),saved)
                with patch.object(jobs,'configured_ids',return_value=[10]),patch.object(jobs,'launch') as launch:
                    replacement=jobs.start(10)
                self.assertNotEqual(replacement['job_id'],'abandoned')
                self.assertEqual(replacement['state'],'queued')
                launch.assert_called_once_with(replacement['job_id'])

    def test_stale_job_with_held_os_guard_is_not_restarted(self):
        import vk_statistics_job as jobs
        from vk_scheduler import singleton
        old={'job_id':'still_owned','state':'running','group_ids':[10],
             'at':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
        accounts.atomic_json(jobs.path(),old)
        saved=jobs.path().read_bytes()
        with singleton(self.root/'.vk-statistics-refresh.guard'), \
             patch.object(jobs,'configured_ids',return_value=[10]),patch.object(jobs,'launch') as launch:
            self.assertEqual(jobs.status(),old)
            self.assertEqual(jobs.start(10),old)
            launch.assert_not_called()
        self.assertEqual(jobs.path().read_bytes(),saved)

    def test_recent_job_without_guard_stays_active(self):
        import vk_statistics_job as jobs
        recent={'job_id':'starting','state':'queued','group_ids':[10],
                'at':(datetime.now(timezone.utc)-timedelta(seconds=1899)).isoformat()}
        accounts.atomic_json(jobs.path(),recent)
        with patch.object(jobs,'configured_ids',return_value=[10]),patch.object(jobs,'launch') as launch:
            self.assertEqual(jobs.status(),recent)
            self.assertEqual(jobs.start(10),recent)
            launch.assert_not_called()

    def test_job_rejects_unconfigured_sources_and_duplicate_does_not_spawn(self):
        import vk_statistics_job as jobs
        with patch.object(jobs,'configured_ids',return_value=[10,20]),patch.object(jobs,'launch') as launch:
            with self.assertRaises(ValueError):jobs.start(999)
            first=jobs.start(10);second=jobs.start(10)
            self.assertEqual(first['job_id'],second['job_id'])
            launch.assert_called_once()

    def test_busy_read_retries_only_lock_no_duplicate_api_on_complete(self):
        import vk_statistics_job as jobs
        accounts.atomic_json(jobs.path(),{'job_id':'test','state':'queued','group_ids':[10,20]})
        results=[{'state':'deferred'},{'state':'complete','observed_posts':100},
                 {'state':'complete','observed_posts':100}]
        with patch.object(jobs,'refresh_one',side_effect=results) as refresh, \
             patch.object(jobs.time,'sleep'):
            jobs.run('test')
        self.assertEqual([c.args[1] for c in refresh.call_args_list],[10,10,20])
        self.assertEqual(jobs.status()['groups_done'],2)
        self.assertEqual(jobs.status()['state'],'complete')

    def test_budget_stops_remaining_reads_without_clear(self):
        import vk_statistics_job as jobs
        accounts.atomic_json(jobs.path(),{'job_id':'test','state':'queued','group_ids':[10,20]})
        with patch.object(jobs,'refresh_one',return_value={'state':'budget'}) as read:
            jobs.run('test')
        read.assert_called_once()
        self.assertEqual(jobs.status()['state'],'budget')

    def test_partial_page_is_saved_and_remaining_groups_continue(self):
        import vk_statistics_job as jobs
        accounts.atomic_json(jobs.path(),{'job_id':'test','state':'queued','group_ids':[10,20]})
        results=[{'state':'partial','observed_posts':98,'returned_posts':100,'rejected_posts':2},
                 {'state':'complete','observed_posts':100,'rejected_posts':0}]
        with patch.object(jobs,'refresh_one',side_effect=results) as refresh:
            jobs.run('test')
        self.assertEqual(refresh.call_count,2)
        state=jobs.status()
        self.assertEqual(state['state'],'partial')
        self.assertEqual(state['groups_done'],2)
        self.assertEqual(state['failures'],1)
        self.assertEqual(state['rejected_posts'],2)
        self.assertEqual(state['requests'],2)
        self.assertEqual(state['reason'],'incomplete_statistics_coverage')


if __name__=='__main__':unittest.main()
