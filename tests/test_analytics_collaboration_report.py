import tempfile
import unittest
from pathlib import Path

import vk_analytics as calc
import vk_analytics_store as store


class CollaborationReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.open_db(Path(self.tmp.name) / 'analytics.sqlite3')
        self.db.execute('''CREATE TABLE IF NOT EXISTS analytics_post_identities(
          run_id TEXT NOT NULL, group_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
          owner_id INTEGER NOT NULL, canonical_post_id INTEGER NOT NULL, proof TEXT NOT NULL,
          PRIMARY KEY(run_id,group_id,post_id))''')
        self.run = store.new_run(self.db, '2026-10-08', '2026-10-15',
                                 [dict(id=1, name='one'), dict(id=2, name='two')])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def observe(self, gid, pid, *, canonical=None, measured='2026-10-16T00:00:00+00:00',
                day='2026-10-08', likes=10, comments=2, reposts=1, views=100):
        item = dict(owner_id=-gid, id=pid, date=calc.stamp(day) + 3600,
                    post_type='post', likes={'count': likes}, comments={'count': comments},
                    reposts={'count': reposts}, views={'count': views})
        store.observe(self.db, self.run, gid, item, 'wall.get', at=measured)
        if canonical:
            self.db.execute('INSERT OR REPLACE INTO analytics_post_identities VALUES(?,?,?,?,?,?)',
                            (self.run, gid, pid, *canonical, 'coowners'))

    def report(self):
        self.db.execute("UPDATE analytics_run_groups SET status='complete'")
        self.db.execute("UPDATE analytics_runs SET started_at=?,finished_at=? WHERE id=?",
                        ('2026-10-16T00:00:00+00:00', '2026-10-16T01:00:00+00:00', self.run))
        self.db.commit()
        return calc.report(self.db, self.run, weeks='period')

    def test_shared_post_counted_once_district_and_once_on_each_wall(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.observe(2, 7, canonical=(-9, 70))
        result = self.report()
        self.assertEqual(result['district']['posts'], 1)
        self.assertEqual(result['district']['likes'], 10)
        self.assertEqual([g['posts'] for g in result['groups']], [1, 1])
        self.assertEqual(result['district']['wall_publications'], 2)
        self.assertEqual(result['district']['shared_publication_duplicates'], 1)
        self.assertEqual(result['district']['aggregation_policy'], 'unique_canonical_post_latest_snapshot')

    def test_different_wall_local_ids_share_canonical_identity(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.observe(2, 18, canonical=(-9, 70))
        self.assertEqual(self.report()['district']['posts'], 1)

    def test_same_post_id_on_different_regular_walls_is_not_deduplicated(self):
        self.observe(1, 7)
        self.observe(2, 7)
        result = self.report()
        self.assertEqual(result['district']['posts'], 2)
        self.assertEqual(result['district']['likes'], 20)

    def test_latest_measurement_uses_one_whole_snapshot_with_timezone_order(self):
        self.observe(1, 7, canonical=(-9, 70), measured='2026-10-16T05:10:00+05:00',
                     likes=99, comments=3, reposts=2, views=900)
        self.observe(2, 18, canonical=(-9, 70), measured='2026-10-16T00:20:00+00:00',
                     likes=12, comments=4, reposts=1, views=200)
        district = self.report()['district']
        self.assertEqual([district[k] for k in store.COUNTERS], [12, 4, 1, 200])
        self.assertEqual(district['er'], 8.5)

    def test_latest_missing_counter_cannot_be_rescued_from_older_snapshot(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.observe(2, 18, canonical=(-9, 70), measured='2026-10-16T00:20:00+00:00',
                     likes=12, views=None)
        result = self.report()
        self.assertEqual(result['district']['completeness'], 'partial')
        self.assertEqual(result['district']['likes'], 12)
        self.assertIsNone(result['district']['views'])
        self.assertIsNone(result['district']['er'])
        self.assertEqual([g['completeness'] for g in result['groups']], ['complete', 'partial'])

    def test_conflicting_canonical_dates_make_district_unknown(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.observe(2, 18, canonical=(-9, 70), day='2026-10-09')
        result = self.report()
        self.assertEqual(result['district']['completeness'], 'partial')
        for key in (*store.COUNTERS, 'er', 'p33'):
            self.assertIsNone(result['district'][key])
        self.assertEqual([g['completeness'] for g in result['groups']], ['complete', 'complete'])

    def test_conflicting_date_outside_period_cannot_be_filtered_away(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.observe(2, 18, canonical=(-9, 70), day='2026-10-07')
        district = self.report()['district']
        self.assertEqual(district['canonical_date_conflicts'], 1)
        self.assertEqual(district['completeness'], 'partial')
        self.assertIsNone(district['likes'])

    def witness(self, gid, pid, owner, canonical_pid, day):
        self.db.execute('''CREATE TABLE IF NOT EXISTS analytics_identity_witnesses(
          run_id TEXT NOT NULL, group_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
          owner_id INTEGER NOT NULL, canonical_post_id INTEGER NOT NULL,
          published_at INTEGER NOT NULL, observed_at TEXT NOT NULL)''')
        self.db.execute('INSERT INTO analytics_identity_witnesses VALUES(?,?,?,?,?,?,?)',
                        (self.run, gid, pid, owner, canonical_pid, calc.stamp(day) + 3600,
                         '2026-10-16T00:10:00+00:00'))

    def test_out_of_period_witness_conflict_without_snapshot_stays_partial_on_resume(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.observe(2, 99)
        self.witness(2, 18, -9, 70, '2026-10-07')
        for _ in range(2):
            district = self.report()['district']
            self.assertEqual(district['canonical_date_conflicts'], 1)
            self.assertEqual(district['completeness'], 'partial')
            self.assertIsNone(district['likes'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_identity_witnesses').fetchone()[0], 1)

    def test_outside_period_only_identity_conflicts_do_not_taint_current_posts(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.observe(2, 99)
        self.witness(1, 20, -8, 80, '2026-10-06')
        self.witness(2, 21, -8, 80, '2026-10-07')
        district = self.report()['district']
        self.assertEqual(district['canonical_date_conflicts'], 0)
        self.assertEqual(district['completeness'], 'complete')

    def test_excluded_community_witness_is_not_part_of_report(self):
        self.observe(1, 7, canonical=(-9, 70))
        self.witness(2, 18, -9, 70, '2026-10-07')
        self.db.execute('UPDATE analytics_run_groups SET included=0 WHERE run_id=? AND group_id=2',
                        (self.run,))
        self.assertEqual(self.report()['district']['canonical_date_conflicts'], 0)

    def test_new_identity_report_does_not_rewrite_frozen_payload(self):
        self.observe(1, 7)
        self.observe(2, 18)
        frozen_id = store.freeze(self.db, self.report())
        saved = self.db.execute('SELECT payload FROM analytics_reports WHERE id=?',
                                (frozen_id,)).fetchone()[0]
        self.db.executemany('INSERT OR REPLACE INTO analytics_post_identities VALUES(?,?,?,?,?,?)',
                            [(self.run, 1, 7, -9, 70, 'coowner'),
                             (self.run, 2, 18, -9, 70, 'coowner')])
        self.assertEqual(self.report()['district']['posts'], 1)
        self.assertEqual(self.db.execute('SELECT payload FROM analytics_reports WHERE id=?',
                                        (frozen_id,)).fetchone()[0], saved)

    def test_legacy_database_without_identity_table_keeps_regular_totals(self):
        self.observe(1, 7)
        self.observe(2, 7)
        self.db.execute('DROP TABLE analytics_post_identities')
        self.db.execute('DROP TABLE IF EXISTS analytics_identity_witnesses')
        result = self.report()
        self.assertEqual(result['district']['posts'], 2)
        self.assertEqual(result['district']['likes'], 20)


if __name__ == '__main__':
    unittest.main()
