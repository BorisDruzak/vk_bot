import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import vk_accounts as accounts
import vk_schedule_policy as policy
import vk_scheduler_state as ledger
import vk_batch as engine
from contextlib import closing


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in [('ROOT', self.root), ('REGISTRY_PATH', self.root/'registry.json'),
                            ('BINDINGS_PATH', self.root/'bindings.json')]:
            context = patch.object(accounts, name, value, create=True)
            context.start(); self.addCleanup(context.stop)

    def test_new_account_separate_disabled_and_zero_quotas(self):
        row = accounts.add_account('Третий')
        self.assertEqual(row['account'], 3)
        self.assertFalse(row['enabled'])
        self.assertEqual(row['policy']['likes'], 0)
        self.assertEqual(accounts.profile_for_account(3), self.root/'.vk-local-profile-3')
        self.assertEqual(accounts.database_for_account(3), self.root/'vk_batch_account_3.sqlite3')
        self.assertFalse(accounts.profile_for_account(3).exists())

    def test_unregistered_and_invalid_ids_rejected(self):
        for number in (0, -1, True, 3, '3'):
            with self.assertRaises((ValueError, accounts.AccountError)):
                accounts.profile_for_account(number)

    def test_enable_requires_binding_and_does_not_reset_ledger(self):
        accounts.add_account('Третий')
        with self.assertRaises(accounts.AccountError):
            accounts.configure_account(3, enabled=True)
        accounts.register_detected_id(self.root/'bindings.json', 3, 333)
        import vk_authentication
        vk_authentication.observe(3,333)
        row = accounts.configure_account(3, enabled=True, likes=5)
        self.assertTrue(row['enabled'])
        self.assertEqual(row['policy']['likes'], 5)
        with self.assertRaises(accounts.AccountError):
            accounts.register_detected_id(self.root/'bindings.json', 1, 333)

    def test_individual_policy_limits_and_destinations(self):
        accounts.add_account('Третий')
        for values in ({'likes':51}, {'likes':True}, {'community_reposts':3,'communities':[5,6]},
                       {'communities':[5,5]}, {'community_reposts':2,'communities':[]}):
            with self.subTest(values=values), self.assertRaises((ValueError,accounts.AccountError)):
                accounts.configure_account(3, **values)

    def test_policy_accepts_additional_account_without_changing_defaults(self):
        cfg = policy.validate_config({'accounts':{'3':{'likes':5,'personal_reposts':0,
                                                     'community_reposts':0,'communities':[]}}})
        self.assertEqual(policy.daily_total(cfg), 165)
        self.assertEqual(cfg['accounts']['1'], policy.DEFAULT_CONFIG['accounts']['1'])

    def test_configuration_errors_explain_destinations_and_capacity_without_writes(self):
        accounts.add_account('Третий')
        before=accounts.REGISTRY_PATH.read_bytes()
        with self.assertRaisesRegex(accounts.AccountError,'^community_destinations_required$'):
            accounts.configure_account(3,community_reposts=20,communities=[])
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)
        accounts.configure_account(3,likes=50,personal_reposts=10,community_reposts=20,communities=[100,200])
        accounts.add_account('Четвёртый')
        before=accounts.REGISTRY_PATH.read_bytes()
        with self.assertRaisesRegex(accounts.AccountError,'^account_schedule_capacity$'):
            accounts.configure_account(4,likes=50,personal_reposts=10,community_reposts=20,communities=[300,400])
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)

    def test_registry_keeps_credentials_out_and_uses_atomic_json(self):
        accounts.add_account('Третий')
        value=json.loads(accounts.REGISTRY_PATH.read_text(encoding='utf-8'))
        self.assertEqual(value['version'],1)
        self.assertEqual(len(accounts.account_numbers()),3)
        self.assertNotIn('password',json.dumps(value))

    def test_concurrent_duplicate_bindings_cannot_both_succeed(self):
        accounts.add_account('Третий');accounts.add_account('Четвёртый')
        barrier=Barrier(2)
        def bind(number):
            barrier.wait()
            try:
                accounts.register_detected_id(self.root/'bindings.json',number,999)
                return True
            except accounts.AccountError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(bind,(3,4)))
        self.assertEqual(sum(results),1)
        rows=accounts.load_bindings(self.root/'bindings.json')['accounts']
        self.assertEqual(len(rows),1)

    def test_enabled_account_cannot_reenter_queue_during_login(self):
        accounts.add_account('Третий')
        accounts.register_detected_id(self.root/'bindings.json',3,333)
        (self.root/'.vk-login-3.json').write_text('{"state":"ready"}',encoding='utf-8')
        with self.assertRaises(accounts.AccountError):accounts.configure_account(3,enabled=True)

    def test_third_account_probe_reads_own_platform_lock(self):
        import vk_scheduler_probe as probe
        accounts.add_account('Третий')
        path=accounts.database_for_account(3)
        with closing(engine.db_open(path)) as db:
            engine.lock_on_platform_warning(db,0,'test_platform_lock')
        with patch.object(probe.smoke,'ACCOUNT2_DB',self.root/'wrong.sqlite3'):
            with self.assertRaises(engine.StopRun):probe._readonly_unlocked(3)
        self.assertFalse((self.root/'wrong.sqlite3').exists())

    def test_live_disabled_guard_and_saved_day_policy_remain(self):
        paths={i:self.root/f'{i}.sqlite3' for i in (1,2)}
        for path in paths.values():
            with closing(engine.db_open(path)): pass
        state=ledger.State(self.root/'scheduler.sqlite3',paths)
        self.addCleanup(state.close)
        now=datetime(2026,10,23,6,tzinfo=timezone.utc)
        slots=state.day_plan(now)
        changed=policy.validate_config({'accounts':{'1':{'likes':20}}})
        state.policy_provider=lambda:changed
        state.enabled_provider=lambda number:number!=1
        state.refresh_policy(now)
        self.assertEqual(state.config['accounts']['1']['likes'],50)
        self.assertEqual(state.day_plan(now),slots)
        self.assertNotIn((1,'like',0),state.remaining(now))
        with self.assertRaises(RuntimeError):state.guard(now,1,'like',0)
        state.refresh_policy(datetime(2026,10,24,6,tzinfo=timezone.utc))
        self.assertEqual(state.config['accounts']['1']['likes'],20)


if __name__ == '__main__': unittest.main()
