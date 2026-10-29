"""CLI identity checks revoke old proof on negative observations; no browser."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_accounts as accounts
import vk_authentication as auth


class BrowserAuthenticationProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.bindings=self.root/'bindings.json'
        for name,value in [('ROOT',self.root),('REGISTRY_PATH',self.root/'registry.json'),
                           ('BINDINGS_PATH',self.bindings)]:
            change=patch.object(accounts,name,value);change.start();self.addCleanup(change.stop)
        for name,values in [('PROFILE_DIRS',{1:self.root/'p1',2:self.root/'p2'}),
                            ('LOCK_DIRS',{1:self.root/'p1.lock',2:self.root/'p2.lock'})]:
            change=patch.dict(getattr(accounts,name),values);change.start();self.addCleanup(change.stop)
        accounts.register_detected_id(self.bindings,1,111)
        accounts.register_detected_id(self.bindings,2,222)
        self.history=self.root/'actor-history.sqlite3'
        self.history.write_bytes(b'unchanged actor history and attempts')
        self.saved_bindings=self.bindings.read_bytes()

    def inspect(self,identity,*,interactive=False,binding_path=None,commands=None,error=None):
        manager=MagicMock()
        context=manager.__enter__.return_value.chromium.launch_persistent_context.return_value
        with patch.object(accounts,'sync_playwright',return_value=manager),\
             patch.object(accounts,'inspect_context',return_value=identity,side_effect=error),\
             patch('builtins.input',side_effect=commands or []),\
             redirect_stdout(io.StringIO()):
            try:
                return accounts.browser_inspect(1,interactive,binding_path=binding_path or self.bindings)
            finally:
                context.close.assert_called_once()
                self.assertEqual(self.history.read_bytes(),b'unchanged actor history and attempts')

    def test_missing_identity_revokes_positive_proof_before_early_return(self):
        auth.observe(1,111)
        self.assertEqual(auth.status(1)['state'],'matched')
        with patch.object(accounts,'register_detected_id',wraps=accounts.register_detected_id) as register:
            self.assertEqual(self.inspect(None),2)
        register.assert_not_called()
        self.assertEqual(auth.status(1)['state'],'not_authenticated')
        self.assertEqual(self.bindings.read_bytes(),self.saved_bindings)

    def test_wrong_identity_revokes_positive_proof_before_rejected_registration(self):
        auth.observe(1,111)
        self.assertEqual(self.inspect(222),3)
        row=auth.status(1)
        self.assertEqual((row['state'],row['observed_vk_id'],row['can_finish']),('wrong_account',222,False))
        self.assertEqual(self.bindings.read_bytes(),self.saved_bindings)

    def test_interactive_check_without_binding_records_actual_negative_identity(self):
        auth.observe(1,111)
        with patch.object(accounts,'register_detected_id',wraps=accounts.register_detected_id) as register:
            self.assertEqual(self.inspect(222,interactive=True,commands=['CHECK','EXIT']),1)
        register.assert_not_called()
        self.assertEqual(auth.status(1)['state'],'wrong_account')
        self.assertEqual(self.bindings.read_bytes(),self.saved_bindings)

    def test_ambiguous_context_revokes_previous_positive_before_raising(self):
        auth.observe(1,111)
        with self.assertRaises(accounts.AccountError):
            self.inspect(None,error=accounts.AccountError('conflicting header identities'))
        self.assertEqual(auth.status(1)['state'],'not_authenticated')
        self.assertEqual(self.bindings.read_bytes(),self.saved_bindings)

    def test_first_binding_refreshes_expected_identity_proof_after_success(self):
        data=accounts.load_bindings(self.bindings);data['accounts'].pop('1')
        accounts.atomic_json(self.bindings,data)
        with patch.object(auth,'observe',wraps=auth.observe) as observe:
            self.assertEqual(self.inspect(111),0)
        self.assertEqual(observe.call_count,2)
        self.assertEqual(accounts.load_bindings(self.bindings)['accounts']['1']['vk_id'],111)
        self.assertEqual(auth.status(1)['state'],'matched')

    def test_matching_existing_binding_needs_only_one_actual_observation(self):
        with patch.object(auth,'observe',wraps=auth.observe) as observe:
            self.assertEqual(self.inspect(111),0)
        self.assertEqual(observe.call_count,1)
        self.assertEqual(auth.status(1)['state'],'matched')

    def test_custom_binding_checks_never_mutate_real_authentication_proof(self):
        auth.observe(1,111)
        proof_path=auth.evidence_path(1);before=proof_path.read_bytes()
        custom=self.root/'custom-bindings.json'
        with patch.object(auth,'observe',wraps=auth.observe) as observe:
            self.assertEqual(self.inspect(None,binding_path=custom),2)
            self.assertFalse(custom.exists())
            self.assertEqual(self.inspect(333,binding_path=custom),0)
        observe.assert_not_called()
        self.assertEqual(proof_path.read_bytes(),before)
        self.assertEqual(self.bindings.read_bytes(),self.saved_bindings)
        self.assertEqual(accounts.load_bindings(custom)['accounts']['1']['vk_id'],333)


if __name__=='__main__':unittest.main()
