"""Platform challenges in every public reader are durable stops, not group errors."""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_catalog_scan as catalog
import vk_statistics_refresh as statistics
from vk_analytics_collect import LiveReader


class PublicChallengeTests(unittest.TestCase):
    def test_challenge_diagnostics_keep_only_safe_type_and_numeric_code(self):
        import vk_diagnostics as diagnostics
        with tempfile.TemporaryDirectory() as temporary:
            with closing(engine.db_open(Path(temporary)/'actor.sqlite3')) as db:
                client=api.VKReadClient(db,'synthetic',transport=lambda _:(200,{'error':{'error_code':14}}))
                with self.assertRaises(api.VkApiLimited) as caught:client.wall_get(101)
                chain=diagnostics.error_chain(caught.exception)
                self.assertEqual(chain[0]['type'],'VkApiProtection')
                self.assertEqual(chain[0].get('vk_error_code'),14)

    def test_catalog_challenges_latch_and_stop_before_another_group(self):
        for code in (14, 17):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                with closing(engine.db_open(Path(temporary)/'actor.sqlite3')) as db:
                    cycle = catalog.start_cycle(db, [{'id':101}, {'id':102}])
                    calls = []
                    def transport(params):
                        calls.append(params['owner_id'])
                        return 200, {'error':{'error_code':code, 'captcha_img':'PRIVATE'}}
                    with self.assertRaises(api.VkApiLimited):
                        catalog.step(db, 'synthetic', cycle, transport=transport, sleeper=lambda _:None)
                    self.assertIsNotNone(engine.safety_lock_state(db, 0))
                    self.assertEqual(catalog.coverage(db, cycle)['state'], 'stopped')
                    self.assertIsNone(catalog.step(db, 'synthetic', cycle, transport=transport))
                    self.assertEqual(calls, [-101])
                    self.assertNotIn('PRIVATE', '\n'.join(db.iterdump()))

    def test_statistics_and_analytics_challenges_block_subsequent_readers(self):
        for origin in ('statistics', 'analytics'):
            for code in (14, 17):
                with self.subTest(origin=origin, code=code), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    path = root/'actor.sqlite3'
                    with closing(engine.db_open(path)):pass
                    calls = []
                    def transport(params):
                        calls.append(params['owner_id'])
                        return 200, {'error':{'error_code':code}}
                    with patch.object(api, '_default_transport', side_effect=transport), \
                         patch.object(api, 'configured_token', return_value='synthetic'), \
                         patch.object(statistics.stats, '_names', return_value={101:'Fixture'}):
                        with self.assertRaises(api.VkApiFailure) as caught:
                            if origin == 'statistics':
                                statistics.refresh_one(path, 101, sleeper=lambda _:None)
                            else:
                                reader = LiveReader.__new__(LiveReader)
                                reader.root, reader.path, reader.token = root, path, 'synthetic'
                                reader.used, reader.max_requests, reader.before_request = 0, 2, None
                                reader._call(101, 'wall.get', {'count':100, 'offset':0})
                        self.assertIsInstance(caught.exception, api.VkApiLimited)
                        with closing(engine.db_open(path)) as db:
                            client = api.VKReadClient(db, 'synthetic', transport=transport)
                            with self.assertRaises(engine.StopRun):client.wall_get(102)
                        self.assertEqual(calls, [-101])
