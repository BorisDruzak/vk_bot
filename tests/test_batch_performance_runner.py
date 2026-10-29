"""Capacity and explicit stop causes with real temporary ledgers, no VK."""
import unittest
from datetime import timedelta
from unittest.mock import patch

import vk_batch_engagement as engagement
import vk_scheduler as scheduler
from tests import test_selection_performance as selection
from tests import test_batch_spacing_wait as spacing


class PickPerformanceTests(unittest.TestCase):
    setUp=selection.SelectionPerformanceTests.setUp

    def test_pick_reads_one_selection_context_and_refreshes_it_next_time(self):
        worker=scheduler.Worker(self.state,self.source,{'groups':self.groups},clock=lambda:self.now)
        with patch.object(self.state,'selection_context',wraps=self.state.selection_context) as contexts, \
             patch.object(engagement,'local_activity',wraps=engagement.local_activity) as own:
            first=worker.pick(self.now)
            second=worker.pick(self.now)
        self.assertIsNotNone(first);self.assertEqual(first,second)
        self.assertEqual(contexts.call_count,2)
        self.assertEqual(own.call_count,2)

    def test_pick_only_sorts_pool_rows_but_keeps_exact_priority_evidence(self):
        worker=scheduler.Worker(self.state,self.source,{'groups':self.groups},clock=lambda:self.now)
        worker._batch_pool={(101,7),(101,9)}
        worker._batch_refresh={'selected_band':'new','started_at':self.now.isoformat()}
        with patch.object(self.state,'selection_context',wraps=self.state.selection_context) as contexts:
            selected=worker.pick(self.now)
        self.assertEqual(selected[-1]['post_id'],7)
        self.assertEqual(contexts.call_count,1)
        self.assertEqual(contexts.call_args.kwargs['pool'],worker._batch_pool)
        self.assertEqual(selected[-1]['refresh_started_at'],self.now.isoformat())

    def test_stop_phases_distinguish_time_quota_and_operator(self):
        worker=scheduler.Worker(self.state,self.source,{'groups':self.groups},clock=lambda:self.now)
        self.state._batch_boundary=self.now+timedelta(seconds=60)
        self.assertEqual(worker.batch_stop_phase(self.now),'batch_action_deadline')
        self.state._batch_boundary=None
        self.state.set('stop_requested',True)
        self.assertEqual(worker.batch_stop_phase(self.now),'batch_stop_requested')
        self.state.set('stop_requested',False)
        self.assertEqual(worker.batch_stop_phase(self.now,1,'like',0),'batch_guard_rejected')
        with patch.object(self.state,'account_enabled',return_value=False):
            self.assertEqual(worker.batch_stop_phase(self.now,1,'like',0),'batch_account_disabled')
        with patch.object(self.state,'remaining',return_value={(1,'like',0):0}):
            self.assertEqual(worker.batch_stop_phase(self.now,1,'like',0),'batch_daily_quota_reached')
        self.assertEqual(worker.batch_stop_phase(self.now.replace(hour=15)),'batch_work_window_closed')


class SpacingDetailsTests(unittest.TestCase):
    setUp=spacing.BatchSpacingWaitTests.setUp
    setup_batch=spacing.BatchSpacingWaitTests.setup_batch

    def test_waiting_heartbeat_has_absolute_ready_and_cutoff(self):
        worker=self.setup_batch(at='2026-10-24T05:00:00+00:00',
            previous='2026-10-24T04:51:40+00:00')
        self.start=self.now
        worker._active_slot=1
        self.state._batch_boundary=self.now+timedelta(minutes=10)
        self.state.set('last_action_batch',{'day':str(self.now.date()),'slot':0,
                                          'at':(self.now-timedelta(seconds=500)).isoformat()})
        self.assertTrue(self.state.claim_batch(self.now,1))
        result=worker.wait_batch_spacing(1)
        hb=self.state.get('heartbeat')
        self.assertIsNone(result)
        self.assertEqual(hb['spacing_ready_at'],(self.start+timedelta(seconds=40)).isoformat())
        self.assertEqual(hb['batch_deadline_at'],(self.start+timedelta(minutes=9)).isoformat())
