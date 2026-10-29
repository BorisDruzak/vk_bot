"""Cached public statistics; temporary databases, no network or profiles."""
import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_groups as public_groups


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'catalog.sqlite3'
        self.db = engine.db_open(self.path)
        self.addCleanup(self.db.close)
        public_groups.init_dates(self.db)
        api.init_api(self.db)
        self.now = int(datetime.now(timezone.utc).timestamp())

    def statistics(self):
        self.assertIsNotNone(importlib.util.find_spec('vk_statistics'),
                             'cached read-only statistics module is required')
        import vk_statistics
        return vk_statistics

    def add(self, gid, pid, published, *, likes=1, views=None,
            observed='2026-10-22T05:00:00+00:00', valid=1):
        self.db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,?)',
                        (gid, pid, published, observed, valid))
        self.db.execute('''INSERT INTO api_post_engagement
            (group_id,post_id,like_count,repost_count,comment_count,view_count,observed_at)
            VALUES(?,?,?,2,3,?,?)''', (gid, pid, likes, views, observed))
        self.db.commit()

    def test_additive_views_migration_preserves_old_values_and_null(self):
        with closing(engine.db_open(Path(self.tmp.name) / 'old.sqlite3')) as db:
            db.execute('''CREATE TABLE api_post_engagement(group_id INTEGER,post_id INTEGER,
                like_count INTEGER,repost_count INTEGER,observed_at TEXT,PRIMARY KEY(group_id,post_id))''')
            db.execute("INSERT INTO api_post_engagement VALUES(1,2,0,4,'old')")
            db.commit()
            api.init_api(db)
            api.init_api(db)
            row = db.execute('SELECT * FROM api_post_engagement').fetchone()
            self.assertIn('view_count', row.keys())
            self.assertEqual((row['like_count'], row['repost_count'], row['view_count']), (0,4,None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0], 0)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE api_post_engagement SET view_count=-1')

    def test_catalog_ingests_real_views_and_invalid_or_missing_is_null(self):
        items = [dict(owner_id=-1,id=i+1,date=self.now-10-i,views={'count':value})
                 for i,value in enumerate([0,19,None,True,-1,'2'])]
        client = api.VKReadClient(self.db, 'fake-key', transport=lambda params:
            (200, {'response':{'count':len(items),'items':items}}))
        api.scan_one_group(self.db, client, {'id':1})
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(api_post_engagement)')}
        self.assertIn('view_count', columns)
        self.assertEqual([r[0] for r in self.db.execute(
            'SELECT view_count FROM api_post_engagement ORDER BY post_id')], [0,19,None,None,None,None])

    def test_batch_observation_replaces_views_without_affecting_priority_counts(self):
        item = dict(owner_id=-1,id=1,date=self.now-10,likes={'count':2},
                    reposts={'count':3},comments={'count':4},views={'count':10})
        engagement._observe_page(self.db,1,[item],self.now)
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(api_post_engagement)')}
        self.assertIn('view_count', columns)
        row = self.db.execute('SELECT * FROM api_post_engagement').fetchone()
        self.assertEqual((row['like_count'],row['repost_count'],row['comment_count'],row['view_count']), (2,3,4,10))
        item.pop('views')
        engagement._observe_page(self.db,1,[item],self.now)
        self.assertIsNone(self.db.execute('SELECT view_count FROM api_post_engagement').fetchone()[0])

    def test_posts_only_cached_valid_latest_100_with_optional_group(self):
        stats = self.statistics()
        for pid in range(1,106):
            self.add(1,pid,self.now-pid,views=pid)
        self.add(2,1,self.now,views=0)
        self.add(2,2,self.now+1,views=999,valid=0)
        self.db.execute('INSERT INTO api_seen_posts VALUES(3,1,?,?,1)', (self.now+2,'seen'))
        self.db.commit()
        before = self.path.read_bytes()
        with patch.object(api, '_default_transport', side_effect=AssertionError('no network')):
            rows = stats.posts(self.path,limit=1000)
        self.assertEqual(len(rows),100)
        self.assertEqual([(r['group_id'],r['post_id']) for r in rows[:2]],[(3,1),(2,1)])
        self.assertIsNone(rows[0]['observed_at'])
        self.assertIsNone(rows[0]['view_count'])
        self.assertEqual(rows[1]['view_count'],0)
        self.assertEqual(rows[1]['url'],'https://vk.com/wall-2_1')
        self.assertTrue(all(key in rows[1] for key in
            ('group_name','published_at','like_count','repost_count','comment_count','observed_at')))
        self.assertEqual([(r['group_id'],r['post_id']) for r in stats.posts(self.path,1,2)],[(1,1),(1,2)])
        self.assertEqual(self.path.read_bytes(),before)

    def test_group_coverage_never_labels_partial_sum_as_total_or_fresh(self):
        stats = self.statistics()
        self.add(1,1,self.now,likes=0,views=None,observed='2026-10-22T05:00:00+00:00')
        self.add(1,2,self.now-1,likes=4,views=12,observed='2026-10-22T06:00:00+00:00')
        self.add(1,3,self.now-2,likes=999,views=999,valid=0)
        row = next(r for r in stats.groups(self.path) if r['id']==1)
        self.assertEqual(row['posts'],2)
        self.assertEqual((row['like_count'],row['like_count_known_posts']), (4,2))
        self.assertIsNone(row['view_count'])
        self.assertEqual((row['view_count_known_posts'],row['view_count_known_sum']), (1,12))
        self.assertEqual(row['observed_at'],'2026-10-22T05:00:00+00:00')
        self.assertEqual(row['observed_until'],'2026-10-22T06:00:00+00:00')

    def test_legacy_statistics_do_not_migrate_or_invent_views(self):
        stats = self.statistics()
        legacy = Path(self.tmp.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db:
            db.execute('''CREATE TABLE api_seen_posts(group_id,post_id,published_at,seen_at,valid)''')
            db.execute("INSERT INTO api_seen_posts VALUES(1,2,123,'seen',1)")
            db.execute('CREATE TABLE api_post_engagement(group_id,post_id,like_count,repost_count,observed_at)')
            db.execute("INSERT INTO api_post_engagement VALUES(1,2,0,1,'observed')")
            db.commit()
        before=legacy.read_bytes()
        row=stats.posts(legacy)[0]
        self.assertEqual(row['like_count'],0)
        self.assertIsNone(row['view_count'])
        self.assertIsNone(row['comment_count'])
        self.assertEqual(legacy.read_bytes(),before)

    def test_missing_database_not_created_and_bad_filter_rejected(self):
        stats = self.statistics()
        missing=Path(self.tmp.name)/'missing.sqlite3'
        self.assertEqual(stats.posts(missing),[])
        self.assertEqual(stats.groups(missing),[])
        self.assertFalse(missing.exists())
        for group in (True,-1,'1 OR 1=1'):
            with self.assertRaises(ValueError): stats.posts(self.path,group)
        for limit in (True,0,-1,'100'):
            with self.assertRaises(ValueError): stats.posts(self.path,limit=limit)


if __name__ == '__main__':
    unittest.main()
