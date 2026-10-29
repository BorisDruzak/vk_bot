import json
import random
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import vk_accounts as accounts
import vk_schedule_policy as policy
import vk_scheduler_state as ledger


def quota(number):
    return {'likes':50, 'personal_reposts':10, 'community_reposts':20,
            'communities':[number*10+1, number*10+2]}


class ExtendedWindowTests(unittest.TestCase):
    def test_four_equal_accounts_fit_80_unique_spaced_batches_until22(self):
        cfg=policy.validate_config({'end_hour':22,'accounts':{str(n):quota(n) for n in (1,2,5,6)}})
        now=datetime(2026,10,29,6,tzinfo=timezone.utc)
        for seed in range(10):
            slots=policy.build_schedule(now,cfg,rng=random.Random(seed))
            gaps=[(b-a).total_seconds() for a,b in zip(slots,slots[1:])]
            self.assertEqual(len(slots),80)
            self.assertEqual(len(set(gaps)),79)
            self.assertGreaterEqual(min(gaps),540)
            self.assertLessEqual(slots[-1],policy.work_bounds(now,cfg)[1]-timedelta(seconds=600))
        self.assertEqual(policy.daily_total(cfg),320)
        self.assertTrue(policy.in_work_window(datetime(2026,10,29,16,59,tzinfo=timezone.utc),cfg))
        self.assertFalse(policy.in_work_window(datetime(2026,10,29,17,tzinfo=timezone.utc),cfg))

    def test_default_remains19_and_later_than22_rejected(self):
        self.assertEqual(policy.validate_config()['end_hour'],19)
        with self.assertRaises(ValueError):policy.validate_config({'end_hour':23})
        with self.assertRaises(ValueError):
            policy.validate_config({'accounts':{str(n):quota(n) for n in (1,2,5,6)}})

    def test_saved_day_keeps_old_window_and_slots_until_next_day(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        directory=temporary.name
        state=ledger.State(Path(directory)/'scheduler.sqlite3',{})
        self.addCleanup(state.close)
        now=datetime(2026,10,28,6,tzinfo=timezone.utc)
        old=state.day_plan(now)
        before=state.db.execute('SELECT config,slots FROM schedule_days').fetchone()
        future=policy.validate_config({'end_hour':22,'accounts':{str(n):quota(n) for n in (1,2,5,6)}})
        state.policy_provider=lambda:future
        state.refresh_policy(now)
        self.assertEqual(state.config['end_hour'],19)
        self.assertEqual(state.day_plan(now),old)
        self.assertEqual(tuple(state.db.execute('SELECT config,slots FROM schedule_days').fetchone()),tuple(before))
        state.refresh_policy(now+timedelta(days=1))
        self.assertEqual(state.config['end_hour'],22)
        self.assertEqual(len(state.day_plan(now+timedelta(days=1))),80)


class FutureRegistryCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        for name,value in (('ROOT',self.root),('REGISTRY_PATH',self.root/'registry.json'),
                           ('BINDINGS_PATH',self.root/'bindings.json')):
            context=patch.object(accounts,name,value);context.start();self.addCleanup(context.stop)
        registry={'version':1,'account_flow':'add_any','accounts':{
            str(n):{'account':n,'name':f'Actor {n}','enabled':False,'policy':quota(n)} for n in (1,2,5,6)}}
        registry['accounts']['6']['policy']={'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[]}
        accounts.atomic_json(accounts.REGISTRY_PATH,registry)
        self.write_window(22)

    def write_window(self,end):
        (self.root/'vk_automation.json').write_text(json.dumps({'policy':{'end_hour':end}}),encoding='utf8')

    def test_save_uses_configured_window_instead_of_default(self):
        before=accounts.load_registry()
        result=accounts.configure_account(6,**quota(6))
        self.assertEqual(result['policy'],quota(6))
        self.assertFalse(result['enabled'])
        for number in ('1','2','5'):
            self.assertEqual(accounts.load_registry()['accounts'][number],before['accounts'][number])

    def test_short_window_refuses_without_writing(self):
        self.write_window(19)
        before=accounts.REGISTRY_PATH.read_bytes()
        with self.assertRaisesRegex(accounts.AccountError,'^account_schedule_capacity$'):
            accounts.configure_account(6,**quota(6))
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)

    def test_snapshot_reports_saved_today_and_configured_future_separately(self):
        import vk_control
        from contextlib import closing
        now=datetime.now(timezone.utc)
        old=policy.validate_config({'accounts':{str(n):quota(n) for n in (1,2,5)}})
        with closing(ledger.State(self.root/'vk_scheduler.sqlite3',{},old)) as state:
            state.day_plan(now)
        result=vk_control.snapshot()
        self.assertEqual(result['scheduler']['work_window'],{'start_hour':9,'end_hour':19})
        self.assertEqual(result['scheduler']['pending_work_window'],{'start_hour':9,'end_hour':22})

    def test_connection_uses_same_configured_window_for_saved_quota(self):
        import vk_connections
        registry=accounts.load_registry();registry['accounts']['6']['policy']=quota(6)
        accounts.atomic_json(accounts.REGISTRY_PATH,registry)
        target=vk_connections.connect(6,666)
        self.assertEqual(target,6)
        self.assertEqual(accounts.account_record(6)['policy'],quota(6))
        self.assertFalse(accounts.account_enabled(6))

    def test_invalid_window_cannot_be_saved_or_connected(self):
        self.write_window(23)
        before=accounts.REGISTRY_PATH.read_bytes()
        with self.assertRaises(accounts.AccountError):accounts.configure_account(6,**quota(6))
        import vk_connections
        with self.assertRaises(accounts.AccountError):vk_connections.connect(6,666)
        self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)


if __name__=='__main__':unittest.main()
