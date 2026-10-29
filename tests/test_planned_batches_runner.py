"""Explicit future slot allocations cap both ordinary attempts and retry passes."""
from contextlib import closing
from datetime import timedelta
import unittest
from unittest.mock import patch

import vk_batch as engine
import vk_schedule_policy as policy
from tests.test_five_account_policy import five_policy
from tests import test_scheduler_runner as runner
from tests import test_action_retry_state as retries


def distributed():
    cfg=five_policy();cfg['accounts']['7']['personal_reposts']=0
    cfg['planned_batches']=67
    return policy.validate_config(cfg)


class PlannedBatchRunnerTests(unittest.TestCase):
    setUp=runner.RunnerTests.setUp
    worker=runner.RunnerTests.worker

    def run_slot(self,slot):
        self.state.config=distributed();slots=self.state.day_plan(self.now)
        self.now=slots[slot];worker=self.worker();calls=[]
        def pick(now):
            number=len(calls)+1
            return 1,'like',0,{'group_id':101,'post_id':number,'published_at':int(now.timestamp())-1}
        def execute(account,kind,row,*,before_attempt,record_attempt,**kw):
            calls.append(row['post_id']);before_attempt()
            with closing(engine.db_open(self.paths[1])) as db:
                engine.add_posts(db,101,[row['post_id']])
                aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",
                    (row['post_id'],self.now.isoformat())).lastrowid
                engine.status_write(db,101,row['post_id'],'like','verified','synthetic');db.commit()
            record_attempt(aid);self.now+=timedelta(seconds=20)
            return {'status':'verified','attempt_id':aid}
        with patch.object(worker,'pick',side_effect=pick),patch('vk_scheduler.actions.execute_action',side_effect=execute):
            result=worker.run_batch(slot)
        return result,calls

    def test_five_target_slot_cannot_reserve_sixth(self):
        result,calls=self.run_slot(0)
        self.assertEqual((result['attempted'],len(calls)),(5,5))

    def test_six_target_slot_cannot_reserve_seventh(self):
        result,calls=self.run_slot(1)
        self.assertEqual((result['attempted'],len(calls)),(6,6))


class PlannedBatchRetryTests(unittest.TestCase):
    setUp=retries.RetryStateTests.setUp
    pick=retries.RetryStateTests.pick
    reserve=retries.RetryStateTests.reserve

    def test_retry_after_fifth_in_five_target_slot_waits_without_sixth_reservation(self):
        self.state.config=distributed();slots=self.state.day_plan(self.now);self.now=slots[0]
        def execute(account,kind,row,**kw):
            self.calls.append(row['post_id']);aid=self.reserve(row,kw)
            if row['post_id']==5:raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("UPDATE posts SET like_status='verified' WHERE group_id=101 AND post_id=?",(row['post_id'],));db.commit()
            return {'status':'verified','attempt_id':aid}
        with patch.object(self.worker,'pick',side_effect=self.pick),patch('vk_scheduler.actions.execute_action',side_effect=execute):
            result=self.worker.run_batch(0)
        self.assertEqual((result['attempted'],self.calls),(5,[1,2,3,4,5]))
        from vk_action_retry import RetryLedger
        self.assertEqual(RetryLedger(self.state).job_snapshot(5)['state'],'pending')


if __name__=='__main__':unittest.main()
