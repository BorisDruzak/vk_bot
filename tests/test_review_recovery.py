"""Real retry/state and cached catalog paths, with only external I/O replaced."""
import json
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import vk_api_read as api
import vk_batch as engine
import vk_scheduler as scheduler
import vk_scheduler_state as ledger
from vk_public_data import PublicPages
from tests import test_action_retry_state as fixtures


class RetrySubtypeTests(unittest.TestCase):
    setUp=fixtures.RetryStateTests.setUp
    pick=fixtures.RetryStateTests.pick
    reserve=fixtures.RetryStateTests.reserve
    run_fake=fixtures.RetryStateTests.run_fake

    def test_os_subtype_retries_once_then_counts_one_logical_failure(self):
        def execute(account,kind,row,**options):
            self.calls.append(row['post_id'])
            raise BlockingIOError('synthetic before intent')
        try:result=self.run_fake(execute)
        except RuntimeError as exc:self.fail(f'Retry classification escaped: {exc}')
        self.assertEqual(self.calls,[1,1,2,2,3,3])
        self.assertEqual(result['phase'],'attention')
        self.assertEqual(self.state.get('paused'),'action_failures_repeated')
        self.assertEqual(self.state.get('action_failure_streak'),3)
        self.assertEqual(sum(self.state.usage(self.now).values()),0)

    def test_os_subtype_with_cleanup_or_http_guard_is_never_retryable(self):
        import vk_action_retry as recovery
        job={'status':'dispatching','attempt_id':None,'kind':'like'}
        cleanup=BlockingIOError('cleanup');cleanup.stage='cleanup'
        http=HTTPError('https://fixture.invalid',429,'',{},None)
        self.addCleanup(http.close)
        self.assertFalse(recovery.retryable(cleanup,job))
        self.assertFalse(recovery.retryable(http,job))


class CatalogRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.path=self.root/'actor.sqlite3'
        with closing(engine.db_open(self.path)) as db:api.init_api(db)
        self.state=ledger.State(self.root/'scheduler.sqlite3',{1:self.path});self.addCleanup(self.state.close)
        self.worker=scheduler.Worker(self.state,self.path,{'groups':[{'id':101}]})

    def test_cache_does_not_reset_streak_and_third_failure_still_stops(self):
        (self.root/'.vk-public-data.json').write_text(json.dumps({'version':1,'enabled':True}))
        PublicPages(self.root/'vk_public_data.sqlite3').get(101,100,0,lambda:dict(count=0,items=[]))
        self.worker.defer_technical(api.VkApiTransientRead('synthetic'),'read')
        with patch.object(api,'configured_token',return_value='synthetic'), \
             patch.object(scheduler.app,'exclusive_run',return_value=nullcontext()), \
             patch.object(api,'_default_transport',side_effect=AssertionError('network forbidden')):
            self.worker.scan_step()
        self.assertEqual(self.state.get('technical_failures')['read'],1)
        self.worker.defer_technical(api.VkApiTransientRead('synthetic'),'read')
        self.assertEqual(self.worker.defer_technical(api.VkApiTransientRead('synthetic'),'read'),'attention')
        self.assertEqual(self.state.get('paused'),'technical_error_repeated')

    def test_real_successful_empty_wall_resets_transport_streak(self):
        self.state.set('technical_failures',{'read':2})
        calls=[]
        def transport(params):calls.append(params['owner_id']);return 200,{'response':dict(count=0,items=[])}
        with patch.object(api,'configured_token',return_value='synthetic'), \
             patch.object(scheduler.app,'exclusive_run',return_value=nullcontext()), \
             patch.object(api,'_default_transport',side_effect=transport):
            self.worker.scan_step()
        self.assertEqual(calls,[-101])
        self.assertEqual(self.state.get('technical_failures')['read'],0)
