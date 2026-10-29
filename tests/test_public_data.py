import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import vk_public_data as public


def page():
    return {'count': 1, 'items': [{'id': 9, 'owner_id': -7,
            'date': 1792700000, 'is_pinned': 0, 'post_type': 'post',
            'likes': {'count': 2}, 'comments': {'count': 3},
            'reposts': {'count': 4}, 'views': {'count': 5}}]}


class PublicPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'public.sqlite3'
        self.store = public.PublicPages(self.path)
        self.now = 1792756800.0
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return page()

    def get(self, **kwargs):
        return self.store.get(7, 100, 0, self.fetch,
                              clock=lambda: self.now, **kwargs)

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT * FROM ' + table).fetchall()

    def test_reuse_preserves_observation_and_does_not_share_mutable_payload(self):
        first = self.get()
        first['items'][0]['likes']['count'] = 99
        self.now += 60
        reused = self.get()
        self.assertEqual(self.calls, 1)
        self.assertEqual(reused['items'][0]['likes']['count'], 2)
        self.assertFalse(first['_cache_hit'])
        self.assertTrue(reused['_cache_hit'])
        self.assertEqual(first['_observed_at'], '2026-10-23T12:00:00+00:00')
        self.assertEqual(reused['_observed_at'], first['_observed_at'])
        self.assertEqual(reused['_observation_id'], first['_observation_id'])
        self.assertIs(type(first['_observation_id']), int)

    def test_expiry_appends_history_without_overwriting_previous_page(self):
        first = self.get()
        self.now += 301
        second = self.get()
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(first['_observation_id'], second['_observation_id'])
        self.assertEqual(len(self.rows('public_pages')), 2)
        self.assertEqual(len(self.rows('public_post_observations')), 2)

    def test_fresh_after_is_strict_despite_ttl_and_accepts_aware_bounds(self):
        first = self.get()
        self.now += 1
        second = self.get(fresh_after='2026-10-23T12:00:01+00:00')
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(first['_observation_id'], second['_observation_id'])
        for bound in (self.now, datetime.fromtimestamp(self.now, timezone.utc),
                      '2026-10-23T17:00:01+05:00'):
            self.assertEqual(self.get(fresh_after=bound)['_observation_id'],
                             second['_observation_id'])

    def test_future_observation_and_invalid_freshness_are_never_fresh(self):
        self.get()
        self.now -= 1
        self.assertIsNone(self.store.lookup(7, 100, 0, clock=lambda: self.now))
        for bound in ('2026-10-23T12:00:00', 'garbage', float('nan')):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                self.get(fresh_after=bound)
        with self.assertRaises(ValueError):
            self.get(max_age_seconds=-1)

    def test_fractional_real_clock_precision_does_not_invalidate_its_own_cache(self):
        self.now += 0.1234567
        first = self.get()
        self.assertEqual(self.get()['_observation_id'], first['_observation_id'])
        self.now += 1
        self.assertEqual(self.get()['_observation_id'], first['_observation_id'])
        self.assertEqual(self.calls, 1)

    def test_lookup_missing_does_not_create_file_or_schema(self):
        self.assertIsNone(self.store.lookup(7, 100, 0))
        self.assertFalse(self.path.exists())
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE unrelated (id INTEGER)')
            db.commit()
        self.assertIsNone(self.store.lookup(7, 100, 0))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [('unrelated',)])

    def test_foreign_database_is_rejected_before_network_or_schema_writes(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE action_attempts (id INTEGER)')
            db.commit()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.get()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_exact_owner_count_offset_identity(self):
        self.get()
        self.assertIsNone(self.store.lookup(8, 100, 0, clock=lambda: self.now))
        self.assertIsNone(self.store.lookup(7, 99, 0, clock=lambda: self.now))
        self.assertIsNone(self.store.lookup(7, 100, 100, clock=lambda: self.now))

    def test_empty_successful_page_is_reusable(self):
        first = self.store.get(7, 100, 300, lambda: {'count': 0, 'items': []},
                               clock=lambda: self.now)
        self.assertEqual(self.store.lookup(7, 100, 300, clock=lambda: self.now)['items'], [])
        self.assertEqual(first['count'], 0)
        self.assertEqual(self.rows('public_post_observations'), [])

    def test_invalid_identity_cannot_spend_network_budget(self):
        for gid, count, offset in [(True, 100, 0), (-7, 100, 0), (7, 101, 0),
                                   (7, 100, -1), (7, 0, 0)]:
            with self.subTest(identity=(gid, count, offset)), self.assertRaises(ValueError):
                self.store.get(gid, count, offset, self.fetch)
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.path.exists())

    def test_timestamp_corruption_and_naive_time_cannot_be_reused(self):
        self.get()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE public_pages SET observed_at='2026-10-23T12:00:00'")
            db.commit()
        self.assertIsNone(self.store.lookup(7, 100, 0, clock=lambda: self.now))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE public_pages SET observed_at='2026-10-23T12:00:01+00:00'")
            db.commit()
        self.assertIsNone(self.store.lookup(7, 100, 0, clock=lambda: self.now))

    def test_missing_null_counters_are_preserved_and_numeric_observation_is_null(self):
        payload = page()
        del payload['items'][0]['likes']
        payload['items'][0]['views'] = None
        payload['items'][0]['comments'] = {'count': None}
        got = self.store.get(7, 100, 0, lambda: payload, clock=lambda: self.now)
        cached = self.store.lookup(7, 100, 0, clock=lambda: self.now)
        self.assertNotIn('likes', got['items'][0])
        self.assertIsNone(cached['items'][0]['views'])
        self.assertIsNone(cached['items'][0]['comments']['count'])
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT like_count,comment_count,repost_count,view_count FROM public_post_observations').fetchone()
        self.assertEqual(row, (None, None, 4, None))

    def test_only_minimal_public_projection_is_persisted(self):
        payload = page()
        payload['access_token'] = 'SECRET'
        payload['items'][0].update(text='SECRET', attachments=['SECRET'])
        payload['items'][0]['likes']['user_likes'] = 1
        payload['items'][0]['copy_history'] = [{'id': 15, 'owner_id': 82,
                                               'date': 1792600000, 'text': 'SECRET'}]
        self.store.get(7, 100, 0, lambda: payload, clock=lambda: self.now)
        got = self.store.lookup(7, 100, 0, clock=lambda: self.now)
        self.assertNotIn('SECRET', json.dumps(got))
        self.assertEqual(got['items'][0]['copy_history'][0],
                         {'id': 15, 'owner_id': 82, 'date': 1792600000})
        self.assertNotIn('user_likes', got['items'][0]['likes'])
        self.assertNotIn(b'SECRET', self.path.read_bytes())

    def test_invalid_payloads_are_returned_unchanged_but_never_cached(self):
        cases = [ {'error': {'error_code': 29}}, {'count': 1, 'items': None} ]
        for field, bad in [('owner_id', 7), ('id', True), ('date', -1),
                           ('date', self.now + 1000), ('is_pinned', '0'),
                           ('likes', {'count': False}), ('reposts', {'count': '2'})]:
            payload = page()
            payload['items'][0][field] = bad
            cases.append(payload)
        duplicate = page()
        duplicate['count'] = 2
        duplicate['items'] *= 2
        cases.append(duplicate)
        for payload in cases:
            with self.subTest(payload=payload):
                got = self.store.get(7, 100, 0, lambda: payload, clock=lambda: self.now)
                self.assertEqual(got, payload)
                self.assertIsNone(self.store.lookup(7, 100, 0, clock=lambda: self.now))
        self.assertEqual(len(self.rows('public_pages')), 0)

    def test_network_errors_do_not_write_or_fallback_to_expired_cache(self):
        self.get()
        self.now += 301
        def fail():
            raise RuntimeError('network failure')
        with self.assertRaises(RuntimeError):
            self.store.get(7, 100, 0, fail, clock=lambda: self.now)
        self.assertEqual(len(self.rows('public_pages')), 1)

    def test_insert_failure_rolls_back_page_and_observations(self):
        self.get()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER reject_post BEFORE INSERT ON public_post_observations BEGIN SELECT RAISE(ABORT,'failed'); END")
            db.commit()
        self.now += 301
        with self.assertRaises(sqlite3.IntegrityError):
            self.get()
        self.assertEqual(len(self.rows('public_pages')), 1)
        self.assertEqual(len(self.rows('public_post_observations')), 1)

    def test_corrupt_stored_payload_cannot_be_reused(self):
        self.get()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE public_pages SET payload_json='not json'")
            db.commit()
        self.assertIsNone(self.store.lookup(7, 100, 0, clock=lambda: self.now))
        self.get()
        self.assertEqual(self.calls, 2)

    def test_busy_os_lease_defers_second_consumer_without_duplicate_fetch(self):
        from vk_unified import RunAlreadyActive
        started, release = threading.Event(), threading.Event()
        failures = []
        results = []
        def slow():
            self.calls += 1
            started.set()
            if not release.wait(10):
                raise TimeoutError()
            return page()
        def first():
            try:
                results.append(self.store.get(7, 100, 0, slow, clock=lambda: self.now))
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(started.wait(10))
        try:
            with self.assertRaises(RunAlreadyActive):
                self.get()
            self.assertEqual(self.calls, 1)
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(failures)
        self.assertEqual(len(results), 1)
        self.assertTrue(self.get()['_cache_hit'])
        self.assertEqual(self.calls, 1)


if __name__ == '__main__':
    unittest.main()
