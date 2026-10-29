"""Bounded counter-read recovery using temporary ledgers and fake HTTP only."""
import json
import subprocess
import time
import unittest
from contextlib import closing, nullcontext
from datetime import timedelta
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_diagnostics as diagnostics
import vk_public_data as public
from tests import test_batch_engagement as fixtures


class PublicReadRetryTests(unittest.TestCase):
    setUp = fixtures.RefreshTests.setUp
    payload = fixtures.RefreshTests.payload

    def run_refresh(self, transport, *, pause=5, budget=2000, **options):
        def invoke(params):
            self.calls.append(params.copy())
            return transport(params)

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now += timedelta(seconds=seconds)

        with patch.object(api, 'configured_token', return_value='fake-test-token'), \
             patch.object(engagement.app, 'exclusive_run', return_value=nullcontext()):
            return engagement.refresh(self.state, self.paths[1], self.groups,
                {'daily_requests': budget, 'pages_per_group': 10, 'pause_seconds': pause},
                clock=lambda: self.now, transport=invoke, sleeper=options.pop('sleeper', sleep),
                **options)

    def failure(self, cls=api.VkApiReadDeadline, *, phase='tcp_connect',
                error='helper_deadline', **detail):
        exc = cls('private exception text must never be emitted')
        exc.read_diagnostics = {'read_phase': phase, 'read_error': error, **detail}
        return exc

    def ledger_requests(self):
        with self.state.account_db(1) as db:
            return db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0]

    def assert_no_actions(self):
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0], 0)
        for account in self.paths:
            with self.state.account_db(account) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0], 0)

    def test_two_network_deadlines_then_success_account_each_physical_request(self):
        def transport(params):
            if len(self.calls) < 3:
                raise self.failure()
            return self.payload()

        result = self.run_refresh(transport)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['pool'], {(101, 11), (101, 12)})
        self.assertEqual(result['report']['requests'], 3)
        self.assertEqual(self.ledger_requests(), 3)
        self.assertEqual(self.sleeps, [5, 5, 5])
        self.assertEqual([p['offset'] for p in self.calls], [0, 0, 0])
        self.assertNotIn('read_attempt', result['report'])
        self.assertNotIn('read_attempt_limit', result['report'])
        self.assertNotIn('read_retry_wait_until', result['report'])
        self.assert_no_actions()

    def test_third_failure_propagates_without_fourth_request(self):
        failures = [self.failure() for _ in range(3)]
        snapshots = []

        def transport(params):
            raise failures[len(self.calls)-1]

        with self.assertRaises(api.VkApiReadDeadline) as caught:
            self.run_refresh(transport, progress=snapshots.append)
        self.assertIs(caught.exception, failures[2])
        self.assertEqual(self.ledger_requests(), 3)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(snapshots[-1]['requests'], 3)
        self.assert_no_actions()

    def test_transient_dns_reset_timeout_and_network_deadline_recover(self):
        cases = [('dns', 'dns_error'), ('tcp_connect', 'connection_refused'),
                 ('tcp_connect', 'connection_reset'), ('tcp_connect', 'network_unreachable'),
                 ('tls_handshake', 'timeout'), ('request_send', 'transport_error'),
                 ('response_headers', 'timeout'), ('response_body', 'timeout'),
                 ('http_open', 'helper_deadline')]
        for phase, error in cases:
            with self.subTest(phase=phase, error=error):
                start = len(self.calls)
                exc = self.failure(api.VkApiTransientRead, phase=phase, error=error)

                def transport(params):
                    if len(self.calls) == start+1:
                        raise exc
                    return self.payload()

                self.now += timedelta(seconds=301)
                result = self.run_refresh(transport)
                self.assertEqual(result['state'], 'ready')
                self.assertEqual(result['report']['requests'], 2)

    def test_longer_configured_pause_is_not_shortened_by_retry_backoff(self):
        start = self.now
        request_times = []

        def transport(params):
            request_times.append(self.now)
            if len(self.calls) < 3:
                raise self.failure()
            return self.payload()

        self.run_refresh(transport, pause=20)
        self.assertEqual([(at-start).total_seconds() for at in request_times], [0, 20, 60])
        self.assertTrue(all(seconds <= 5 for seconds in self.sleeps))

    def test_non_network_phase_or_unsafe_error_never_retries(self):
        cases = [('helper_start', 'helper_deadline'), ('helper_imports', 'helper_deadline'),
                 ('input_read', 'helper_deadline'), ('request_prepare', 'helper_deadline'),
                 ('json_parse', 'helper_deadline'), ('json_parse', 'invalid_json'),
                 ('tls_handshake', 'tls_certificate_error'), ('tls_handshake', 'tls_error'),
                 ('tcp_connect', 'access_denied'), ('tcp_connect', 'unknown'),
                 ('tcp_connect', 'helper_start_failed')]
        for phase, error in cases:
            with self.subTest(phase=phase, error=error):
                self.now += timedelta(seconds=301)
                before = len(self.calls)
                exc = self.failure(phase=phase, error=error)
                with self.assertRaises(api.VkApiReadDeadline) as caught:
                    self.run_refresh(lambda params: self.raise_error(exc))
                self.assertIs(caught.exception, exc)
                self.assertEqual(len(self.calls)-before, 1)

    @staticmethod
    def raise_error(exc):
        raise exc

    def test_missing_metadata_and_unknown_exception_class_never_retries(self):
        class UnknownTransient(api.VkApiTransientRead):
            pass
        for exc in (api.VkApiReadDeadline('timeout'), api.VkApiTransientRead('timeout'),
                    self.failure(UnknownTransient), self.failure(api.VkApiFailure)):
            with self.subTest(error_type=type(exc).__name__):
                self.now += timedelta(seconds=301)
                before = len(self.calls)
                with self.assertRaises(type(exc)):
                    self.run_refresh(lambda params: self.raise_error(exc))
                self.assertEqual(len(self.calls)-before, 1)

    def test_any_vk_code_or_http429_metadata_vetoes_outer_network_deadline(self):
        for detail in ({'vk_error_code': 14}, {'vk_error_code': 17}, {'vk_error_code': 7},
                       {'vk_error_code': 100}, {'http_status': 429}):
            with self.subTest(detail=detail):
                self.now += timedelta(seconds=301)
                before = len(self.calls)
                exc = self.failure(**detail)
                with self.assertRaises(api.VkApiReadDeadline):
                    self.run_refresh(lambda params: self.raise_error(exc))
                self.assertEqual(len(self.calls)-before, 1)

    def test_guard_unknown_cause_and_exception_cycles_veto_retry(self):
        causes = [engine.StopRun('platform guard'), api.VkApiProtection('protection', code=14),
                  api.VkApiLimited('rate limit'), api.VkApiAuthFailure('auth', code=7),
                  RuntimeError('unknown wrapper')]
        for cause in causes:
            with self.subTest(cause=type(cause).__name__):
                self.now += timedelta(seconds=301)
                before = len(self.calls)
                exc = self.failure()
                exc.__cause__ = cause
                with self.assertRaises(api.VkApiReadDeadline):
                    self.run_refresh(lambda params: self.raise_error(exc))
                self.assertEqual(len(self.calls)-before, 1)
        exc = self.failure()
        exc.__context__ = exc
        with self.assertRaises(api.VkApiReadDeadline):
            self.run_refresh(lambda params: self.raise_error(exc))
        self.assertEqual(len(self.calls)-before, 2)

    def test_suppressed_guard_context_is_still_vetoed(self):
        exc = self.failure()
        exc.__context__ = engine.StopRun('hidden guard')
        exc.__suppress_context__ = True
        with self.assertRaises(api.VkApiReadDeadline):
            self.run_refresh(lambda params: self.raise_error(exc))
        self.assertEqual(len(self.calls), 1)

    def test_real_helper_timeout_context_can_recover_when_network_phase_is_proven(self):
        exc = self.failure()
        exc.__context__ = subprocess.TimeoutExpired(['private-helper'], 20)
        exc.__suppress_context__ = True

        def transport(params):
            if len(self.calls) == 1:
                raise exc
            return self.payload()

        result = self.run_refresh(transport)
        self.assertEqual(result['report']['requests'], 2)

    def test_actual_bounded_helper_deadline_recovers_with_suppressed_timeout_context(self):
        progress = ('VKREAD '+json.dumps({'version': 1, 'phase': 'tcp_connect',
                    'state': 'started', 'tick_ns': time.monotonic_ns()})+'\n').encode()
        expired = subprocess.TimeoutExpired(['private-helper'], 20, stderr=progress)
        success = subprocess.CompletedProcess([], 0,
            json.dumps({'status': 200, 'payload': self.payload()[1]}).encode(), b'')
        with patch.object(api.subprocess, 'run', side_effect=[expired, success]):
            result = self.run_refresh(lambda params: api.bounded_transport(params))
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['report']['requests'], 2)
        self.assertEqual(self.ledger_requests(), 2)

    def test_nested_metadata_rate_limit_and_too_deep_chain_are_not_retried(self):
        for deep in (False, True):
            with self.subTest(deep=deep):
                self.now += timedelta(seconds=301)
                before = len(self.calls)
                exc = self.failure()
                link = exc
                for _ in range(12 if deep else 1):
                    link.__cause__ = self.failure()
                    link = link.__cause__
                if not deep:
                    link.read_diagnostics['http_status'] = 429
                with self.assertRaises(api.VkApiReadDeadline):
                    self.run_refresh(lambda params: self.raise_error(exc))
                self.assertEqual(len(self.calls)-before, 1)

    def test_stop_during_retry_wait_discards_pool_without_second_request(self):
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now += timedelta(seconds=seconds)
            self.state.set('stop_requested', True)

        result = self.run_refresh(lambda params: self.raise_error(self.failure()), sleeper=sleep)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['pool'], set())
        self.assertEqual(result['report']['requests'], 1)
        self.assertEqual(self.ledger_requests(), 1)
        self.assertEqual(len(self.calls), 1)

    def test_platform_guard_during_retry_wait_propagates_without_second_request(self):
        def sleep(seconds):
            self.now += timedelta(seconds=seconds)
            with closing(engine.db_open(self.paths[2])) as db:
                engine.lock_on_platform_warning(db, 0, 'offline guard')

        with self.assertRaises(engine.StopRun):
            self.run_refresh(lambda params: self.raise_error(self.failure()), sleeper=sleep)
        self.assertEqual(len(self.calls), 1)
        self.assert_no_actions()

    def test_window_close_during_retry_wait_never_uses_partial_pool(self):
        self.now = self.now.replace(hour=13, minute=59, second=58)
        result = self.run_refresh(lambda params: self.raise_error(self.failure()))
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['pool'], set())
        self.assertEqual(len(self.calls), 1)

    def test_network_failure_with_insufficient_retry_headroom_keeps_original_error(self):
        exc = self.failure()
        with self.assertRaises(api.VkApiReadDeadline) as caught:
            self.run_refresh(lambda params: self.raise_error(exc), deadline=self.now+timedelta(seconds=25))
        self.assertIs(caught.exception, exc)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.ledger_requests(), 1)

    def test_network_failure_when_daily_budget_ends_keeps_original_error(self):
        exc = self.failure()
        with self.assertRaises(api.VkApiReadDeadline) as caught:
            self.run_refresh(lambda params: self.raise_error(exc), budget=1)
        self.assertIs(caught.exception, exc)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.ledger_requests(), 1)

    def test_slow_retry_diagnostic_past_headroom_preserves_original_failure(self):
        exc = self.failure()

        def diagnostic(event, **data):
            if event == 'public_read_retry':
                self.now += timedelta(seconds=40)

        with self.assertRaises(api.VkApiReadDeadline) as caught:
            self.run_refresh(lambda params: self.raise_error(exc),
                deadline=self.now+timedelta(seconds=40), diagnostic=diagnostic)
        self.assertIs(caught.exception, exc)
        self.assertEqual(self.ledger_requests(), 1)

    def test_daily_budget_consumed_during_wait_preserves_original_failure(self):
        exc = self.failure()

        def sleep(seconds):
            self.now += timedelta(seconds=seconds)
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(101,?)',
                           (engine.moment(),))
                db.commit()

        with self.assertRaises(api.VkApiReadDeadline) as caught:
            self.run_refresh(lambda params: self.raise_error(exc), budget=2, sleeper=sleep)
        self.assertIs(caught.exception, exc)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger_requests(), 2)

    def test_retry_sleeper_overshoot_past_cutoff_preserves_original_failure(self):
        exc = self.failure()

        def sleep(seconds):
            self.now += timedelta(seconds=60)

        with self.assertRaises(api.VkApiReadDeadline) as caught:
            self.run_refresh(lambda params: self.raise_error(exc),
                deadline=self.now+timedelta(seconds=40), sleeper=sleep)
        self.assertIs(caught.exception, exc)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger_requests(), 1)

    def test_shared_cache_older_than_refresh_is_not_reused_after_network_failure(self):
        (self.root/'.vk-public-data.json').write_text('{"version":1,"enabled":true}')
        store = public.PublicPages(self.root/'vk_public_data.sqlite3')
        store.get(101, 100, 0, lambda: self.payload()[1]['response'],
                  clock=lambda: (self.now-timedelta(seconds=1)).timestamp())

        def transport(params):
            if len(self.calls) == 1:
                raise self.failure()
            return self.payload()

        result = self.run_refresh(transport)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['report']['requests'], 2)
        self.assertEqual(self.ledger_requests(), 2)

    def test_fresh_shared_cache_can_complete_retry_even_when_budget_is_consumed(self):
        (self.root/'.vk-public-data.json').write_text('{"version":1,"enabled":true}')
        store = public.PublicPages(self.root/'vk_public_data.sqlite3')

        def sleep(seconds):
            self.now += timedelta(seconds=seconds)
            store.get(101, 100, 0, lambda: self.payload()[1]['response'],
                      clock=lambda: self.now.timestamp())
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(101,?)',
                           (engine.moment(),))
                db.commit()

        result = self.run_refresh(lambda params: self.raise_error(self.failure()),
                                  budget=2, sleeper=sleep)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['pool'], {(101, 11), (101, 12)})
        self.assertEqual(result['report']['requests'], 1)
        self.assertEqual(self.ledger_requests(), 2)
        self.assertEqual(len(self.calls), 1)

    def test_deadline_already_crossed_keeps_old_benign_deadline_result(self):
        def transport(params):
            self.now += timedelta(seconds=26)
            raise self.failure()

        result = self.run_refresh(transport, deadline=self.now+timedelta(seconds=25))
        self.assertEqual(result['state'], 'deadline')
        self.assertEqual(result['pool'], set())
        self.assertEqual(result['report']['requests'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_retry_progress_and_diagnostic_are_safe_and_wait_field_is_cleared(self):
        snapshots = []
        records = []

        def transport(params):
            if len(self.calls) < 3:
                raise self.failure(url='https://private', body='private', token='private')
            return self.payload()

        result = self.run_refresh(transport, progress=snapshots.append,
            diagnostic=lambda event, **data: records.append(diagnostics.make_record(event, **data)))
        waiting = [r for r in snapshots if r.get('read_retry_wait_until')]
        self.assertEqual([r['read_attempt'] for r in waiting], [2, 3])
        self.assertTrue(all(r['read_attempt_limit'] == 3 for r in waiting))
        self.assertNotIn('read_retry_wait_until', result['report'])
        retries = [r for r in records if r['event'] == 'public_read_retry']
        self.assertEqual([r['read_attempt'] for r in retries], [2, 3])
        self.assertEqual([r['retry_delay_seconds'] for r in retries], [5, 10])
        self.assertNotIn('private', repr(records))
        self.assertNotIn('access_token', repr(records))

    def test_next_page_resets_attempt_number_and_retries_exact_offset(self):
        snapshots = []

        def transport(params):
            if params['offset'] == 0:
                if len(self.calls) == 1:
                    raise self.failure()
                items = [dict(owner_id=-101, id=1000+i, date=self.stamp,
                              likes={'count': 0}, reposts={'count': 0}, comments={'count': 0})
                         for i in range(100)]
                return 200, {'response': {'count': 102, 'items': items}}
            if len(self.calls) == 3:
                raise self.failure()
            status, payload = self.payload()
            payload['response']['count'] = 102
            return status, payload

        result = self.run_refresh(transport, progress=snapshots.append)
        self.assertEqual([p['offset'] for p in self.calls], [0, 0, 100, 100])
        self.assertEqual(result['report']['requests'], 4)
        request_progress = [r['read_attempt'] for r in snapshots
                            if r.get('read_attempt') and not r.get('read_retry_wait_until')]
        self.assertEqual(request_progress[:4], [1, 2, 1, 2])
        self.assertTrue({(101, 11), (101, 12)} <= result['pool'])
        self.assertEqual(len(result['pool']), 102)

    def test_failed_retry_never_relabels_stale_counts_or_projects_actions(self):
        before = []
        with self.state.account_db(1) as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM api_post_engagement')]
        with self.assertRaises(api.VkApiReadDeadline):
            self.run_refresh(lambda params: self.raise_error(self.failure()))
        with self.state.account_db(1) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM api_post_engagement')], before)
        self.assertEqual(self.ledger_requests(), 3)
        self.assert_no_actions()


if __name__ == '__main__':
    unittest.main()
