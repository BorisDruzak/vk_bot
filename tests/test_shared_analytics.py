import json
import socket
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import vk_analytics as calc
import vk_analytics_collect as collect
import vk_analytics_store as store
import vk_api_read as api
import vk_batch as engine


def post(pid=1):
    return dict(owner_id=-1, id=pid,
                date=int(datetime.fromisoformat('2026-10-08T12:00:00+05:00').timestamp()),
                likes={'count': 3}, comments={'count': 2}, reposts={'count': 1},
                views={'count': 100})


class SharedAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = store.open_db(self.root/'analytics.sqlite3')
        self.groups = [dict(id=1, name='one', included=True, business_card=False)]
        self.run = self.new_run()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def new_run(self):
        return store.new_run(self.db, '2026-10-08', '2026-10-15', self.groups)

    def test_cached_new_run_keeps_observation_time_and_frozen_history(self):
        old = '2026-10-22T07:00:00+00:00'
        cached = '2026-10-23T03:00:00+00:00'
        collect.collect_run(self.db, self.run, lambda *args:
                            dict(count=1, items=[post()], _observed_at=old))
        frozen = store.freeze(self.db, calc.report(self.db, self.run))
        before = tuple(self.db.execute('SELECT * FROM analytics_reports WHERE id=?', (frozen,)).fetchone())
        run2 = self.new_run()
        collect.collect_run(self.db, run2, lambda *args:
                            dict(count=1, items=[{**post(), 'likes': {'count': 9}}],
                                 _observed_at=cached, _cache_hit=True))
        rows = self.db.execute('SELECT run_id,measured_at,likes FROM analytics_snapshots ORDER BY measured_at').fetchall()
        self.assertEqual([tuple(row) for row in rows], [(self.run, old, 3), (run2, cached, 9)])
        self.assertEqual(tuple(self.db.execute('SELECT * FROM analytics_reports WHERE id=?', (frozen,)).fetchone()), before)

    def test_invalid_cache_timestamp_cannot_be_relabelled_as_current(self):
        for observed in ('not-a-date', '2026-10-23T03:00:00', None, 123):
            with self.subTest(observed=observed):
                run = self.new_run()
                collect.collect_run(self.db, run, lambda *args:
                                    dict(count=1, items=[post()], _observed_at=observed))
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots WHERE run_id=?', (run,)).fetchone()[0], 0)
                self.assertEqual(self.db.execute('SELECT status FROM analytics_runs WHERE id=?', (run,)).fetchone()[0], 'partial')

    def test_legacy_page_keeps_current_observation_fallback(self):
        with patch.object(store, 'now', return_value='2026-10-23T04:00:00+00:00'):
            collect.collect_run(self.db, self.run, lambda *args: dict(count=1, items=[post()]))
        self.assertEqual(self.db.execute('SELECT measured_at FROM analytics_snapshots').fetchone()[0], '2026-10-23T04:00:00+00:00')

    def test_resume_does_not_rewrite_existing_snapshot_time_or_counters(self):
        collect.collect_run(self.db, self.run, lambda *args:
                            dict(count=2, items=[post(2)], _observed_at='2026-10-22T07:00:00+00:00'),
                            page_size=1, max_pages=1)
        def resumed(gid, offset, count):
            return dict(count=2, items=[{**post(2-offset), 'likes': {'count': 9}}],
                        _observed_at='2026-10-23T03:00:00+00:00')
        collect.collect_run(self.db, self.run, resumed, page_size=1)
        rows = self.db.execute('SELECT post_id,measured_at,likes FROM analytics_snapshots ORDER BY post_id').fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [(1, '2026-10-23T03:00:00+00:00', 9), (2, '2026-10-22T07:00:00+00:00', 3)])

    def live(self):
        ledger = engine.db_open(self.root/'vk_batch.sqlite3'); ledger.close()
        (self.root/'.vk-analytics-runtime.json').write_text(json.dumps(dict(machine=socket.gethostname(), root=str(self.root))), encoding='utf8')
        stack = ExitStack()
        stack.enter_context(patch.object(collect, '__file__', str(self.root/'vk_analytics_collect.py')))
        stack.enter_context(patch.object(api, 'configured_token', return_value='dummy-test-token'))
        self.addCleanup(stack.close)
        return collect.LiveReader(self.root, max_requests=1)

    def test_continuation_rejects_page_older_than_its_head_and_resets_at_head(self):
        reader = self.live()
        floors = []
        class Client:
            used = 0
            def __init__(self, *args, **kwargs):
                floors.append(kwargs.get('fresh_after'))
            def wall_get(self, gid, **kwargs):
                return dict(count=2, items=[post()], _observed_at='2026-10-23T03:00:00+00:00')
        with patch.object(api, 'VKReadClient', Client):
            reader.wall_get(1, 0, 1)
            reader.wall_get(1, 1, 1)
            reader.wall_get(2, 0, 1)
            reader.wall_get(1, 0, 1)
        self.assertEqual(floors, [None, '2026-10-23T03:00:00+00:00', None, None])

    def test_byid_probe_explicitly_excludes_shared_wall_cache(self):
        reader = self.live()
        policies = []
        class Client:
            used = 0
            def __init__(self, *args, **kwargs): policies.append(kwargs.get('shared_path'))
            def wall_get(self, *args, **kwargs): return dict(items=[post()])
        with patch.object(api, 'VKReadClient', Client):
            self.assertEqual(reader.wall_get_by_id([(1, 1)]), [post()])
        self.assertEqual(policies, [False])

    def test_deadline_guard_runs_before_serving_cached_page(self):
        reader = self.live()
        def expired(): raise collect.Deferred('deadline')
        reader.before_request = expired
        class Client:
            used = 0
            def __init__(self, *args, **kwargs): pass
            def wall_get(self, *args, **kwargs): return dict(count=1, items=[post()])
        with patch.object(api, 'VKReadClient', Client):
            with self.assertRaises(collect.Deferred): reader.wall_get(1, 0, 100)
        self.assertEqual(reader.used, 0)

    def test_deadline_after_pacing_preserves_ledger_and_counts_no_unsent_http(self):
        reader = self.live()
        self.activate_shared()
        instant = [datetime.fromisoformat('2026-10-23T03:39:58+00:00').timestamp()]
        deadline = datetime.fromisoformat('2026-10-23T03:40:00+00:00').timestamp()
        previous = [(1, 77, '2026-10-22T08:00:00+00:00')]
        with closing(sqlite3.connect(reader.path)) as ledger:
            ledger.row_factory = sqlite3.Row
            api.init_api(ledger)
            ledger.executemany('INSERT INTO api_read_attempts VALUES(?,?,?)', previous)
            ledger.execute('INSERT INTO read_navigation_pacing VALUES(0,?)', (instant[0],))
            ledger.commit()
        def check_deadline():
            if instant[0] >= deadline: raise collect.Deferred('deadline')
        reader.before_request = check_deadline
        cls = api.VKReadClient
        def client(*args, **kwargs):
            return cls(*args, **kwargs, clock=lambda: instant[0],
                       sleeper=lambda seconds: instant.__setitem__(0, instant[0]+seconds))
        with patch.object(api, 'VKReadClient', side_effect=client), patch.object(api, '_default_transport') as network:
            with self.assertRaises(collect.Deferred): reader.wall_get(1, 0, 100)
        network.assert_not_called()
        self.assertEqual(reader.used, 0)
        with closing(sqlite3.connect(reader.path)) as ledger:
            self.assertEqual(ledger.execute('SELECT * FROM api_read_attempts').fetchall(), previous)
            self.assertEqual(ledger.execute('SELECT last_navigation_at FROM read_navigation_pacing').fetchone()[0], deadline-2)

    def test_budget_recheck_after_pacing_does_not_reserve_unsent_http(self):
        reader = self.live()
        instant = [time.time()]
        with closing(sqlite3.connect(reader.path)) as ledger:
            ledger.row_factory = sqlite3.Row
            api.init_api(ledger)
            ledger.execute('INSERT INTO read_navigation_pacing VALUES(0,?)', (instant[0],))
            ledger.commit()
        cls = api.VKReadClient
        def pace(seconds):
            instant[0] += seconds
            reader.used = 1
        def client(*args, **kwargs):
            return cls(*args, **kwargs, clock=lambda: instant[0], sleeper=pace)
        with patch.object(api, 'VKReadClient', side_effect=client), patch.object(api, '_default_transport') as network:
            with self.assertRaises(api.VkApiFailure): reader.wall_get(1, 0, 100)
        network.assert_not_called()
        self.assertEqual(reader.used, 1)
        with closing(sqlite3.connect(reader.path)) as ledger:
            self.assertEqual(ledger.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0], 0)

    def test_cache_read_allowed_after_network_budget_is_used(self):
        reader = self.live()
        reader.used = 1
        class Client:
            used = 0
            def __init__(self, *args, **kwargs): pass
            def has_cached_wall(self, *args, **kwargs): return True
            def wall_get(self, *args, **kwargs):
                return dict(count=1, items=[post()], _cache_hit=True,
                            _observed_at='2026-10-23T03:00:00+00:00')
        with patch.object(api, 'VKReadClient', Client):
            result = reader.wall_get(1, 0, 100)
        self.assertTrue(result['_cache_hit'])
        self.assertEqual(reader.used, 1)

    def activate_shared(self):
        (self.root/'.vk-public-data.json').write_text(json.dumps(dict(version=1, enabled=True)), encoding='utf8')

    def test_real_shared_client_new_runs_reuse_one_http_and_original_time(self):
        reader = self.live()
        self.activate_shared()
        with patch.object(api, '_default_transport', return_value=(200, dict(response=dict(count=1, items=[post()])))) as transport:
            collect.collect_run(self.db, self.run, reader.wall_get)
            run2 = self.new_run()
            collect.collect_run(self.db, run2, reader.wall_get)
        rows = self.db.execute('SELECT measured_at FROM analytics_snapshots ORDER BY run_id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], rows[1][0])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(reader.used, 1)
        with closing(sqlite3.connect(self.root/'vk_batch.sqlite3')) as ledger:
            self.assertEqual(ledger.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0], 1)
            self.assertEqual(ledger.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0], 0)

    def test_real_continuation_fetches_again_when_cached_page_predates_head(self):
        reader = self.live()
        reader.max_requests = 3
        self.activate_shared()
        instant = [time.time()]
        cls = api.VKReadClient
        def client(*args, **kwargs):
            return cls(*args, **kwargs, clock=lambda: instant[0],
                       sleeper=lambda seconds: instant.__setitem__(0, instant[0]+seconds))
        offsets = []
        def network(params):
            offsets.append(params['offset'])
            return 200, dict(response=dict(count=2, items=[post(2-params['offset'])]))
        with patch.object(api, 'VKReadClient', side_effect=client), patch.object(api, '_default_transport', side_effect=network):
            reader.wall_get(1, 1, 1)
            instant[0] += 10
            head = reader.wall_get(1, 0, 1)
            tail = reader.wall_get(1, 1, 1)
        self.assertEqual(offsets, [1, 0, 1])
        self.assertFalse(tail['_cache_hit'])
        self.assertGreaterEqual(datetime.fromisoformat(tail['_observed_at']), datetime.fromisoformat(head['_observed_at']))

    def test_real_byid_probe_does_not_replace_cached_wall_page(self):
        reader = self.live()
        reader.max_requests = 3
        self.activate_shared()
        def probe(data, params):
            return 200, dict(response=dict(items=[{**post(), 'likes': {'count': 9}}]))
        with patch.object(api, '_default_transport', return_value=(200, dict(response=dict(count=1, items=[post()])))) as transport, \
             patch.object(collect.LiveReader, '_byid_transport', side_effect=probe), \
             patch.object(api.time, 'sleep'):
            reader.wall_get(1, 0, 100)
            self.assertEqual(reader.wall_get_by_id([(1, 1)])[0]['likes']['count'], 9)
            result = reader.wall_get(1, 0, 100)
        self.assertEqual(result['items'][0]['likes']['count'], 3)
        self.assertTrue(result['_cache_hit'])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(reader.used, 2)


if __name__ == '__main__':
    unittest.main()
