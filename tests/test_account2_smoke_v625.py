"""v6.25: strictly one-post, independently bound account #2 smoke tests. No VK traffic."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_api_read as api
import vk_accounts as accounts
import vk_account_smoke as smoke
import vk_batch as engine
import vk_groups as multi
import vk_unified as unified


class SecondAccountSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / 'first.sqlite3'
        self.second_path = self.root / 'second.sqlite3'
        self.bindings = self.root / 'bindings.json'
        self.group_id = 188206921
        self.post_id = 13418
        self.stamp = int(datetime.now(timezone.utc).timestamp()) - 300
        self.profile1 = self.root / '.vk-local-profile'
        self.profile2 = self.root / '.vk-local-profile-2'
        self.lock1 = self.root / 'lock-1'
        self.lock2 = self.root / 'lock-2'
        p1=patch.dict(accounts.PROFILE_DIRS, {1:self.profile1, 2:self.profile2})
        p2=patch.dict(accounts.LOCK_DIRS, {1:self.lock1, 2:self.lock2})
        p1.start();p2.start();self.addCleanup(p1.stop);self.addCleanup(p2.stop)
        self.profile2.mkdir()
        accounts.register_detected_id(self.bindings, 1, 101001)
        accounts.register_detected_id(self.bindings, 2, 202002)
        self.first = engine.db_open(self.catalog_path)
        self.addCleanup(self.first.close)
        multi.init_dates(self.first)
        api.init_api(self.first)
        engine.add_posts(self.first, self.group_id, [self.post_id])
        multi.save_date(self.first, self.group_id, self.post_id, self.stamp,
                        'vk-api-wall.get', True)
        api.init_api(self.first)

    def call(self, **overrides):
        opts=dict(binding_path=self.bindings, catalog_db=self.catalog_path,
                  account_db=self.second_path, config_path=multi.CONFIG_PATH)
        opts.update(overrides)
        return smoke.test_post(2, f'{self.group_id}_{self.post_id}', **opts)

    def test_parse_posts_cannot_open_other_hosts_or_post_types(self):
        expected=(self.group_id,self.post_id)
        for value in [f'{self.group_id}_{self.post_id}',
                      f'https://vk.com/wall-{self.group_id}_{self.post_id}',
                      f'https://vk.ru/wall-{self.group_id}_{self.post_id}']:
            self.assertEqual(smoke.parse_post(value), expected)
        for value in [f'https://example.com/wall-{self.group_id}_{self.post_id}',
                      f'https://vk.com.evil.test/wall-{self.group_id}_{self.post_id}',
                      f'https://vk.com/wall-{self.group_id}_{self.post_id}?x=1',
                      'id12345','club188206921', '0_100', '123_0']:
            with self.assertRaises(smoke.SmokeError):
                smoke.parse_post(value)

    def test_pinned_vk_id_required_and_duplicate_id_rejected(self):
        self.assertEqual(smoke.pinned_user_id(2,binding_path=self.bindings),202002)
        self.assertRaises(smoke.SmokeError, smoke.pinned_user_id, 1,
                          binding_path=self.bindings)
        altered = json.loads(self.bindings.read_text())
        altered['accounts']['2']['vk_id']=101001
        self.bindings.write_text(json.dumps(altered))
        with self.assertRaises(smoke.SmokeError):
            smoke.pinned_user_id(2,binding_path=self.bindings)

    def test_only_proven_vk_api_post_is_eligible(self):
        self.assertEqual(smoke.catalog_post(self.group_id,self.post_id,
                          catalog_db=self.catalog_path),self.stamp)
        with self.assertRaises(smoke.SmokeError):
            smoke.catalog_post(self.group_id,self.post_id+1,catalog_db=self.catalog_path)
        self.first.execute('UPDATE api_seen_posts SET valid=0')
        self.first.commit()
        with self.assertRaises(smoke.SmokeError):
            smoke.catalog_post(self.group_id,self.post_id,catalog_db=self.catalog_path)

    def test_old_or_inexact_date_fails_closed(self):
        yesterday = self.stamp-10*86400
        self.first.execute('UPDATE api_seen_posts SET published_at=?',(yesterday,))
        self.first.execute('UPDATE post_publication SET published_at=?',(yesterday,))
        self.first.commit()
        with self.assertRaises(smoke.SmokeError):
            smoke.catalog_post(self.group_id,self.post_id,catalog_db=self.catalog_path)
        self.first.execute('UPDATE post_publication SET published_at=?,exact=0',(self.stamp,))
        self.first.execute('UPDATE api_seen_posts SET published_at=?',(self.stamp,))
        self.first.commit()
        with self.assertRaises(smoke.SmokeError):
            smoke.catalog_post(self.group_id,self.post_id,catalog_db=self.catalog_path)

    def test_cannot_adopt_copy_of_first_account_ledger(self):
        bad = engine.db_open(self.second_path)
        engine.add_posts(bad,self.group_id,[self.post_id])
        engine.status_write(bad,self.group_id,self.post_id,'like','verified','FIRST ACCOUNT')
        with self.assertRaises(smoke.SmokeError):
            smoke.init_account2(bad,202002)
        self.assertIsNone(bad.execute('SELECT * FROM local_account_identity').fetchone())
        bad.close()

    def test_independent_id_must_match_database(self):
        with closing(engine.db_open(self.second_path)) as second:
            smoke.init_account2(second, 202002)
            with self.assertRaises(smoke.SmokeError):
                smoke.init_account2(second,303003)
            self.assertEqual(second.execute('SELECT vk_id FROM local_account_identity').fetchone()[0],202002)

    def test_preflight_checks_before_opening_browser(self):
        self.lock1.touch()
        with patch.object(smoke,'sync_playwright',side_effect=AssertionError('browser launched')):
            with self.assertRaises(smoke.SmokeError):
                self.call()
        self.assertFalse(self.second_path.exists())

    def test_attempt_recorded_only_in_second_sqlite(self):
        with closing(engine.db_open(self.second_path)) as second:
            smoke.init_account2(second,202002)
            recorder=smoke.OnePostAttemptRecorder(second,self.group_id,self.post_id)
            recorder.reserve(self.post_id,'like')
            recorder.reserve(self.post_id,'repost')
            self.assertEqual(second.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],2)
            with self.assertRaises(smoke.SmokeError):
                recorder.reserve(self.post_id,'like')
            with self.assertRaises(smoke.SmokeError):
                recorder.reserve(self.post_id+1,'repost')
        self.assertEqual(self.first.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertEqual(self.first.execute('SELECT like_status FROM posts').fetchone()[0],'pending')

    def test_identity_mismatch_never_calls_actions(self):
        self.assertRaises(smoke.SmokeError,smoke._live_identity,MagicMock(),202002)
        with patch.object(accounts,'inspect_context',return_value=101001):
            with self.assertRaises(smoke.SmokeError):
                smoke._live_identity(MagicMock(),202002)

    def fake_browser(self):
        page=MagicMock()
        page.url=f'https://vk.com/wall-{self.group_id}_{self.post_id}'
        context=MagicMock()
        context.pages=[page]
        browser=MagicMock()
        browser.chromium.launch_persistent_context.return_value=context
        manager=MagicMock()
        manager.__enter__.return_value=browser
        manager.__exit__.return_value=False
        return manager

    def _simulated_pass(self, *, confirm: bool, which: str, state=None):
        state = state or {'liked':False,'reposted':False,'source':'inline',
                          'canLike':True,'canRepost':True}
        def simulate(page, db, group_id, row, pause, safety, actions, already_open):
            self.assertEqual(group_id, self.group_id)
            self.assertTrue(already_open)
            for action in actions:
                if row[f'{action}_status']=='pending':
                    current = state['liked' if action=='like' else 'reposted']
                    if current:
                        engine.status_write(db,group_id,self.post_id,action,'already')
                    else:
                        safety.reserve(self.post_id,action)
                        engine.status_write(db,group_id,self.post_id,action,'verified')
            return True
        patches=[patch.object(smoke,'sync_playwright',return_value=self.fake_browser()),
                 patch.object(smoke.engine,'go_to_post',return_value=None),
                 patch.object(smoke,'_live_identity',return_value=None),
                 patch.object(smoke.multi,'exact_post_date_info',return_value={'publishedAt':self.stamp}),
                 patch.object(smoke.engine,'refresh_state',return_value=state),
                 patch.object(smoke.engine,'wait_post_footer_info',return_value={'ok':True,'liked':state['liked']}),
                 patch.object(smoke.engine,'process_one',side_effect=simulate)]
        for p in patches:
            p.start();self.addCleanup(p.stop)
        with redirect_stdout(io.StringIO()):
            return self.call(which=which,confirm=confirm)

    def test_readonly_opens_second_profile_and_makes_no_attempt(self):
        self.assertEqual(self._simulated_pass(confirm=False,which='both'),0)
        with closing(engine.db_open(self.second_path)) as second:
            self.assertEqual(second.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(second.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_two_actions_recorded_only_in_second_account(self):
        self.assertEqual(self._simulated_pass(confirm=True,which='both'),0)
        with closing(engine.db_open(self.second_path)) as second:
            states=second.execute('SELECT like_status,repost_status FROM posts').fetchone()
            self.assertEqual(tuple(states),('verified','verified'))
            self.assertEqual(dict(second.execute('SELECT action,COUNT(*) FROM action_attempts GROUP BY action')),
                             {'like':1,'repost':1})
        self.assertEqual(self.first.execute('SELECT like_status FROM posts').fetchone()[0],'pending')
        self.assertEqual(self.first.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_account2_rate_lock_stops_before_click(self):
        with closing(engine.db_open(self.second_path)) as second:
            smoke.init_account2(second,202002)
            engine.lock_on_platform_warning(second,0,'VK 429')
        with patch.object(smoke,'sync_playwright',side_effect=AssertionError('should not open')):
            with self.assertRaises(engine.StopRun):
                self.call(confirm=True)

    def test_unified_routes_second_test_without_bulk_settings(self):
        with patch.object(smoke,'test_post',return_value=0) as target:
            argv=['--account','2','--test-post',f'{self.group_id}_{self.post_id}',
                  '--test-actions','like','--confirm']
            self.assertEqual(unified.main(argv),0)
            self.assertEqual(target.call_args.kwargs['which'],'like')
            self.assertEqual(target.call_args.kwargs['confirm'],True)
        with redirect_stdout(io.StringIO()),patch('sys.stderr',new=io.StringIO()):
            with self.assertRaises(SystemExit) as err:
                unified.main(['--account','2','--run-actions','--likes','30','--confirm'])
            self.assertEqual(err.exception.code,2)

    def test_account2_only_status_is_local(self):
        with patch.object(smoke,'print_test_status',return_value=0) as status:
            self.assertEqual(unified.main(['--account','2','--test-status']),0)
            status.assert_called_once_with(2)


if __name__=='__main__':
    unittest.main()
