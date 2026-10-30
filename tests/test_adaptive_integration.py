"""Real adaptive state/worker integration; all account databases are temporary."""
import copy
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_schedule_policy as policy
import vk_scheduler as scheduler
from vk_adaptive_state import AdaptiveState
from vk_adaptive_worker import AdaptiveWorker


class AdaptiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.paths={a:self.root/f'actor-{a}.sqlite3' for a in (1,2)}
        for path in self.paths.values():
            with closing(engine.db_open(path)) as db:
                api.init_api(db)
        self.now=datetime(2026,10,30,5,tzinfo=timezone.utc)
        self.config=policy.validate_config({'schedule_mode':'adaptive','start_hour':0,'end_hour':24,
            'accounts':{str(a):{'likes':50 if a==1 else 0,'personal_reposts':0,
                'community_reposts':0,'communities':[]} for a in (1,2)}})
        self.enabled={1:True,2:False}
        self.state=self.open_state()
        self.worker=self.open_worker(self.state)
        self.post=0
        self.elapsed=20
        self.after_action=None
        self.executed=[]

    def open_state(self):
        state=AdaptiveState(self.root/'scheduler.sqlite3',self.paths,self.config)
        state.policy_provider=lambda:copy.deepcopy(self.config)
        state.enabled_provider=lambda actor:self.enabled[actor]
        self.addCleanup(state.close)
        return state

    def open_worker(self,state):
        worker=AdaptiveWorker(state,self.paths[1],{'groups':[{'id':101,'enabled':True}]},clock=lambda:self.now)
        worker.refresh_batch=self.refresh
        worker.pick=self.pick
        return worker

    def refresh(self,**kwargs):
        return {'state':'ready','pool':None,'report':{
            'started_at':self.now.isoformat(),'finished_at':self.now.isoformat(),'requests':0}}

    def pick(self,now):
        pending=self.state.remaining(now)
        actor=next((actor for (actor,kind,target),left in pending.items() if left and kind=='like'),None)
        if actor is None:return None
        self.post+=1
        return actor,'like',0,{'group_id':101,'post_id':self.post,'published_at':int(now.timestamp())-1}

    def execute(self,actor,kind,row,*,before_attempt,record_attempt,**kwargs):
        before_attempt()
        with closing(engine.db_open(self.paths[actor])) as db:
            attempt=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",
                (row['post_id'],self.now.isoformat())).lastrowid
            db.commit()
        record_attempt(attempt)
        self.executed.append(actor)
        self.now+=timedelta(seconds=self.elapsed)
        if self.after_action:self.after_action()
        return {'status':'verified','attempt_id':attempt}

    def tick(self):
        with patch.object(scheduler.actions,'execute_action',side_effect=self.execute):
            return self.worker.tick(execute=True)

    def test_actual_slow_finish_then_restart_gates_next_claim_for_540_seconds(self):
        self.elapsed=900
        result=self.tick()
        self.assertEqual(result['phase'],'batch_complete')
        first=dict(self.state.db.execute('SELECT * FROM scheduler_batches').fetchone())
        claim=dict(self.state.db.execute('SELECT * FROM scheduler_adaptive_batches').fetchone())
        self.assertGreater(datetime.fromisoformat(first['finished_at']),datetime.fromisoformat(claim['estimated_end']))
        finished=self.now
        self.state=self.open_state()
        self.worker=self.open_worker(self.state)
        self.now=finished+timedelta(seconds=539)
        self.assertEqual(self.tick()['phase'],'waiting_batch')
        self.now=finished+timedelta(seconds=540)
        self.elapsed=20
        self.tick()
        rows=self.state.db.execute('SELECT started_at,finished_at FROM scheduler_batches ORDER BY slot').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual((datetime.fromisoformat(rows[1]['started_at'])-datetime.fromisoformat(rows[0]['finished_at'])).total_seconds(),540)
        self.assertGreaterEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_adaptive_forecasts').fetchone()[0],2)

    def test_new_actor_is_included_in_next_claim_and_old_spend_is_retained(self):
        self.config['accounts']['1']['likes']=1
        self.tick()
        first_finish=self.now
        self.config['accounts']['2']['likes']=2
        self.enabled[2]=True
        self.now=first_finish+timedelta(seconds=540)
        self.tick()
        claim_forecast=self.state.db.execute('''SELECT f.remaining FROM scheduler_adaptive_batches b
            JOIN scheduler_adaptive_forecasts f ON f.id=b.revision ORDER BY b.slot DESC LIMIT 1''').fetchone()[0]
        self.assertEqual(json.loads(claim_forecast)['1:like:0'],0)
        self.assertEqual(json.loads(claim_forecast)['2:like:0'],2)
        latest=self.state.db.execute('SELECT remaining FROM scheduler_adaptive_forecasts ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertEqual(json.loads(latest)['2:like:0'],1)
        self.assertEqual(self.state.usage(self.now)[(1,'like',0)],1)
        self.assertIn(2,self.executed)

    def test_old_day_safe_completion_does_not_claim_new_day_before_rest(self):
        self.config['accounts']['1']['likes']=1
        midnight=self.state.calendar_end(self.now)
        self.now=midnight-timedelta(seconds=240)
        self.elapsed=300
        self.tick()
        row=self.state.db.execute('SELECT day,finished_at FROM scheduler_batches').fetchone()
        self.assertEqual(row['day'],'2026-10-30')
        self.assertGreater(datetime.fromisoformat(row['finished_at']),midnight)
        finish=self.now
        self.assertEqual(self.tick()['phase'],'waiting_batch')
        self.now=finish+timedelta(seconds=540)
        self.elapsed=20
        self.tick()
        self.assertEqual([r[0] for r in self.state.db.execute('SELECT day FROM scheduler_batches ORDER BY day')],['2026-10-30','2026-10-31'])

    def test_manual_block_and_existing_claim_prevent_any_executor_call(self):
        self.state.ensure_day(self.now)
        self.state.pause('manual_block',kind='manual',now=self.now)
        self.assertEqual(self.tick()['phase'],'attention')
        self.assertEqual(self.executed,[])
        self.assertTrue(self.state.clear_manual_block(now=self.now))
        self.now+=timedelta(seconds=61)
        self.state.update_forecast(self.now,force=True)
        self.assertTrue(self.state.claim_batch(self.now,self.state.next_slot(self.now)))
        result=self.tick()
        self.assertEqual(result['phase'],'batch_already_claimed')
        self.assertEqual(self.executed,[])

    def test_shared_api_budget_is_read_from_source_not_actor_two(self):
        with closing(engine.db_open(self.paths[1])) as db:
            db.executemany('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(101,?)',
                [((self.now-timedelta(seconds=5*i)).isoformat(),) for i in range(5000)])
            db.commit()
        result=self.tick()
        self.assertIn(result['phase'],('waiting_batch','insufficient_capacity'))
        self.assertEqual(self.executed,[])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)

    def test_enabling_an_actor_during_execution_waits_for_next_claim_snapshot(self):
        self.config['accounts']['2']['likes']=50
        self.now=self.state.calendar_end(self.now)-timedelta(minutes=20)
        initial_pick=self.worker.pick
        def prefer_enabled_new_actor(now):
            if not self.enabled[2]:return initial_pick(now)
            self.post+=1
            return 2,'like',0,{'group_id':101,'post_id':self.post,'published_at':int(now.timestamp())-1}
        self.worker.pick=prefer_enabled_new_actor
        self.after_action=lambda:self.enabled.update({2:True})
        self.tick()
        claimed=self.state.db.execute('SELECT target_attempts FROM scheduler_adaptive_batches').fetchone()[0]
        self.assertGreater(claimed,1)
        self.assertNotIn(2,self.executed,'A newly enabled actor may join only the next durable batch snapshot')

    def test_unfinished_job_blocks_claim_even_without_an_active_batch(self):
        self.state.create_job(self.now,99,1,'like',0,
            {'group_id':101,'post_id':100,'published_at':int(self.now.timestamp())-1})
        result=self.tick()
        self.assertEqual(result['phase'],'batch_already_claimed')
        self.assertEqual(self.executed,[])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)

    def test_automatic_previous_day_block_releases_only_to_future_gate(self):
        self.state.ensure_day(self.now)
        self.state.pause('TimeoutError',now=self.now)
        self.now=self.state.calendar_end(self.now)+timedelta(hours=1)
        released_at=self.now
        result=self.tick()
        self.assertEqual(result['phase'],'waiting_batch')
        self.assertIsNone(self.state.block_status())
        self.assertGreater(self.state.get('resume_not_before'),released_at.timestamp()+60)
        self.assertEqual(self.executed,[])
        self.now+=timedelta(seconds=61)
        self.tick()
        self.assertTrue(self.executed)

    def test_uncertain_attempt_remains_spent_and_blocks_after_scheduler_pause_removed(self):
        original_execute=self.execute
        def uncertain(*args,**kwargs):
            original_execute(*args,**kwargs)
            raise engine.StopRun('ambiguous simulated result')
        with patch.object(scheduler.actions,'execute_action',side_effect=uncertain):
            result=self.worker.tick(execute=True)
        self.assertEqual(result['phase'],'attention')
        self.assertEqual(self.state.usage(self.now)[(1,'like',0)],1)
        self.assertEqual(self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'uncertain')
        self.state.set('paused',None)
        self.now+=timedelta(seconds=540)
        self.assertEqual(self.tick()['phase'],'attention')
        self.assertEqual(self.executed,[1])


if __name__=='__main__':unittest.main()
