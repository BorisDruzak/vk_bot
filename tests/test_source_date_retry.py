"""Date hydration retries use virtual time and cannot send actions."""
import unittest
from unittest.mock import Mock,patch
import vk_scheduled_action as actions
import vk_batch as engine


class DateReadRetryTests(unittest.TestCase):
    def run_reader(self,reader,guard=None,before_read=None):
        page=Mock();elapsed=[0.0]
        def wait(ms):elapsed[0]+=ms/1000
        page.wait_for_timeout.side_effect=wait
        with patch.object(engine,'go_to_post') as navigate,patch.object(actions.groups,'exact_post_date_info',side_effect=reader),patch.object(actions.time,'monotonic',side_effect=lambda:elapsed[0]):
            options={'before_read':before_read} if before_read is not None else {}
            try:actions.source_ready(page,101,11,1792644694,guard or (lambda:None),**options)
            finally:self.navigation_count=navigate.call_count;self.elapsed=elapsed[0]

    def test_three_additional_windows_then_skip(self):
        with self.assertRaises(actions.ScheduledActionStopped) as caught:
            self.run_reader(lambda *args:{'publishedAt':None,'source':'date-unknown'})
        self.assertEqual(type(caught.exception).__name__,'SourceDateUnavailable')
        self.assertEqual(caught.exception.read_attempts,4)
        self.assertEqual(self.navigation_count,1)
        self.assertEqual(self.elapsed,36)

    def test_date_can_arrive_in_fourth_window(self):
        def reader(*args):
            return {'publishedAt':1792644694 if calls[0]>=113 else None}
        calls=[0]
        def count(*args):calls[0]+=1;return reader()
        self.run_reader(count)
        self.assertGreaterEqual(self.elapsed,27)
        self.assertEqual(self.navigation_count,1)

    def test_three_evaluation_failures_then_success(self):
        reads=[0]
        def reader(*args):
            reads[0]+=1
            if reads[0]<4:raise actions.PlaywrightError('synthetic date evaluation failure')
            return {'publishedAt':1792644694}
        self.run_reader(reader)
        self.assertEqual(reads[0],4)

    def test_conflicting_date_is_never_retried(self):
        reads=Mock(return_value={'publishedAt':None,'source':'date-conflict'})
        with self.assertRaises(actions.ScheduledActionStopped) as caught:self.run_reader(reads)
        self.assertEqual(caught.exception.stage,'source_date_conflict')
        self.assertEqual(reads.call_count,1)

    def test_guard_warning_stops_before_retry(self):
        guard=Mock(side_effect=[None,engine.PlatformRateLimited('synthetic')])
        with self.assertRaises(engine.PlatformRateLimited):self.run_reader(lambda *args:{'publishedAt':None},guard)
        self.assertEqual(self.navigation_count,1)

    def test_date_wait_respects_callers_cutoff(self):
        checks=[0]
        def cutoff():
            checks[0]+=1
            if checks[0]==3:raise RuntimeError('Batch intent window closed')
        with self.assertRaisesRegex(RuntimeError,'window closed'):
            self.run_reader(lambda *args:{'publishedAt':None},before_read=cutoff)
        self.assertLess(self.elapsed,1)
