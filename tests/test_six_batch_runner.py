"""Six-attempt orchestration on temporary ledgers, retaining saved four policy."""
import copy
from contextlib import closing
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import vk_batch as engine
import vk_schedule_policy as policy
import vk_scheduler_state as ledger
from tests import test_scheduler_runner as runner_fixtures
from tests import test_action_retry_state as retry_fixtures
from tests.batch_fixtures import save_plan


class SixRunnerTests(unittest.TestCase):
    setUp=runner_fixtures.RunnerTests.setUp
    worker=runner_fixtures.RunnerTests.worker

    def run_six(self,*,skips=0,late_sixth=False):
        self.state.config=policy.validate_config({'batch_size':6})
        save_plan(self.state,self.now,slot=1,minutes=20)
        worker=self.worker();sequence=[]
        def pick(now):
            number=len(sequence)+1;sequence.append(number)
            return 1,'like',0,{'group_id':101,'post_id':number,'published_at':int(now.timestamp())-1}
        def execute(account,kind,row,*,before_attempt,record_attempt,**kw):
            pid=row['post_id']
            with closing(engine.db_open(self.paths[1])) as db:engine.add_posts(db,101,[pid])
            if pid<=skips:
                return {'status':'skipped_vk','attempt_id':None,'skip_evidence':{
                    'reason':'vk_like_denied','permission':'likes.can_like','value':False,'observations':2}}
            if late_sixth and pid==6:self.now=worker.batch_boundary(1)-timedelta(seconds=60)
            before_attempt()
            with closing(engine.db_open(self.paths[1])) as db:
                aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",(pid,self.now.isoformat())).lastrowid
                engine.status_write(db,101,pid,'like','verified','synthetic confirmation');db.commit()
            record_attempt(aid);self.now+=timedelta(seconds=20)
            return {'status':'verified','attempt_id':aid}
        with patch.object(worker,'pick',side_effect=pick),patch('vk_scheduler.actions.execute_action',side_effect=execute):
            result=worker.run_batch(1)
        return result,sequence,worker

    def test_six_real_reservations_no_seventh_or_reclaimed_slot(self):
        result,sequence,worker=self.run_six()
        self.assertEqual((result['attempted'],len(sequence)),(6,6))
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],44)
        self.assertEqual(worker.run_batch(1)['attempted'],0)

    def test_six_can_fill_after_five_proven_skips_without_skips_spending_quota(self):
        result,sequence,_=self.run_six(skips=5)
        self.assertEqual(result['attempted'],6)
        self.assertEqual(result['skipped_vk'],5)
        self.assertEqual(len(sequence),11)
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],44)

    def test_sixth_preparation_crossing_intent_cutoff_keeps_five_and_no_new_attempt(self):
        result,sequence,_=self.run_six(late_sixth=True)
        self.assertEqual(result['attempted'],5)
        self.assertEqual(result['phase'],'batch_action_deadline')
        last=self.state.db.execute('SELECT status,attempt_id FROM scheduler_jobs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(last),('failed',None))
        self.assertIsNone(self.state.get('paused'))
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],45)

    def test_existing_four_eighty_day_and_history_survive_future_six_and_restart(self):
        cfg=policy.validate_config({'end_hour':22,'accounts':{str(n):{
            'likes':50,'personal_reposts':10,'community_reposts':20,'communities':[500+n*10,501+n*10]}
            for n in (1,2,3,4)}})
        self.state.config=cfg;today=self.state.day_plan(self.now)
        before=tuple(self.state.db.execute('SELECT config,slots FROM schedule_days').fetchone())
        future=copy.deepcopy(cfg);future['batch_size']=6
        self.state.policy_provider=lambda:future
        self.state.refresh_policy(self.now)
        self.assertEqual(self.state.config['batch_size'],4)
        self.assertEqual(self.state.day_plan(self.now),today)
        tomorrow=self.now+timedelta(days=1)
        self.state.refresh_policy(tomorrow)
        self.assertEqual(len(self.state.day_plan(tomorrow)),54)
        self.assertEqual(self.state.config['accounts'],cfg['accounts'])
        with closing(ledger.State(self.root/'scheduler.sqlite3',self.paths,future)) as restarted:
            restarted.policy_provider=lambda:future;restarted.refresh_policy(self.now)
            self.assertEqual((restarted.config['batch_size'],len(restarted.day_plan(self.now))),(4,80))
            self.assertEqual(tuple(restarted.db.execute('SELECT config,slots FROM schedule_days WHERE day=?',('2026-10-22',)).fetchone()),before)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)


class SixRetryTests(unittest.TestCase):
    setUp=retry_fixtures.RetryStateTests.setUp
    pick=retry_fixtures.RetryStateTests.pick
    reserve=retry_fixtures.RetryStateTests.reserve
    run_fake=retry_fixtures.RetryStateTests.run_fake

    def test_both_retry_passes_spend_six_places_and_daily_quota(self):
        self.state.config=policy.validate_config({'batch_size':6})
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);aid=self.reserve(row,kw)
            if kw.get('recovery_attempt_id') is None:raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
            return {'status':'verified','attempt_id':aid}
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],6)
        self.assertEqual(self.calls,[1,1,2,2,3,3])
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],44)

    def test_pending_retry_after_sixth_reservation_waits_for_next_regular_batch(self):
        self.state.config=policy.validate_config({'batch_size':6})
        def execute(a,k,row,**kw):
            self.calls.append(row['post_id']);aid=self.reserve(row,kw)
            if row['post_id']==6 and kw.get('recovery_attempt_id') is None:raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
            return {'status':'verified','attempt_id':aid}
        result=self.run_fake(execute)
        self.assertEqual(result['attempted'],6)
        self.assertEqual(self.calls,[1,2,3,4,5,6])
        from vk_action_retry import RetryLedger
        self.assertEqual(RetryLedger(self.state).job_snapshot(6)['state'],'pending')
        self.assertFalse(self.state.blocked())
