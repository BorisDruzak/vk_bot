import json
import tempfile
import unittest
from unittest.mock import MagicMock
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

import vk_accounts as accounts
import vk_control as control


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name,value in [('ROOT',self.root),('REGISTRY_PATH',self.root/'registry.json'),
                           ('BINDINGS_PATH',self.root/'bindings.json')]:
            p=patch.object(accounts,name,value);p.start();self.addCleanup(p.stop)
        accounts.register_detected_id(accounts.BINDINGS_PATH,1,111)
        accounts.register_detected_id(accounts.BINDINGS_PATH,2,222)

    def test_saved_binding_is_not_authentication(self):
        import vk_authentication as auth
        self.assertEqual(auth.status(1)['state'],'unverified')

    def test_wrong_identity_in_second_profile_never_ready(self):
        import vk_authentication as auth
        row=auth.observe(2,111)
        self.assertEqual(row['state'],'wrong_account')
        self.assertEqual(row['expected_vk_id'],222)
        self.assertEqual(row['observed_vk_id'],111)
        self.assertFalse(auth.may_finish(2))

    def test_empty_header_clears_previous_positive_observation(self):
        import vk_authentication as auth
        auth.observe(1,111)
        self.assertEqual(auth.status(1)['state'],'matched')
        auth.observe(1,None)
        self.assertEqual(auth.status(1)['state'],'not_authenticated')

    def test_proof_is_account_specific_fresh_and_binding_specific(self):
        import vk_authentication as auth
        now=datetime.now(timezone.utc)
        auth.observe(1,111,now=now-timedelta(seconds=301))
        self.assertEqual(auth.status(1,now=now)['state'],'stale')
        auth.observe(1,111,now=now)
        self.assertEqual(auth.status(2,now=now)['state'],'unverified')
        bindings=accounts.load_bindings(accounts.BINDINGS_PATH)
        bindings['accounts']['1']['vk_id']=999
        accounts.atomic_json(accounts.BINDINGS_PATH,bindings)
        self.assertEqual(auth.status(1,now=now)['state'],'unverified')

    def test_legacy_ready_without_identity_proof_is_unverified(self):
        raw={'state':'ready','detected':True,'at':datetime.now(timezone.utc).isoformat(),'session':'x'}
        accounts.atomic_json(control.login_path(2),raw)
        self.assertEqual(control.login_status(2)['state'],'unverified')
        self.assertEqual(json.loads(control.login_path(2).read_text())['state'],'ready')

    def test_legacy_ready_cannot_finish_even_with_other_fresh_proof(self):
        import vk_authentication as auth
        auth.observe(1,111)
        accounts.atomic_json(control.login_path(1),{'state':'ready','detected':True,
            'at':datetime.now(timezone.utc).isoformat(),'session':'x'})
        with self.assertRaises(accounts.AccountError):control.finish_login(1)
        self.assertFalse(control.login_path(1).with_suffix('.finish.json').exists())

    def test_new_check_invalidates_previous_match_before_launch(self):
        import vk_authentication as auth
        accounts.configure_account(1,enabled=False)
        auth.observe(1,111)
        with patch.object(control,'launch_login_task') as launch:
            control.check_login(1)
        launch.assert_called_once()
        self.assertEqual(auth.status(1)['state'],'unverified')
        with self.assertRaises(accounts.AccountError):accounts.configure_account(1,enabled=True)

    def test_enable_requires_real_recent_matched_proof(self):
        import vk_authentication as auth
        accounts.configure_account(1,enabled=False)
        with self.assertRaises(accounts.AccountError):accounts.configure_account(1,enabled=True)
        auth.observe(1,222)
        with self.assertRaises(accounts.AccountError):accounts.configure_account(1,enabled=True)
        auth.observe(1,111)
        self.assertTrue(accounts.configure_account(1,enabled=True)['enabled'])

    def test_legacy_active_session_is_not_relaunched_or_closed(self):
        accounts.configure_account(1,enabled=False)
        raw={'state':'ready','detected':True,'at':datetime.now(timezone.utc).isoformat(),'session':'x'}
        accounts.atomic_json(control.login_path(1),raw)
        with patch.object(control,'launch_login_task') as launch:
            self.assertEqual(control.start_login(1)['state'],'unverified')
            launch.assert_not_called()

    def test_live_checker_compares_header_in_exact_profile_and_preserves_binding(self):
        import vk_authentication as auth
        accounts.configure_account(2,enabled=False)
        session='c'*32
        accounts.atomic_json(control.login_path(2),{'session':session,'state':'opening'})
        fake=MagicMock()
        context=fake.__enter__.return_value.chromium.launch_persistent_context.return_value
        with patch.dict(accounts.PROFILE_DIRS,{2:self.root/'profile2'}), \
             patch.dict(accounts.LOCK_DIRS,{2:self.root/'profile2.lock'}), \
             patch('playwright.sync_api.sync_playwright',return_value=fake), \
             patch.object(accounts,'inspect_context',return_value=111):
            self.assertEqual(control.login_worker(2,session,check_only=True),2)
            self.assertEqual(auth.status(2)['state'],'wrong_account')
        self.assertEqual(accounts.load_bindings(accounts.BINDINGS_PATH)['accounts']['2']['vk_id'],222)
        context.close.assert_called_once()


if __name__=='__main__':unittest.main()
