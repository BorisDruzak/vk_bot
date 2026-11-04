import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import vk_analytics as calc
import vk_analytics_store as store
import vk_web as web
from vk_analytics_web import read_analytics


class CollaborationWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'vk_analytics.sqlite3'
        self.db = store.open_db(self.path)
        self.run = store.new_run(self.db, '2026-10-19', '2026-10-26',
                                 [dict(id=1, name='one'), dict(id=2, name='two')])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def shared(self, gid, local_pid, *, day='2026-10-19',
               at='2026-10-27T00:00:00+00:00', counts=(10, 2, 1, 100)):
        item = dict(owner_id=-9, id=70, date=calc.stamp(day) + 3600,
                    inner_type='wall_wallpost', type='post', post_type='post',
                    coowners=dict(coowner_post_id=dict(owner_id=-gid, post_id=local_pid),
                                  list=[dict(owner_id=-9), dict(owner_id=-1), dict(owner_id=-2)],
                                  requests=[]))
        item.update({key: {'count': value} for key, value in zip(store.COUNTERS, counts)})
        store.observe(self.db, self.run, gid, item, 'wall.get', at=at)
        return item

    def ordinary(self, gid, pid):
        item = dict(owner_id=-gid, id=pid, date=calc.stamp('2026-10-19') + 3600)
        item.update({key: {'count': value} for key, value in zip(store.COUNTERS, (0, 0, 0, 100))})
        store.observe(self.db, self.run, gid, item, 'wall.get', at='2026-10-27T00:00:00+00:00')

    def finish(self):
        self.db.execute("UPDATE analytics_run_groups SET status='complete' WHERE run_id=?", (self.run,))
        self.db.execute('UPDATE analytics_runs SET started_at=?,finished_at=?,status=? WHERE id=?',
                        ('2026-10-27T00:00:00+00:00', '2026-10-27T01:00:00+00:00', 'complete', self.run))
        self.db.commit()

    def test_panel_uses_latest_whole_canonical_snapshot_and_actual_post_url(self):
        self.shared(1, 7, at='2026-10-27T05:10:00+05:00', counts=(99, 3, 2, 900))
        self.shared(2, 18, at='2026-10-27T00:20:00+00:00', counts=(12, 4, 1, 200))
        self.finish()
        result = read_analytics(self.root)
        district = result['district']
        self.assertEqual(district['posts'], 1)
        self.assertEqual([district[key] for key in store.COUNTERS], [12, 4, 1, 200])
        self.assertEqual(district['observed_er'], 8.5)
        self.assertEqual(district['wall_publications'], 2)
        self.assertEqual(district['shared_publication_duplicates'], 1)
        self.assertEqual([g['posts'] for g in result['groups']], [1, 1])
        self.assertEqual([g['publications'][0]['post_id'] for g in result['groups']], [7, 18])
        for group in result['groups']:
            publication = group['publications'][0]
            self.assertEqual(publication['canonical_owner_id'], -9)
            self.assertEqual(publication['canonical_post_id'], 70)
            self.assertEqual(publication['url'], 'https://vk.com/wall-9_70')

    def test_latest_missing_snapshot_keeps_panel_district_unknown(self):
        self.shared(1, 7)
        self.shared(2, 18, at='2026-10-27T00:20:00+00:00', counts=(12, 4, 1, None))
        self.finish()
        result = read_analytics(self.root)
        self.assertEqual(result['district']['posts'], 1)
        self.assertIsNone(result['district']['views'])
        self.assertIsNone(result['district']['er'])
        self.assertIsNone(result['district']['observed_er'])
        self.assertIsNone(result['district']['observed_p33'])
        self.assertEqual([g['completeness'] for g in result['groups']], ['complete', 'partial'])

    def conflict_fixture(self):
        self.shared(1, 7)
        item = self.shared(2, 18)
        for gid in (1, 2):
            self.ordinary(gid, 101)
            self.ordinary(gid, 102)
        item['date'] = calc.stamp('2026-10-18') + 3600
        store.record_identity_witness(self.db, self.run, 2, item, '2026-10-27T00:30:00+00:00')
        self.finish()

    def test_outside_period_witness_blocks_panel_weekly_exact_score(self):
        self.conflict_fixture()
        result = read_analytics(self.root)
        self.assertEqual(result['district'].get('canonical_date_conflicts'), 1)
        self.assertEqual(result['district']['completeness'], 'partial')
        self.assertIsNone(result['district']['likes'])
        self.assertEqual([g['weeks'][0]['status'] for g in result['groups']], ['met', 'met'])
        self.assertIsNone(result['weekly_summary'][0]['p32'])

    def test_report_and_publications_are_read_in_same_readonly_transaction(self):
        self.shared(1, 7)
        self.shared(2, 18)
        self.finish()
        before = list(self.db.iterdump())
        report = calc.report
        def transaction_report(db, *args, **kwargs):
            self.assertTrue(db.in_transaction)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE analytics_runs SET status='changed'")
            return report(db, *args, **kwargs)
        with patch.object(calc, 'report', side_effect=transaction_report) as called:
            read_analytics(self.root)
        called.assert_called_once()
        self.assertEqual(list(self.db.iterdump()), before)

    def test_legacy_panel_without_identity_tables_preserves_local_urls_and_totals(self):
        self.ordinary(1, 7)
        self.ordinary(2, 7)
        self.db.execute('DROP TABLE analytics_post_identities')
        self.db.execute('DROP TABLE analytics_identity_witnesses')
        self.finish()
        result = read_analytics(self.root)
        self.assertEqual(result['district']['posts'], 2)
        self.assertEqual(result['groups'][0]['publications'][0]['url'], 'https://vk.com/wall-1_7')
        self.assertEqual(result['groups'][1]['publications'][0]['url'], 'https://vk.com/wall-2_7')

    def test_authenticated_analytics_api_exposes_witness_conflict_and_canonical_link(self):
        self.conflict_fixture()
        auth = self.root / 'auth.json'
        web.write_auth(auth, 'fixture', 'fictional-analytics-password')
        rpc = Mock()
        server = web.make_server('127.0.0.1', 0, auth, rpc,
                                 analytics_reader=lambda **kwargs: read_analytics(self.root, **kwargs))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
            origin = f'http://127.0.0.1:{port}'
            connection.request('POST', '/api/login', json.dumps(dict(username='fixture', password='fictional-analytics-password')),
                               {'Origin': origin, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader('Set-Cookie').split(';')[0]
            csrf = json.loads(response.read())['csrf']
            connection.request('GET', '/api/analytics', headers={'Origin': origin, 'Cookie': cookie, 'X-CSRF-Token': csrf})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())
            connection.close()
            self.assertEqual(result['district'].get('canonical_date_conflicts'), 1)
            self.assertIsNone(result['weekly_summary'][0]['p32'])
            self.assertEqual(result['groups'][0]['publications'][0]['url'], 'https://vk.com/wall-9_70')
            rpc.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
