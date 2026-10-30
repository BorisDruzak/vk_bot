"""Six-place future schedules keep quotas and historical four-place plans."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import vk_accounts as accounts
import vk_schedule_policy as policy


def quota(number):
    return {'likes':50,'personal_reposts':10,'community_reposts':20,
            'communities':[number*10+1,number*10+2]}


class SixBatchPolicyTests(unittest.TestCase):
    def four_account_policy(self):
        return {'start_hour':9,'end_hour':22,'batch_size':6,
                'accounts':{str(number):quota(number) for number in (1,2,5,6)}}

    def test_six_place_policy_preserves_four_equal_quotas(self):
        source=self.four_account_policy();before=deepcopy(source)
        config=policy.validate_config(source)
        self.assertEqual(policy.daily_total(config),320)
        self.assertEqual(policy.batch_count(config),54)
        self.assertEqual(config['accounts'],before['accounts'])
        self.assertEqual(source,before)

    def test_batch_capacity_above_six_and_noninteger_values_are_rejected(self):
        for value in (7,True,False,6.0,'6',0):
            with self.subTest(value=value),self.assertRaises(ValueError):
                policy.validate_config({'batch_size':value})

    def test_generated_54_starts_have_unique_safe_gaps_and_finish_reserve(self):
        config=self.four_account_policy()
        start=datetime(2026,10,30,4,tzinfo=timezone.utc)
        end=datetime(2026,10,30,17,tzinfo=timezone.utc)
        for seed in range(100):
            with self.subTest(seed=seed):
                slots=policy.build_schedule(date(2026,10,30),config,rng=random.Random(seed))
                gaps=[int((later-earlier).total_seconds()) for earlier,later in zip(slots,slots[1:])]
                self.assertEqual(len(slots),54)
                self.assertEqual(slots,sorted(set(slots)))
                self.assertEqual(len(set(gaps)),53)
                self.assertGreaterEqual(min(gaps),540)
                self.assertGreaterEqual(slots[0],start)
                self.assertLessEqual(slots[-1],end-timedelta(seconds=600))
                self.assertEqual(policy.due_slot(slots[30],slots,config=config),30)
                self.assertIsNone(policy.due_slot(slots[30]+timedelta(seconds=121),slots,config=config))

    def test_six_place_base_template_derives_27_batches_without_adding_actors(self):
        config=policy.validate_config({'end_hour':22,'batch_size':6})
        self.assertEqual(policy.daily_total(config),160)
        self.assertEqual(policy.batch_count(config),27)
        self.assertEqual(set(config['accounts']),{'1','2'})

    def test_historical_default_and_stored_four_place_policy_remain_valid(self):
        default=policy.validate_config()
        self.assertEqual((default['batch_size'],default['end_hour']),(4,19))
        self.assertEqual(policy.batch_count(default),40)
        stored=self.four_account_policy();stored['batch_size']=4
        restored=policy.validate_config(json.loads(json.dumps(stored)))
        self.assertEqual(restored,policy.validate_config(stored))
        self.assertEqual(policy.daily_total(restored),320)
        self.assertEqual(policy.batch_count(restored),80)
        self.assertEqual(len(policy.build_schedule(date(2026,10,29),restored,rng=random.Random(2))),80)

    def test_configured_template_opts_into_adaptive_day_without_fixed_count(self):
        configured=json.loads((Path(__file__).resolve().parents[1]/'vk_automation.json').read_text(encoding='utf-8'))
        config=policy.validate_config(configured['policy'])
        self.assertEqual((config['start_hour'],config['end_hour']),(0,24))
        self.assertEqual(policy.daily_total(config),160)
        self.assertEqual(config['schedule_mode'],'adaptive')
        self.assertEqual(config['batch_rest_seconds'],540)
        self.assertNotIn('planned_batches',config)

    def test_registry_four_actor_overlay_derives_54_without_mutating_quotas(self):
        configured=json.loads((Path(__file__).resolve().parents[1]/'vk_automation.json').read_text(encoding='utf-8'))
        configured['policy'].pop('planned_batches',None)
        configured['policy'].update(schedule_mode='fixed',start_hour=9,end_hour=22)
        configured['policy'].pop('batch_rest_seconds',None)
        registry={'version':1,'account_flow':'add_any','accounts':{
            str(number):{'account':number,'name':f'Actor {number}','enabled':True,'policy':quota(number)}
            for number in (1,2,5,6)}}
        before=deepcopy(registry)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'vk_automation.json').write_text(json.dumps(configured),encoding='utf-8')
            with patch.object(accounts,'ROOT',root):
                config=accounts.future_policy(registry)
        self.assertEqual(policy.daily_total(config),320)
        self.assertEqual(policy.batch_count(config),54)
        self.assertEqual(config['accounts'],{str(number):quota(number) for number in (1,2,5,6)})
        self.assertEqual(registry,before)


if __name__=='__main__':unittest.main()
