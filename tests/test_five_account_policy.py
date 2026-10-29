"""Five equal quotas fit future6/67 while every saved4/80 day stays immutable."""
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import vk_accounts as accounts
import vk_schedule_policy as policy
import vk_scheduler_state as ledger
from tests.test_six_batch_policy import quota
from tests import test_scheduler_runner as fixtures


def five_policy(size=6):
    return policy.validate_config({'start_hour':9,'end_hour':22,'batch_size':size,
        'accounts':{str(n):quota(n) for n in (1,2,5,6,7)}})


class FiveAccountPolicyTests(unittest.TestCase):
    def test_five_equal_quotas_have400_attempts_and67_batches_not402_attempts(self):
        cfg=five_policy();before=deepcopy(cfg)
        self.assertEqual(policy.daily_total(cfg),400)
        self.assertEqual(policy.batch_count(cfg),67)
        self.assertEqual(sum(sum(p[k] for k in ('likes','personal_reposts','community_reposts'))
                             for p in cfg['accounts'].values()),400)
        self.assertEqual(cfg,before)

    def test_seeded67_plans_keep_unique540_spacing_and_end_reserve(self):
        cfg=five_policy()
        for seed in range(100):
            with self.subTest(seed=seed):
                slots=policy.build_schedule(date(2026,10,30),cfg,rng=random.Random(seed))
                start,end=policy.work_bounds(slots[0],cfg)
                gaps=[(b-a).total_seconds() for a,b in zip(slots,slots[1:])]
                self.assertEqual(len(slots),67)
                self.assertEqual(len(set(gaps)),66)
                self.assertGreaterEqual(min(gaps),540)
                self.assertGreaterEqual(slots[0],start)
                self.assertLessEqual(slots[-1],end-timedelta(seconds=600))

    def test_five_quotas_correctly_fail_under_old_four_attempt_capacity(self):
        with self.assertRaises(ValueError):
            policy.build_schedule(date(2026,10,30),five_policy(4))

    def test_saving_disabled_fifth_quotas_uses_future_six_without_touching_others(self):
        registry={'version':1,'account_flow':'add_any','accounts':{
            str(n):{'account':n,'name':f'Actor {n}','enabled':n!=7,'policy':quota(n)}
            for n in (1,2,5,6,7)}}
        registry['accounts']['7']['policy']={'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[]}
        before=deepcopy(registry)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'vk_account_registry.json'
            path.write_text(json.dumps(registry),encoding='utf-8')
            (root/'vk_automation.json').write_text(json.dumps({'policy':{'end_hour':22,'batch_size':6}}),encoding='utf-8')
            with patch.object(accounts,'ROOT',root),patch.object(accounts,'REGISTRY_PATH',path):
                row=accounts.configure_account(7,enabled=False,**quota(7))
                after=accounts.load_registry();future=accounts.future_policy(after)
            self.assertFalse(row['enabled'])
            self.assertEqual((policy.daily_total(future),policy.batch_count(future)),(400,67))
            for number in ('1','2','5','6'):
                self.assertEqual(after['accounts'][number],before['accounts'][number])
            self.assertEqual(after['accounts']['7']['policy'],quota(7))
            self.assertEqual(list(root.glob('*.sqlite3')),[])


class FiveAccountSavedDayTests(unittest.TestCase):
    setUp=fixtures.RunnerTests.setUp

    def test_fifth_future_quota_and_restart_preserve_saved_today_and_history(self):
        cfg=five_policy();cfg['accounts'].pop('7');cfg['batch_size']=4
        cfg=policy.validate_config(cfg)
        self.state.config=cfg;today=self.state.day_plan(self.now)
        saved=[tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')]
        future=five_policy();self.state.policy_provider=lambda:future
        self.state.refresh_policy(self.now)
        self.assertEqual((self.state.config['batch_size'],len(self.state.day_plan(self.now))),(4,80))
        self.assertNotIn('7',self.state.config['accounts'])
        self.assertEqual([tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')],saved)
        with closing(ledger.State(self.root/'scheduler.sqlite3',self.paths,future)) as restarted:
            restarted.policy_provider=lambda:future;restarted.refresh_policy(self.now)
            self.assertEqual(restarted.day_plan(self.now),today)
            tomorrow=self.now+timedelta(days=1);restarted.refresh_policy(tomorrow)
            self.assertEqual((restarted.config['batch_size'],len(restarted.day_plan(tomorrow))),(6,67))
            self.assertEqual(restarted.config['accounts']['7'],quota(7))
            self.assertEqual(policy.daily_total(restarted.config),400)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)


if __name__=='__main__':unittest.main()
