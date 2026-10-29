"""Offline tests: two separated persistent Chromium profiles, no VK requests."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_accounts as accounts
import vk_unified as unified


class AccountProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name,value in (('ROOT',self.root),('REGISTRY_PATH',self.root/'registry.json')):
            setting=patch.object(accounts,name,value);setting.start();self.addCleanup(setting.stop)
        self.reg = self.root / 'vk_account_bindings.json'
        self.profile1 = self.root / '.vk-local-profile'
        self.profile2 = self.root / '.vk-local-profile-2'
        self.lock1 = self.root / 'legacy.lock'
        self.lock2 = self.root / 'two.lock'
        p1 = patch.dict(accounts.PROFILE_DIRS, {1:self.profile1, 2:self.profile2})
        p2 = patch.dict(accounts.LOCK_DIRS, {1:self.lock1, 2:self.lock2})
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_old_profile_is_not_renamed_or_overwritten(self):
        self.assertEqual(accounts.profile_for_account(1), self.profile1)
        self.assertEqual(accounts.profile_for_account(2), self.profile2)
        self.profile1.mkdir()
        sentinel = self.profile1 / 'auth_is_not_copied.txt'
        sentinel.write_text('keep existing data',encoding='utf8')
        accounts.register_detected_id(self.reg, 2, 222)
        self.assertEqual(sentinel.read_text(encoding='utf8'),'keep existing data')
        self.assertFalse(self.profile2.exists())

    def test_extract_numeric_id_only_from_valid_vk_header_link(self):
        yes = ['/id12345', 'https://vk.com/id12345', 'https://vk.ru/id12345',
               'https://vk.com/id12345?ignore=1']
        for href in yes:
            self.assertEqual(accounts.vk_id_from_profile_href(href),12345)
        no = ['/wall-100_200','https://example.org/id12345',
              'javascript:alert(1)', '/club999', '/id0', '/id12345/more',
              'https://vk.com.evil.test/id12345', None, '', '//evil.test/id12345']
        for href in no:
            self.assertIsNone(accounts.vk_id_from_profile_href(href),href)

    def test_read_id_from_header_not_comment_author(self):
        page = MagicMock()
        page.is_closed.return_value=False
        page.url='https://vk.com/feed'
        page.locator.return_value.count.return_value=1
        page.locator.return_value.first.get_attribute.return_value='/id222'
        self.assertEqual(accounts.inspect_header_identity(page),222)
        page.locator.assert_called_once_with(accounts.PROFILE_LINK)

    def test_login_redirect_not_a_verified_profile(self):
        page = MagicMock()
        page.is_closed.return_value=False
        page.url='https://id.vk.com/auth'
        self.assertIsNone(accounts.inspect_header_identity(page))
        page.locator.assert_not_called()

    def test_binding_is_stable_and_keeps_no_cookies_or_tokens(self):
        kind, _ = accounts.register_detected_id(self.reg, 1, 111)
        self.assertEqual(kind,'bound')
        kind, _ = accounts.register_detected_id(self.reg, 1, 111)
        self.assertEqual(kind,'matched')
        kind, _ = accounts.register_detected_id(self.reg, 2, 222)
        self.assertEqual(kind,'bound')
        data=json.loads(self.reg.read_text(encoding='utf8'))
        self.assertEqual(data['accounts']['1']['vk_id'],111)
        self.assertEqual(data['accounts']['2']['vk_id'],222)
        self.assertNotIn('cookie',self.reg.read_text(encoding='utf8').lower())
        self.assertNotIn('token',self.reg.read_text(encoding='utf8').lower())

    def test_duplicate_account_identity_fails_closed(self):
        accounts.register_detected_id(self.reg, 1, 111)
        before=self.reg.read_bytes()
        with self.assertRaises(accounts.AccountError):
            accounts.register_detected_id(self.reg, 2, 111)
        self.assertEqual(self.reg.read_bytes(),before)

    def test_existing_binding_is_not_silently_replaced(self):
        accounts.register_detected_id(self.reg, 2, 222)
        before=self.reg.read_bytes()
        with self.assertRaises(accounts.AccountError):
            accounts.register_detected_id(self.reg, 2, 999)
        self.assertEqual(self.reg.read_bytes(),before)

    def test_separate_locks_and_no_overlap(self):
        with accounts.exclusive_profile(1):
            with accounts.exclusive_profile(2):
                self.assertTrue(self.lock1.exists())
                self.assertTrue(self.lock2.exists())
                with self.assertRaises(accounts.AccountError):
                    with accounts.exclusive_profile(2):
                        pass
            self.assertFalse(self.lock2.exists())
        self.assertFalse(self.lock1.exists())

    def test_local_accounts_report_does_not_open_browser(self):
        accounts.register_detected_id(self.reg, 1, 111)
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(accounts.accounts_report(self.reg),0)
        self.assertIn('VK ID 111',output.getvalue())
        self.assertIn('личность не привязана',output.getvalue())

    def test_unified_routes_account_2_login_without_loading_config(self):
        with patch.object(accounts,'run_account_mode',return_value=0) as route, \
             patch.object(unified.multi,'load_config',side_effect=AssertionError('Loaded config')):
            self.assertEqual(unified.main(['--account','2','--login-only']),0)
        route.assert_called_once_with(2,'login-only')

    def test_unified_routes_account_2_read_only_check(self):
        with patch.object(accounts,'run_account_mode',return_value=0) as route:
            self.assertEqual(unified.main(['--account','2','--check-account']),0)
        route.assert_called_once_with(2,'check-account')

    def test_unified_routes_account2_batch_through_isolated_handler(self):
        with patch.object(unified,'run_account2_mode',return_value=0) as handler:
            self.assertEqual(unified.main(['--account','2','--run-actions','--likes','1','--confirm']),0)
            handler.assert_called_once()
            self.assertEqual(handler.call_args.args[0].account,2)

    def test_empty_header_never_binds_identity(self):
        context=MagicMock()
        page=MagicMock()
        context.pages=[page]
        page.is_closed.return_value=False
        page.url='https://vk.com/feed'
        page.locator.return_value.count.return_value=0
        page.wait_for_timeout.return_value=None
        self.assertIsNone(accounts.inspect_context(context,attempts=2))
        self.assertFalse(self.reg.exists())

    def test_unknown_identity_check_does_not_mutate_registry(self):
        context=MagicMock(); page=MagicMock()
        context.new_page.return_value=page
        context.pages=[page]
        context.close.return_value=None
        mock_pw=MagicMock()
        mock_pw.chromium.launch_persistent_context.return_value=context
        manager=MagicMock()
        manager.__enter__.return_value=mock_pw
        manager.__exit__.return_value=False
        with patch.object(accounts,'sync_playwright',return_value=manager), \
             patch.object(accounts,'inspect_context',return_value=None), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(accounts.browser_inspect(2,False,binding_path=self.reg),2)
        self.assertFalse(self.reg.exists())
        self.assertTrue(self.profile2.is_dir())
        self.assertFalse(self.lock2.exists())

    def test_check_account_binds_only_its_own_directory(self):
        context=MagicMock(); page=MagicMock()
        context.new_page.return_value=page
        context.pages=[page]
        mock_pw=MagicMock()
        mock_pw.chromium.launch_persistent_context.return_value=context
        manager=MagicMock()
        manager.__enter__.return_value=mock_pw
        manager.__exit__.return_value=False
        with patch.object(accounts,'sync_playwright',return_value=manager), \
             patch.object(accounts,'inspect_context',return_value=222), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(accounts.browser_inspect(2,False,binding_path=self.reg),0)
        kwargs=mock_pw.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs['user_data_dir'],str(self.profile2))
        self.assertEqual(accounts.load_bindings(self.reg)['accounts']['2']['vk_id'],222)
        self.assertFalse(self.profile1.exists())


if __name__=='__main__':
    unittest.main()
