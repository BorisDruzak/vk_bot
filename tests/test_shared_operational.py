"""Shared public observations preserve worker freshness and HTTP accounting."""
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import vk_accounts as accounts
import vk_api_read as api
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_groups as groups
import vk_scheduler_state as ledger
import vk_statistics_job as jobs
import vk_statistics_refresh as statistics


class OperationalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)
        self.stamp = int(self.now.timestamp()) - 10
        self.path = self.root / 'actor.sqlite3'
        with closing(engine.db_open(self.path)) as db:
            groups.init_dates(db)
            api.init_api(db)
        for obj, name, value in (
            (accounts, 'ROOT', self.root),
            (accounts, 'REGISTRY_PATH', self.root / 'accounts.json'),
            (engine, 'moment', lambda: self.now.isoformat(timespec='seconds')),
            (api, 'configured_token', lambda: 'offline-test-token'),
            (engagement.app, 'exclusive_run', lambda: nullcontext()),
            (statistics.stats, '_names', lambda: {101: 'Test public group'}),
        ):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def item(self, *, likes=7, date=None):
        return dict(owner_id=-101, id=11, date=self.stamp if date is None else date,
                    likes={'count': likes}, reposts={'count': 2},
                    comments={'count': 3}, views={'count': 20})

    def observation(self):
        with closing(engine.db_open(self.path)) as db:
            return dict(db.execute('SELECT * FROM api_post_engagement').fetchone())

    def client(self, payload, *, remaining=1):
        """An external API boundary with exact fresh-after cache eligibility."""
        class CachedClient:
            def __init__(inner, db, token, **kwargs):
                inner.fresh_after = kwargs.get('fresh_after')
                inner.used = 0

            def remaining(inner):
                return remaining

            def has_cached_wall(inner, group_id, *, count=100, offset=0):
                return (group_id == 101 and count == 100 and offset == 0 and
                        (inner.fresh_after is None or
                         payload['_observed_at'] >= inner.fresh_after))

            def wall_get(inner, group_id, *, count=100, offset=0):
                if remaining < 1 and not inner.has_cached_wall(group_id, count=count, offset=offset):
                    raise api.VkApiFailure('No eligible cached observation')
                return payload
        return CachedClient

    def refresh_worker(self, client, *, transport=None, daily_requests=5000):
        with closing(engine.db_open(self.path)) as db:
            engagement._observe_page(db, 101, [self.item()], int(self.now.timestamp()))
        second = self.root / 'second.sqlite3'
        with closing(engine.db_open(second)) as db:
            groups.init_dates(db)
            api.init_api(db)
        state = ledger.State(self.root / 'schedule.sqlite3', {1: self.path, 2: second})
        self.addCleanup(state.close)
        selected = [{'id': 101, 'enabled': True, 'like_enabled': True, 'repost_enabled': True}]
        with patch.object(api, 'VKReadClient', client):
            return engagement.refresh(state, self.path, selected,
                {'daily_requests': daily_requests, 'pages_per_group': 1, 'pause_seconds': 5},
                clock=lambda: self.now, transport=transport, sleeper=lambda seconds: None)

    def enable_shared(self):
        (self.root / '.vk-public-data.json').write_text(
            json.dumps({'version': 1, 'enabled': True}), encoding='utf-8')

    def transport(self, params):
        return 200, {'response': {'count': 1, 'items': [self.item()]}}

    def test_projection_keeps_original_observation_time_and_nullable_counts(self):
        observed = '2026-10-23T04:58:30+00:00'
        item = self.item()
        item.pop('comments')
        with closing(engine.db_open(self.path)) as db:
            engagement._observe_page(db, 101, [item], int(self.now.timestamp()), observed_at=observed)
        row = self.observation()
        self.assertEqual(row['observed_at'], observed)
        self.assertIsNone(row['comment_count'])

    def test_old_cached_projection_cannot_replace_newer_counts_even_with_microseconds(self):
        with closing(engine.db_open(self.path)) as db:
            engagement._observe_page(db, 101, [self.item(likes=19)], int(self.now.timestamp()),
                                    observed_at='2026-10-23T04:59:00.000002+00:00')
            engagement._observe_page(db, 101, [self.item(likes=1)], int(self.now.timestamp()),
                                    observed_at='2026-10-23T04:59:00.000001+00:00')
        self.assertEqual((self.observation()['like_count'], self.observation()['observed_at']),
                         (19, '2026-10-23T04:59:00.000002+00:00'))

    def test_old_conflicting_publication_still_invalidates_source(self):
        with closing(engine.db_open(self.path)) as db:
            engagement._observe_page(db, 101, [self.item(likes=19)], int(self.now.timestamp()),
                                    observed_at='2026-10-23T04:59:00+00:00')
            engagement._observe_page(db, 101, [self.item(likes=1, date=self.stamp - 1)],
                                    int(self.now.timestamp()), observed_at='2026-10-23T04:58:00+00:00')
            self.assertEqual(db.execute('SELECT valid FROM api_seen_posts').fetchone()[0], 0)
        self.assertEqual(self.observation()['like_count'], 19)

    def test_invalid_shared_timestamp_rejects_before_projection_writes(self):
        for observed in ('bad', '2026-10-23T04:59:00', 42, ''):
            with self.subTest(observed=observed), closing(engine.db_open(self.path)) as db:
                with self.assertRaises(api.VkApiFailure):
                    engagement._observe_page(db, 101, [self.item()], int(self.now.timestamp()),
                                            observed_at=observed)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM api_post_engagement').fetchone()[0], 0)

    def test_worker_fresh_cache_allows_read_at_exhausted_budget_without_action_writes(self):
        payload = {'count': 1, 'items': [self.item()], '_cache_hit': True,
                   '_observed_at': self.now.isoformat(timespec='seconds'), '_observation_id': 2}
        result = self.refresh_worker(self.client(payload, remaining=0))
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['pool'], {(101, 11)})
        self.assertEqual(result['report']['requests'], 0)
        with closing(engine.db_open(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0], 0)

    def test_worker_ttl_cache_before_current_refresh_never_becomes_action_pool(self):
        payload = {'count': 1, 'items': [self.item()], '_cache_hit': True,
                   '_observed_at': '2026-10-23T04:59:59+00:00', '_observation_id': 1}
        result = self.refresh_worker(self.client(payload, remaining=0))
        self.assertEqual(result['state'], 'budget')
        self.assertEqual(result['pool'], set())

    def test_worker_never_restamps_stale_response_into_current_refresh_pool(self):
        payload = {'count': 1, 'items': [self.item()], '_cache_hit': True,
                   '_observed_at': '2026-10-23T04:59:59+00:00', '_observation_id': 1}
        result = self.refresh_worker(self.client(payload))
        self.assertEqual(result['pool'], set())

    def test_statistics_cached_read_preserves_time_and_reports_zero_http_at_budget_limit(self):
        observed = '2026-10-23T04:58:30+00:00'
        payload = {'count': 1, 'items': [self.item()], '_cache_hit': True,
                   '_observed_at': observed, '_observation_id': 1}
        with patch.object(api, 'VKReadClient', self.client(payload, remaining=0)):
            result = statistics.refresh_one(self.path, 101, clock=lambda: self.now.timestamp())
        self.assertEqual(result['state'], 'complete')
        self.assertEqual((result['requests'], result['cache_hit'], result['observed_at']),
                         (0, True, observed))
        self.assertEqual(self.observation()['observed_at'], observed)

    def test_statistics_job_counts_real_requests_and_cache_hits_with_legacy_fixture_default(self):
        accounts.atomic_json(jobs.path(), {'job_id': 'offline', 'state': 'queued', 'group_ids': [101, 102, 103]})
        results = [{'state': 'complete', 'requests': 0, 'cache_hit': True},
                   {'state': 'partial', 'requests': 1, 'cache_hit': False, 'rejected_posts': 1},
                   {'state': 'complete'}]
        with patch.object(jobs, 'refresh_one', side_effect=results):
            jobs.run('offline')
        result = jobs.status()
        self.assertEqual((result['state'], result['groups_done'], result['requests'], result['cache_hits']),
                         ('partial', 3, 2, 1))

    def test_real_shared_client_statistics_reuses_one_http_and_original_timestamp(self):
        self.enable_shared()
        first = statistics.refresh_one(self.path, 101, transport=self.transport,
                                       clock=lambda: self.now.timestamp())
        self.now += timedelta(seconds=90)
        second = statistics.refresh_one(self.path, 101,
            transport=lambda params: self.fail('duplicate HTTP for reusable public page'),
            clock=lambda: self.now.timestamp())
        self.assertEqual((first['requests'], second['requests'], second['cache_hit']), (1, 0, True))
        self.assertEqual(second['observed_at'], first['observed_at'])
        self.assertEqual(self.observation()['observed_at'], first['observed_at'])
        with closing(engine.db_open(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0], 0)

    def test_real_shared_client_worker_can_reuse_current_batch_observation_without_budget(self):
        self.enable_shared()
        statistics.refresh_one(self.path, 101, transport=self.transport, clock=lambda: self.now.timestamp())
        result = self.refresh_worker(api.VKReadClient, daily_requests=1,
            transport=lambda params: self.fail('fresh cache should not need HTTP budget'))
        self.assertEqual((result['state'], result['pool'], result['report']['requests']),
                         ('ready', {(101, 11)}, 0))

    def test_real_shared_client_worker_rejects_previous_batch_cache_even_with_ordinary_ttl(self):
        self.enable_shared()
        statistics.refresh_one(self.path, 101, transport=self.transport, clock=lambda: self.now.timestamp())
        self.now += timedelta(seconds=1)
        result = self.refresh_worker(api.VKReadClient, daily_requests=1,
            transport=lambda params: self.fail('stale cache at exhausted budget must stop'))
        self.assertEqual((result['state'], result['pool'], result['report']['requests']),
                         ('budget', set(), 0))

    def test_real_shared_client_worker_rejects_earlier_cache_within_same_second(self):
        self.enable_shared()
        self.now = self.now.replace(microsecond=100000)
        statistics.refresh_one(self.path, 101, transport=self.transport, clock=lambda: self.now.timestamp())
        self.now = self.now.replace(microsecond=900000)
        result = self.refresh_worker(api.VKReadClient, daily_requests=1,
            transport=lambda params: self.fail('same-second stale cache must not bypass budget'))
        self.assertEqual((result['state'], result['pool'], result['report']['requests']),
                         ('budget', set(), 0))
        self.assertEqual(result['report']['started_at'], '2026-10-23T05:00:00.900000+00:00')

    def test_legacy_network_observation_is_fresh_for_fractional_refresh_start(self):
        self.now = self.now.replace(microsecond=900000)
        result = self.refresh_worker(api.VKReadClient, transport=self.transport)
        self.assertEqual((result['state'], result['pool'], result['report']['requests']),
                         ('ready', {(101, 11)}, 1))
        self.assertEqual(self.observation()['observed_at'], '2026-10-23T05:00:00.900000+00:00')

    def test_late_window_close_before_http_does_not_charge_request_ledger(self):
        self.enable_shared()
        self.now = self.now.replace(hour=13, minute=59, second=59)
        open_db = engine.db_open

        def open_with_window_boundary(path):
            db = open_db(path)

            def advance_clock(statement):
                if statement.startswith('SELECT last_navigation_at FROM read_navigation_pacing'):
                    self.now = self.now.replace(hour=14, minute=0, second=0)

            db.set_trace_callback(advance_clock)
            return db

        with patch.object(engine, 'db_open', side_effect=open_with_window_boundary):
            result = self.refresh_worker(api.VKReadClient,
                transport=lambda params: self.fail('HTTP after work window ended'))
        self.assertEqual((result['state'], result['pool'], result['report']['requests']),
                         ('stopped', set(), 0))
        with closing(engine.db_open(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM read_navigation_pacing').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
