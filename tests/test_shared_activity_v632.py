"""Real OS leases on temporary files; no browsers, VK or production state."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import vk_scheduler as scheduler
import vk_unified as app


class SharedActivityLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = self.root / '.vk-activity.guard'

    def shared(self):
        import vk_activity
        return vk_activity.shared_activity(self.root)

    def test_two_readers_share_one_guard_file(self):
        with self.shared():
            with self.shared():
                self.assertTrue(self.guard.is_file())
        self.assertTrue(self.guard.is_file())

    def test_existing_worker_exclusive_blocks_reader(self):
        with scheduler.singleton(self.guard):
            with self.assertRaises(app.RunAlreadyActive):
                with self.shared():
                    self.fail('reader entered during worker batch')
        with self.shared():
            pass

    def test_reader_blocks_existing_worker_until_last_reader_exits(self):
        with self.shared():
            with self.shared():
                with self.assertRaises(app.RunAlreadyActive):
                    with scheduler.singleton(self.guard):
                        self.fail('worker entered during shared login')
            with self.assertRaises(app.RunAlreadyActive):
                with scheduler.singleton(self.guard):
                    self.fail('inner reader released outer reader lease')
        with scheduler.singleton(self.guard):
            pass

    def test_reader_exception_releases_guard_without_deleting_file(self):
        with self.assertRaisesRegex(ValueError, 'test failure'):
            with self.shared():
                raise ValueError('test failure')
        self.assertTrue(self.guard.is_file())
        with scheduler.singleton(self.guard):
            pass

    def test_separate_roots_do_not_block_one_another(self):
        import vk_activity
        other = self.root / 'other'
        other.mkdir()
        with scheduler.singleton(self.guard):
            with vk_activity.shared_activity(other):
                self.assertTrue((other / '.vk-activity.guard').is_file())

    def child(self, mode):
        script = '''
import os, sys
from pathlib import Path
from vk_activity import shared_activity
from vk_scheduler import singleton
root = Path(sys.argv[1])
lease = shared_activity(root) if sys.argv[2] == 'shared' else singleton(root / '.vk-activity.guard')
with lease:
    print('locked', flush=True)
    sys.stdin.readline()
    os._exit(0)
'''
        process = subprocess.Popen(
            [sys.executable, '-u', '-c', script, str(self.root), mode],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.close_child, process)
        # Bound the ready-line wait so a broken child cannot hang the suite.
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=1)
        ready = pool.submit(process.stdout.readline)
        try:
            self.assertEqual(ready.result(timeout=10).strip(), 'locked')
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
        finally:
            pool.shutdown(wait=True)
        return process

    @staticmethod
    def close_child(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def test_cross_process_readers_share_and_crash_releases_worker(self):
        # Loading the feature first makes missing-module failure immediate.
        import vk_activity
        process = self.child('shared')
        with self.shared():
            with self.assertRaises(app.RunAlreadyActive):
                with scheduler.singleton(self.guard):
                    self.fail('worker ignored child reader')
        process.stdin.write('\n')
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertTrue(self.guard.is_file())
        with scheduler.singleton(self.guard):
            pass

    def test_cross_process_worker_blocks_reader_and_releases_after_crash(self):
        import vk_activity
        process = self.child('exclusive')
        with self.assertRaises(app.RunAlreadyActive):
            with self.shared():
                self.fail('reader ignored child worker')
        process.stdin.write('\n')
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=10), 0)
        with self.shared():
            pass


if __name__ == '__main__':
    unittest.main()
