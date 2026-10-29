"""Timing and no-send recovery; temporary ledgers and fake transports only."""
import io,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from contextlib import closing,redirect_stdout
from unittest.mock import Mock,patch
import vk_batch as engine
import vk_api_read as api
import vk_groups as groups
import vk_scheduler_state as ledger
import vk_scheduler as scheduler


class ThroughputTests(unittest.TestCase):
    def test_approved_api_budget_accepts_5000_without_changing_action_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'automation.json'
            path.write_text(json.dumps({'scan':{'daily_requests':2000,'pause_seconds':5}}))
            old_policy,old_scan=scheduler.load_settings(path)
            path.write_text(json.dumps({'scan':{'daily_requests':5000,'pause_seconds':5}}))
            new_policy,new_scan=scheduler.load_settings(path)
            self.assertEqual(new_policy,old_policy)
            self.assertEqual(new_scan,{**old_scan,'daily_requests':5000})
            for invalid in (5001,True,0):
                path.write_text(json.dumps({'scan':{'daily_requests':invalid}}))
                with self.assertRaises(ValueError):scheduler.load_settings(path)
    def test_approved_five_second_api_setting_preserves_action_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'automation.json';path.write_text(json.dumps({'scan':{'pause_seconds':5}}))
            policy,scan=scheduler.load_settings(path)
        self.assertEqual(scan['pause_seconds'],5)
        self.assertEqual((policy['start_hour'],policy['end_hour'],policy['batch_size']),(9,19,4))
        self.assertEqual(policy['accounts']['1']['likes'],50)

    def test_catalog_accepts_five_seconds_and_transient_failure_does_not_poison_group(self):
        import vk_catalog_scan as catalog
        with tempfile.TemporaryDirectory() as temp,closing(engine.db_open(Path(temp)/'actor.sqlite3')) as db:
            cycle=catalog.start_cycle(db,[{'id':101}],days=8,max_pages_per_group=10)
            def fail(params):raise api.VkApiTransientRead('temporary')
            with self.assertRaises(api.VkApiTransientRead):
                catalog.step(db,'fake',cycle,pause_seconds=5,transport=fail,sleeper=lambda _:None)
            row=db.execute('SELECT state,pages,next_offset FROM catalog_cycle_groups WHERE cycle_id=?',(cycle,)).fetchone()
            self.assertEqual(tuple(row),('pending',0,0))
            self.assertEqual(db.execute('SELECT state FROM catalog_cycles').fetchone()[0],'running')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],1)
            payload={'response':{'count':0,'items':[]}}
            result=catalog.step(db,'fake',cycle,pause_seconds=5,transport=lambda p:(200,payload),sleeper=lambda _:None)
            self.assertEqual(result['status'],'complete')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.paths={a:self.root/f'{a}.sqlite3' for a in (1,2)};self.now=datetime(2026,10,22,5,tzinfo=timezone.utc)
        for path in self.paths.values():
            with closing(engine.db_open(path)) as db:api.init_api(db);groups.init_dates(db);engine.add_posts(db,101,[11,12,13])
        self.state=ledger.State(self.root/'scheduler.sqlite3',self.paths);self.addCleanup(self.state.close)
        self.worker=scheduler.Worker(self.state,self.paths[1],{'groups':[{'id':101,'enabled':True}]},clock=lambda:self.now)
        self.worker.refresh_batch=Mock(return_value={'state':'ready','pool':None,'report':{}})
        self.silent=redirect_stdout(io.StringIO());self.silent.__enter__();self.addCleanup(self.silent.__exit__,None,None,None)
    def selected(self,pid=11):return (1,'like',0,{'group_id':101,'post_id':pid,'published_at':int(self.now.timestamp())})
    def run_failure(self,exc,slot=1,pid=11):
        from tests.batch_fixtures import save_plan
        if not self.state.db.execute('SELECT 1 FROM schedule_days').fetchone():save_plan(self.state,self.now)
        with patch.object(self.worker,'pick',return_value=self.selected(pid)),patch.object(scheduler.actions,'execute_action',side_effect=exc):
            return self.worker.run_batch(slot)
    def test_pre_send_os_error_releases_batch_without_resetting_history_or_reopening_job(self):
        result=self.run_failure(OSError('SECRET'))
        self.assertEqual(result['phase'],'technical_error_deferred')
        self.assertFalse(self.state.blocked());self.assertIsNone(self.worker._batch_pool)
        row=self.state.db.execute('SELECT status,attempt_id FROM scheduler_jobs').fetchone()
        self.assertEqual(tuple(row),('failed',None));self.assertEqual(sum(self.state.usage(self.now).values()),0)
        self.assertGreater(self.state.get('technical_backoff_until'),self.now.timestamp())
        with patch.object(self.worker,'scan_step') as scan,patch.object(self.worker,'run_batch') as batch:
            self.assertEqual(self.worker.tick(execute=True)['phase'],'technical_error_backoff')
        scan.assert_not_called();batch.assert_not_called()
    def test_actual_guard_and_unknown_result_remain_blocking(self):
        self.run_failure(engine.StopRun('profile guard'))
        self.assertTrue(self.state.blocked());self.assertEqual(self.state.get('technical_failures',{}),{})
    def test_incomplete_or_cyclic_exception_chain_never_recovers(self):
        error=engine.PlatformRateLimited('guard')
        for _ in range(9):
            outer=OSError('wrapper');outer.__cause__=error;error=outer
        self.assertFalse(scheduler.recovery.is_technical(error,'action'))
        cyclic=OSError('cycle');cyclic.__cause__=cyclic
        self.assertFalse(scheduler.recovery.is_technical(cyclic,'action'))
    def test_raw_cleanup_os_error_stays_blocking(self):
        error=OSError('close failure');error.stage='cleanup'
        self.assertEqual(self.run_failure(error)['phase'],'attention')
        self.assertTrue(self.state.blocked());self.assertEqual(self.state.get('technical_failures',{}),{})
    def test_raw_http_errors_never_recover_as_action_os_errors(self):
        from urllib.error import HTTPError
        for status in (403,429,503):
            error=HTTPError('https://invalid',status,'fixed',{},None)
            try:self.assertFalse(scheduler.recovery.is_technical(error,'action'))
            finally:error.close()
    def test_real_transport_conversion_keeps_503_read_recovery_and_rejects_vk_limits(self):
        from urllib.error import HTTPError
        for status,expected in ((403,False),(429,False),(500,True),(502,True),(503,True),(504,True)):
            error=HTTPError('https://invalid',status,'fixed',{},None)
            try:
                with patch('urllib.request.urlopen',side_effect=error):
                    try:api._default_transport({'access_token':'offline'})
                    except api.VkApiFailure as converted:
                        self.assertEqual(scheduler.recovery.is_technical(converted,'read'),expected)
            finally:error.close()
    def test_two_explicit_isolations_allow_only_their_exact_uncertain_attempts(self):
        for pid in (11,12):
            with closing(engine.db_open(self.paths[2])) as db:
                cur=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",(pid,self.now.isoformat()))
                attempt=cur.lastrowid
                db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=101 AND post_id=?",(pid,));db.commit()
            # Reuse the durable scheduler schema without sending an action.
            columns=[r[1] for r in self.state.db.execute('PRAGMA table_info(scheduler_jobs)')]
            values={'id':pid,'day':'2026-10-22','slot':pid,'account':2,'kind':'like','community_id':0,'group_id':101,'post_id':pid,
                    'status':'uncertain','attempt_id':attempt,'baseline':attempt-1,'reason':'like_confirmation_timeout',
                    'created_at':self.now.isoformat(),'updated_at':self.now.isoformat()}
            self.state.db.execute('INSERT INTO scheduler_jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[values.get(c,self.now.isoformat()) for c in columns]);self.state.db.commit()
            self.state.isolate_uncertain_like(pid,self.now,authorization='explicit_user_post_isolation')
        self.assertTrue(scheduler.recovery.safe_to_defer(self.state))
        self.state.db.execute("UPDATE scheduler_jobs SET reason='different' WHERE id=12");self.state.db.commit()
        self.assertFalse(scheduler.recovery.safe_to_defer(self.state))
    def test_verified_action_resets_only_action_failure_streak(self):
        self.state.set('technical_failures',{'action':2,'read':2})
        self.state.set('last_technical_recovery',{'scope':'action','state':'deferred'})
        self.worker.technical_succeeded('action')
        self.assertEqual(self.state.get('technical_failures'),{'action':0,'read':2})
        self.assertEqual(self.state.get('last_technical_recovery')['state'],'recovered')
    def test_error_after_durable_intent_never_auto_resets_or_retries(self):
        def action(*args,**kw):kw['before_attempt']();raise OSError('after intent')
        with patch.object(self.worker,'pick',return_value=self.selected()),patch.object(scheduler.actions,'execute_action',side_effect=action) as execute:
            self.worker.run_batch(1)
        self.assertEqual(execute.call_count,1);self.assertTrue(self.state.blocked())
        self.assertEqual(self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'uncertain')
        self.assertEqual(sum(self.state.usage(self.now).values()),1)
    def test_unexpected_actor_attempt_prevents_no_send_recovery(self):
        def action(*args,**kw):
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,11,'like',?)",(self.now.isoformat(),));db.commit()
            raise OSError('after untracked attempt')
        with patch.object(self.worker,'pick',return_value=self.selected()),patch.object(scheduler.actions,'execute_action',side_effect=action):self.worker.run_batch(1)
        self.assertTrue(self.state.blocked());self.assertFalse(self.state.get('technical_failures',{}))
    def test_three_consecutive_technical_failures_stop_for_repair(self):
        for index in range(3):
            self.now+=timedelta(minutes=20);self.run_failure(OSError('technical'),slot=index+1,pid=11+index)
        self.assertEqual(self.state.get('paused'),'technical_error_repeated')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],3)
    def test_rate_limit_is_not_recovered(self):
        self.worker.refresh_batch.side_effect=api.VkApiLimited('429',code=6)
        from tests.batch_fixtures import save_plan
        save_plan(self.state,self.now)
        self.assertEqual(self.worker.tick(execute=True)['phase'],'attention');self.assertTrue(self.state.blocked())
    def test_browser_navigation_transport_failure_before_attempt_can_defer(self):
        try:
            try:raise scheduler.actions.PlaywrightError('navigation timeout')
            except scheduler.actions.PlaywrightError as cause:raise scheduler.actions.ScheduledActionStopped('source_navigation','fixed') from cause
        except scheduler.actions.ScheduledActionStopped as error:result=self.run_failure(error)
        self.assertEqual(result['phase'],'technical_error_deferred');self.assertFalse(self.state.blocked())

    def test_cached_or_empty_scan_is_not_evidence_of_read_recovery(self):
        self.state.set('technical_failures',{'read':2})
        with patch.object(scheduler.policy,'due_slot',return_value=None),patch.object(self.state,'day_plan',return_value=[self.now+timedelta(minutes=10)]),patch.object(self.worker,'scan_step',return_value=None) as scan:
            self.worker.tick(execute=True)
        scan.assert_called_once()
        self.assertEqual(self.state.get('technical_failures')['read'],2)

    def test_transient_refresh_preserves_empty_job_history_and_applies_backoff(self):
        self.worker.refresh_batch.side_effect=api.VkApiTransientRead('temporary')
        self.assertEqual(self.worker.run_batch(1)['phase'],'technical_error_deferred')
        self.assertFalse(self.state.blocked());self.assertEqual(self.state.get('technical_failures')['read'],1)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)


if __name__=='__main__':unittest.main()
