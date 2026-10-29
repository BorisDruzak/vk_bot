import json
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock
from pathlib import Path
from unittest.mock import patch

import vk_accounts as accounts


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for key,value in [('ROOT',self.root),('REGISTRY_PATH',self.root/'registry.json'),
                          ('BINDINGS_PATH',self.root/'bindings.json')]:
            p=patch.object(accounts,key,value);p.start();self.addCleanup(p.stop)
        for key,values in [('PROFILE_DIRS',{1:self.root/'.vk-local-profile',2:self.root/'.vk-local-profile-2'}),
                           ('LOCK_DIRS',{1:self.root/'.vk-unified-running.lock',2:self.root/'.vk-unified-running-2.lock'})]:
            p=patch.dict(getattr(accounts,key),values);p.start();self.addCleanup(p.stop)
        accounts.register_detected_id(accounts.BINDINGS_PATH,1,111)
        accounts.register_detected_id(accounts.BINDINGS_PATH,2,222)
        for number in (1,2):accounts.configure_account(number,enabled=False)

    def flexible(self):
        import vk_connections as connections
        connections.initialize()
        return connections

    def test_initialize_archives_legacy_cards_without_changing_bindings(self):
        before=accounts.BINDINGS_PATH.read_bytes()
        c=self.flexible()
        self.assertEqual(c.visible_numbers(),[])
        self.assertEqual(accounts.BINDINGS_PATH.read_bytes(),before)
        self.assertEqual(accounts.account_numbers(),[1,2])

    def test_first_new_connection_accepts_any_new_vk_id(self):
        c=self.flexible();slot=accounts.add_account('Рабочий')
        number=c.connect(slot['account'],333)
        self.assertEqual(number,3)
        self.assertEqual(c.visible_numbers(),[3])
        self.assertFalse(accounts.account_enabled(3))
        import vk_authentication as auth
        self.assertEqual(auth.status(3)['state'],'matched')

    def test_existing_vk_reuses_actor_and_history_without_duplicate_binding(self):
        c=self.flexible();slot=accounts.add_account('Мой VK')
        history=accounts.database_for_account(1);history.write_bytes(b'preserved actor history')
        number=c.connect(slot['account'],111)
        self.assertEqual(number,1)
        self.assertEqual(history.read_bytes(),b'preserved actor history')
        self.assertEqual(accounts.profile_for_account(1),self.root/'.vk-local-profile-3')
        self.assertEqual(c.visible_numbers(),[1])
        self.assertEqual(accounts.account_record(3)['redirect_account'],1)
        self.assertEqual(len(accounts.load_bindings(accounts.BINDINGS_PATH)['accounts']),2)
        self.assertEqual(accounts.account_record(1)['name'],'Мой VK')

    def test_changed_vk_creates_different_actor_without_rebinding_previous_history(self):
        c=self.flexible();slot=accounts.add_account('Первый')
        c.connect(slot['account'],333)
        history=accounts.database_for_account(3);history.write_bytes(b'old history')
        other=c.connect(3,444)
        self.assertEqual(other,4)
        self.assertEqual(history.read_bytes(),b'old history')
        self.assertEqual(accounts.load_bindings(accounts.BINDINGS_PATH)['accounts']['3']['vk_id'],333)
        self.assertEqual(accounts.load_bindings(accounts.BINDINGS_PATH)['accounts']['4']['vk_id'],444)
        self.assertEqual(accounts.account_record(4)['policy']['likes'],0)
        self.assertEqual(c.visible_numbers(),[4])

    def test_enabled_existing_actor_cannot_be_replaced(self):
        c=self.flexible();slot=accounts.add_account('Мой VK');c.connect(3,111)
        accounts.configure_account(1,enabled=True)
        another=accounts.add_account('Ещё')
        before=accounts.REGISTRY_PATH.read_bytes()
        with self.assertRaises(accounts.AccountError):c.connect(another['account'],111)
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)

    def test_archived_actor_cannot_enable_with_old_positive_proof(self):
        import vk_authentication as auth
        auth.observe(1,111)
        self.flexible()
        with self.assertRaises(accounts.AccountError):accounts.configure_account(1,enabled=True)

    def test_profile_pointer_rejects_path_escape(self):
        c=self.flexible();slot=accounts.add_account('Мой')
        registry=accounts.load_registry();registry['accounts']['3']['profile_dir']='../outside'
        accounts.atomic_json(accounts.REGISTRY_PATH,registry)
        with self.assertRaises(accounts.AccountError):accounts.profile_for_account(3)

    def test_connected_alias_uses_same_physical_profile_lock(self):
        c=self.flexible();accounts.add_account('Мой');c.connect(3,111)
        with accounts.exclusive_profile(1):
            with self.assertRaises(accounts.AccountError):
                with accounts.exclusive_profile(3):pass

    def test_archived_policies_do_not_consume_future_schedule_capacity(self):
        self.flexible()
        import vk_schedule_policy as policy
        effective=accounts.effective_policy(policy.validate_config())
        self.assertEqual(effective['accounts']['1']['likes'],0)
        self.assertEqual(accounts.account_record(1)['policy']['likes'],50)

    def test_vm_login_autosaves_chosen_vk_and_reuses_its_history_actor(self):
        c=self.flexible();accounts.add_account('Свободный выбор')
        import vk_control as control
        session='f'*32
        accounts.atomic_json(control.login_path(3),{'session':session,'state':'opening'})
        fake=MagicMock();context=fake.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.new_page.return_value.goto.return_value.status=200
        clock=iter(range(0,100000,300))
        with patch('playwright.sync_api.sync_playwright',return_value=fake), \
             patch.object(accounts,'inspect_context',return_value=111), \
             patch.object(control.time,'monotonic',side_effect=lambda:next(clock)):
            result=control.login_worker(3,session)
        self.assertEqual(result,0)
        saved=json.loads(control.login_path(3).read_text())
        self.assertEqual(saved['state'],'complete')
        self.assertEqual(saved['canonical_account'],1)
        self.assertEqual(c.visible_numbers(),[1])
        context.close.assert_called_once()
        fake.__enter__.return_value.chromium.launch_persistent_context.assert_called_once()

    def test_busy_worker_defers_login_then_opens_without_stopping_it(self):
        self.flexible();accounts.add_account('Новый')
        import vk_control as control
        import vk_activity
        import vk_unified as app
        session='b'*32
        accounts.atomic_json(control.login_path(3),{'session':session,'state':'opening'})
        states=[]
        original=accounts.atomic_json
        def save(path,value):
            if path==control.login_path(3):states.append(value['state'])
            original(path,value)
        fake=MagicMock();context=fake.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.new_page.return_value.goto.return_value.status=200
        clock=iter(range(0,100000,300))
        with patch('playwright.sync_api.sync_playwright',return_value=fake), \
             patch.object(accounts,'inspect_context',return_value=333), \
             patch.object(vk_activity,'shared_activity',side_effect=[app.RunAlreadyActive('busy'),nullcontext()]), \
             patch.object(accounts,'atomic_json',side_effect=save),patch.object(control.time,'sleep'), \
             patch.object(control.time,'monotonic',side_effect=lambda:next(clock)):
            result=control.login_worker(3,session)
        self.assertEqual(result,0)
        self.assertIn('waiting_browser',states)
        self.assertEqual(states[-1],'complete')

    def test_active_target_profile_rejects_duplicate_connection_before_remap(self):
        c=self.flexible();accounts.add_account('Первый');c.connect(3,111)
        second=accounts.add_account('Второе окно')
        before=accounts.REGISTRY_PATH.read_bytes()
        with accounts.exclusive_profile(1):
            with self.assertRaises(accounts.AccountError):c.connect(second['account'],111)
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)

    def test_stale_browser_observation_cannot_follow_changed_profile_pointer(self):
        c=self.flexible();accounts.add_account('Первый');c.connect(3,111)
        original=accounts.profile_for_account(1)
        second=accounts.add_account('Второе окно');c.connect(second['account'],111)
        before=accounts.REGISTRY_PATH.read_bytes()
        with self.assertRaises(accounts.AccountError):c.connect(1,222,profile=original)
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)

    def test_reconnecting_history_preflights_preserved_quota_before_publish(self):
        c=self.flexible()
        for identity in (333,444,555):
            slot=accounts.add_account('Новый');number=c.connect(slot['account'],identity)
            accounts.configure_account(number,likes=50,personal_reposts=10,community_reposts=20,communities=[501,502])
        slot=accounts.add_account('Старый VK')
        before=accounts.REGISTRY_PATH.read_bytes()
        with self.assertRaises((accounts.AccountError,ValueError)):c.connect(slot['account'],111)
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)


if __name__=='__main__':unittest.main()
