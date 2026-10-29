"""No network, profile or actual operating-system tasks in these tests."""
import tempfile
import unittest
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock,patch

import vk_batch as engine
import vk_api_read as api
import vk_groups as groups
import vk_scheduler_state as state_module


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.paths={1:self.root/'one.sqlite3',2:self.root/'two.sqlite3'}
        self.now=datetime(2026,10,22,5,0,tzinfo=timezone.utc)
        for path in self.paths.values():
            with closing(engine.db_open(path)) as db:
                api.init_api(db);groups.init_dates(db)
        self.state=state_module.State(self.root/'scheduler.sqlite3',self.paths);self.addCleanup(self.state.close)
        self.cfg={'groups':[{'id':101,'enabled':True,'like_enabled':True,'repost_enabled':True}]}

    def worker(self):
        import vk_scheduler as s
        worker=s.Worker(self.state,self.paths[1],self.cfg,clock=lambda:self.now)
        # These runner fixtures intentionally isolate orchestration from API.
        worker.refresh_batch=Mock(return_value={'state':'ready','pool':None,'report':{}})
        return worker

    def test_night_tick_does_not_scan_or_execute(self):
        self.now=datetime(2026,10,22,16,tzinfo=timezone.utc)
        worker=self.worker()
        with patch.object(worker,'scan_step') as scan,patch('vk_scheduler.actions.execute_action') as execute:
            result=worker.tick(execute=True)
        self.assertEqual(result['phase'],'waiting_window');scan.assert_not_called();execute.assert_not_called()

    def test_proven_vk_skip_does_not_consume_quota_or_prevent_four_real_attempts(self):
        worker=self.worker();n=0
        def candidate(now):
            nonlocal n
            n+=1;return (1,'like',0,{'group_id':101,'post_id':n,'published_at':int(now.timestamp())-1})
        def execute(account,kind,row,*,before_attempt,record_attempt,**kw):
            if row['post_id']==1:
                with closing(engine.db_open(self.paths[1])) as db:engine.add_posts(db,101,[1])
                return {'status':'skipped_vk','attempt_id':None,'skip_evidence':{
                    'reason':'vk_like_denied','permission':'likes.can_like','value':False,'observations':2}}
            before_attempt()
            with closing(engine.db_open(self.paths[1])) as db:
                aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",(row['post_id'],self.now.isoformat())).lastrowid;db.commit()
            record_attempt(aid);return {'status':'verified','attempt_id':aid}
        with patch.object(worker,'pick',side_effect=candidate),patch('vk_scheduler.actions.execute_action',side_effect=execute):
            result=worker.run_batch(3)
        self.assertEqual(result['attempted'],4)
        self.assertFalse(self.state.blocked())
        self.assertEqual(self.state.remaining(self.now)[(1,'like',0)],46)
        first=self.state.db.execute('SELECT status,reason,attempt_id FROM scheduler_jobs ORDER BY id LIMIT 1').fetchone()
        self.assertEqual(tuple(first),('failed','vk_like_denied',None))
        evidence=self.state.db.execute('SELECT snapshot FROM scheduler_skip_evidence ORDER BY job_id LIMIT 1').fetchone()
        import json
        self.assertEqual(json.loads(evidence[0])['permission'],'likes.can_like')

    def test_skip_result_after_reservation_is_uncertain_and_stops_the_batch(self):
        worker=self.worker()
        def invalid(account,kind,row,*,before_attempt,**kw):
            before_attempt()
            return {'status':'skipped_vk','attempt_id':None,'skip_evidence':{'reason':'vk_like_denied','permission':'likes.can_like','value':False,'observations':2}}
        choice=(1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        with patch.object(worker,'pick',return_value=choice),patch('vk_scheduler.actions.execute_action',side_effect=invalid):
            result=worker.run_batch(3)
        self.assertEqual(result['phase'],'attention');self.assertTrue(self.state.blocked())
        self.assertEqual(self.state.usage(self.now)[(1,'like',0)],1)
        self.assertEqual(self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'uncertain')

    def test_dry_monitor_reads_live_catalog_and_queue_but_never_claims(self):
        worker=self.worker()
        with patch.object(worker,'scan_step',return_value={'selected':57}) as scan,\
             patch.object(worker,'queue_snapshot',return_value={'new':12}) as queue,\
             patch('vk_scheduler.actions.execute_action',side_effect=AssertionError('forbidden')):
            result=worker.tick(execute=False,monitor=True)
        scan.assert_called_once();queue.assert_called_once()
        self.assertEqual(result['phase'],'dry_run_monitoring')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)

    def test_batch_executes_at_most_four_single_actions_and_never_reclaims(self):
        from tests.batch_fixtures import save_plan
        worker=self.worker();save_plan(self.state,self.now,slot=3,minutes=15)
        selected=[]
        def candidate(now):
            n=len(selected)+1
            return (1,'like',0,{'group_id':101,'post_id':n,'published_at':int(now.timestamp())-1})
        def execute(account,kind,row,*,before_attempt,record_attempt,**kwargs):
            selected.append(kind);before_attempt()
            with closing(engine.db_open(self.paths[account])) as db:
                cur=db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                    (101,row['post_id'],'like',self.now.isoformat()));db.commit()
            record_attempt(cur.lastrowid)
            return {'status':'verified','attempt_id':cur.lastrowid}
        with patch.object(worker,'pick',side_effect=candidate),patch('vk_scheduler.actions.execute_action',side_effect=execute):
            self.assertEqual(worker.run_batch(3)['attempted'],4)
            self.assertEqual(worker.run_batch(3)['attempted'],0)
        self.assertEqual(selected,['like']*4)

    def test_uncertain_attempt_stops_batch_without_second_action(self):
        worker=self.worker()
        def unknown(account,kind,row,*,before_attempt,record_attempt,**kwargs):
            before_attempt();raise engine.StopRun('unknown')
        candidate=(1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        with patch.object(worker,'pick',return_value=candidate),patch('vk_scheduler.actions.execute_action',side_effect=unknown) as execute:
            result=worker.run_batch(1)
        self.assertEqual(execute.call_count,1);self.assertTrue(self.state.blocked())
        self.assertEqual(result['phase'],'attention')

    def test_preview_does_not_claim_or_execute(self):
        worker=self.worker()
        with patch.object(worker,'scan_step') as scan,patch('vk_scheduler.actions.execute_action') as execute:
            result=worker.tick(execute=False)
        scan.assert_not_called();execute.assert_not_called()
        self.assertEqual(result['phase'],'preview')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)

    def test_confirmation_failure_persists_safe_code_without_private_exception_text(self):
        import vk_scheduler as scheduler
        import vk_scheduled_action as actions
        failure=actions.ScheduledActionStopped('community_confirm','PRIVATE-DOM-SECRET')
        worker=self.worker()
        def unknown(account,kind,row,*,before_attempt,**kwargs):
            before_attempt()
            raise failure
        candidate=(2,'community',242479058,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        with patch.object(worker,'pick',return_value=candidate),patch.object(actions,'execute_action',side_effect=unknown) as execute:
            result=worker.run_batch(1)
        execute.assert_called_once()
        self.assertEqual(result['phase'],'attention')
        self.assertEqual(self.state.get('paused'),'action_stopped_community_confirm')
        saved=self.state.db.execute('SELECT status,reason FROM scheduler_jobs').fetchone()
        self.assertEqual(tuple(saved),('uncertain','action_stopped_community_confirm'))
        self.assertEqual(scheduler.safe_failure_reason(engine.StopRun('PRIVATE-DOM-SECRET')),'StopRun')
        self.assertEqual(scheduler.safe_failure_reason(actions.community.ui.CommunityConfirmationError()),'community_confirmation_timeout')
        failure.reason_code='PRIVATE-DOM-SECRET'
        self.assertEqual(scheduler.safe_failure_reason(failure),'ScheduledActionStopped')

    def test_action_failure_retains_stage_cause_and_attempt_state_without_exception_messages(self):
        worker=self.worker()
        def fail(*args,**kwargs):
            try:raise ValueError('INNER_SECRET')
            except ValueError as cause:
                import vk_scheduled_action as action
                raise action.ScheduledActionStopped('personal_action','OUTER_SECRET') from cause
        candidate=(1,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        with patch.object(worker,'pick',return_value=candidate),patch('vk_scheduler.actions.execute_action',side_effect=fail):
            result=worker.run_batch(1)
        self.assertEqual(result['phase'],'attention')
        record=self.state.get('last_diagnostic_error')
        self.assertEqual(record['job'],1);self.assertEqual(record['stage'],'personal_action')
        self.assertEqual(record['outcome'],'failed');self.assertIsNone(record.get('attempt_id'))
        self.assertEqual(record['error_chain'][1]['type'],'ValueError')
        import json
        self.assertNotIn('SECRET',json.dumps(record))

    def test_like_confirmation_failure_keeps_uncertain_and_boolean_evidence(self):
        worker=self.worker()
        failure=engine.ActionConfirmationError('like',{'liked':False,'reposted':False,'main_post_liked':False,'secret':'PRIVATE'})
        def unknown(account,kind,row,*,before_attempt,**kw):before_attempt();raise failure
        candidate=(2,'like',0,{'group_id':101,'post_id':11,'published_at':int(self.now.timestamp())})
        with patch.object(worker,'pick',return_value=candidate),patch('vk_scheduler.actions.execute_action',side_effect=unknown):
            result=worker.run_batch(1)
        self.assertEqual(result['phase'],'attention');self.assertEqual(self.state.get('paused'),'like_confirmation_timeout')
        self.assertEqual(self.state.get('last_action_confirmation')['observation'],{'liked':False,'reposted':False,'main_post_liked':False})
        self.assertEqual(self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'uncertain')

    def test_stop_received_after_first_action_prevents_remaining_three(self):
        worker=self.worker();selected=[]
        def pick(now):
            return (1,'like',0,{'group_id':101,'post_id':len(selected)+1,'published_at':int(now.timestamp())})
        def execute(account,kind,row,*,before_attempt,record_attempt,**kwargs):
            selected.append(row['post_id']);before_attempt()
            with closing(engine.db_open(self.paths[account])) as db:
                cur=db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,?,?)',
                    (row['post_id'],'like',self.now.isoformat()));db.commit()
            record_attempt(cur.lastrowid);self.state.set('stop_requested',True)
            return {'status':'verified','attempt_id':cur.lastrowid}
        with patch.object(worker,'pick',side_effect=pick),patch('vk_scheduler.actions.execute_action',side_effect=execute):
            self.assertEqual(worker.run_batch(1)['attempted'],1)
        self.assertEqual(selected,[1])

    def test_batch_refresh_happens_before_first_selection(self):
        worker=self.worker();events=[]
        worker.refresh_batch.side_effect=lambda:(events.append('refresh') or {'state':'ready','pool':set(),'report':{}})
        with patch.object(worker,'pick',side_effect=lambda now:(events.append('pick') or None)):
            worker.run_batch(4)
        self.assertEqual(events,['refresh','pick'])

    def test_refresh_budget_shortage_never_uses_old_queue_or_creates_job(self):
        worker=self.worker();worker.refresh_batch.return_value={'state':'budget','pool':set(),'report':{}}
        with patch.object(worker,'pick') as pick,patch('vk_scheduler.actions.execute_action') as execute:
            result=worker.run_batch(4)
        pick.assert_not_called();execute.assert_not_called()
        self.assertEqual(result['phase'],'counter_refresh_budget')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)

    def test_real_pick_restricts_to_fresh_pool_and_records_three_counter_minimum(self):
        worker=self.worker()
        worker._batch_pool={(101,12)}
        rows=[dict(group_id=101,post_id=11,published_at=int(self.now.timestamp())-1,like_count=0,repost_count=0,comment_count=0),
              dict(group_id=101,post_id=12,published_at=int(self.now.timestamp())-2,like_count=1,repost_count=0,comment_count=4)]
        with patch.object(self.state,'candidates',return_value=rows):
            result=worker.pick(self.now)
        self.assertEqual(result[3]['post_id'],12)
        self.assertEqual(result[3]['selection_minimum'],[0,0,5])
        rows[1]['comment_count']=None
        with patch.object(self.state,'candidates',return_value=rows):
            self.assertIsNone(worker.pick(self.now))

    def test_variable_refresh_duration_cannot_compress_actual_action_batches(self):
        worker=self.worker();selected=[];calls=[]
        def prepare():
            if not calls:self.now=self.now.replace(minute=14)
            calls.append('refresh')
            return {'state':'ready','pool':None,'report':{}}
        worker.refresh_batch.side_effect=prepare
        def pick(now):return (1,'like',0,{'group_id':101,'post_id':len(selected)+1,'published_at':int(now.timestamp())})
        def execute(account,kind,row,*,before_attempt,record_attempt,**kw):
            before_attempt();selected.append(self.now)
            with closing(engine.db_open(self.paths[account])) as db:
                cur=db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                    (101,row['post_id'],'like',self.now.isoformat()));db.commit()
            record_attempt(cur.lastrowid)
            return {'status':'verified','attempt_id':cur.lastrowid}
        with patch.object(worker,'pick',side_effect=pick),patch('vk_scheduler.actions.execute_action',side_effect=execute):
            worker.run_batch(0)
            self.now=self.now.replace(minute=15)
            result=worker.run_batch(1)
        self.assertEqual(len(selected),4)
        self.assertEqual(result['phase'],'batch_already_claimed')


if __name__=='__main__':unittest.main()
