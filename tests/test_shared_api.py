import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_statistics as statistics


class SharedApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = engine.db_open(self.root/'vk_batch.sqlite3')
        self.now = 1792738800.125
        self.calls = []
        self.payload = {'count': 1, 'items': [dict(owner_id=-1, id=1,
            date=int(self.now)-60, likes={'count': 4}, reposts={'count': 2},
            comments={'count': 3})]}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def client(self, **kwargs):
        def transport(params):
            self.calls.append(params['offset'])
            return 200, {'response': self.payload}
        return api.VKReadClient(self.db, 'test-only', pause_seconds=5, per_run=1,
            per_24h=1, transport=transport, clock=lambda: self.now,
            sleeper=lambda seconds: None, **kwargs)

    def enable(self):
        (self.root/'.vk-public-data.json').write_text(json.dumps(dict(version=1, enabled=True)))

    def test_opt_in_two_consumers_one_http_and_original_timestamp(self):
        self.enable()
        a=self.client(); first=a.wall_get(1)
        self.now += 1
        b=self.client(); second=b.wall_get(1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((a.used,b.used), (1,0))
        self.assertEqual(first['_observed_at'], second['_observed_at'])
        self.assertTrue(second['_cache_hit'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],1)
        row=statistics.posts(self.root/'vk_batch.sqlite3',1)[0]
        self.assertEqual((row['like_count'],row['view_count'],row['observed_at']),
                         (4,None,first['_observed_at']))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_action_fresh_floor_does_not_use_cached_data_at_budget_limit(self):
        self.enable();self.client().wall_get(1);self.now+=1
        reader=self.client(fresh_after=self.now)
        self.assertFalse(reader.has_cached_wall(1))
        with self.assertRaises(api.VkApiFailure):reader.wall_get(1)
        self.assertEqual(len(self.calls),1)

    def test_cache_hit_still_checks_platform_guard(self):
        self.enable();self.client().wall_get(1)
        with patch.object(engine,'require_platform_unlocked',side_effect=RuntimeError('guard')):
            with self.assertRaises(RuntimeError):self.client().wall_get(1)
        self.assertEqual(len(self.calls),1)

    def test_disabled_custom_transport_does_not_touch_cache(self):
        self.enable();data=self.client(shared_path=False).wall_get(1)
        self.assertNotIn('_cache_hit',data)
        self.assertFalse((self.root/'vk_public_data.sqlite3').exists())

    def test_unactivated_legacy_and_bad_config(self):
        self.assertNotIn('_cache_hit',self.client().wall_get(1))
        (self.root/'.vk-public-data.json').write_text('{broken')
        with self.assertRaises(ValueError):self.client()

    def test_old_cache_cannot_overwrite_new_projection(self):
        self.enable();reader=self.client();first=reader.wall_get(1)
        self.db.execute("UPDATE api_post_engagement SET like_count=99, observed_at=?",
            ('2026-12-01T00:00:00+00:00',));self.db.commit()
        reader.wall_get(1)
        self.assertEqual(self.db.execute('SELECT like_count FROM api_post_engagement').fetchone()[0],99)


if __name__=='__main__':unittest.main()
