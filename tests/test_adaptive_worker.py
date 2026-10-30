"""Adaptive dispatch uses temp actor ledgers and simulated browser actions only."""
import io
import json
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import vk_api_read as api
import vk_batch as engine
import vk_groups as groups
import vk_schedule_policy as policy
import vk_scheduler as scheduler
import vk_scheduler_state as ledger
from vk_adaptive_worker import AdaptiveWorker


class BoundaryState(ledger.State):
    """Small explicit adaptive contract; durable-State has separate integration tests."""
    def __init__(self, path, paths, now):
        super().__init__(path, paths, {'schedule_mode':'adaptive','start_hour':0,'end_hour':24})
        self.target = 8
        self.start = now
        self.counter = 80
        self.finished = None
    def ensure_day(self, now): return str(policy.local_day(now))
    def refresh_policy(self, now): pass
    def update_forecast(self, now, reason='tick'):
        start=max(now, self.rest_ready_at(now) or now)
        return {'next_start':start.isoformat(),'next_target':self.target,'feasible':True}
    def next_slot(self, now): return self.counter
    def target_for(self, now, slot): return self.target
    def calendar_end(self, now): return policy.work_bounds(now,self.config)[1]
    def rest_ready_at(self, now): return self.finished+timedelta(seconds=540) if self.finished else now
    def adaptive_view(self, now): return {'schedule_mode':'adaptive','next_target':self.target}
    def day_plan(self, now): return [datetime.fromisoformat(self.update_forecast(now)['next_start'])]
    def claim_batch(self, now, slot):
        if self.finished and now < self.rest_ready_at(now):return False
        if self.db.execute("SELECT 1 FROM scheduler_batches WHERE status='running'").fetchone():return False
        cur=self.db.execute("INSERT OR IGNORE INTO scheduler_batches VALUES(?,?,?,NULL,'running')",(str(policy.local_day(now)),slot,now.isoformat()))
        self.db.commit();return bool(cur.rowcount)
    def finish_batch(self, now, slot, status, *, day=None):
        super().finish_batch(now,slot,status,day=day)
        self.finished=now;self.counter+=1
    def batch_spacing_ready(self, now, slot):return True


class AdaptiveWorkerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.path=self.root/'actor.sqlite3'
        self.now=datetime(2026,10,30,5,tzinfo=timezone.utc)
        with closing(engine.db_open(self.path)) as db:api.init_api(db);groups.init_dates(db)
        self.state=BoundaryState(self.root/'scheduler.sqlite3',{1:self.path},self.now)
        self.addCleanup(self.state.close)
        self.cfg={'groups':[{'id':101,'enabled':True,'like_enabled':True,'repost_enabled':True}]}
        self.worker=AdaptiveWorker(self.state,self.path,self.cfg,clock=lambda:self.now)
        self.worker.refresh_batch=Mock(return_value={'state':'ready','pool':None,'report':{}})
        self.worker.scan_step=Mock(side_effect=AssertionError('idle reads forbidden'))
        self.number=0
    def choice(self, now):
        self.number+=1
        return (1,'like',0,{'group_id':101,'post_id':self.number,'published_at':int(now.timestamp())-1})
    def execute(self, account, kind, row, *, before_attempt, record_attempt, **options):
        before_attempt()
        with closing(engine.db_open(self.path)) as db:
            aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",(row['post_id'],self.now.isoformat())).lastrowid;db.commit()
        record_attempt(aid);self.now+=timedelta(seconds=20)
        return {'status':'verified','attempt_id':aid}

    def test_factory_keeps_legacy_default_and_dispatches_explicit_adaptive(self):
        fixed_state,fixed_worker=scheduler.scheduler_types(policy.DEFAULT_CONFIG)
        self.assertIs(fixed_state,ledger.State);self.assertIs(fixed_worker,scheduler.Worker)
        adaptive_state,adaptive_worker=scheduler.scheduler_types(self.state.config)
        self.assertEqual(adaptive_state.__module__,'vk_adaptive_state')
        self.assertIs(adaptive_worker,AdaptiveWorker)

    def test_cli_schedule_preview_activates_forecast_without_claim_or_actions(self):
        cfg=self.state.config
        settings=self.root/'automation.json'
        settings.write_text(json.dumps({'policy':cfg}),encoding='utf-8')
        output=io.StringIO();state_path=self.root/'preview.sqlite3'
        with patch.object(scheduler,'STATE_PATH',state_path),patch.object(scheduler.engine,'DB_PATH',self.path),\
             patch.object(scheduler,'utcnow',return_value=self.now),\
             patch.object(scheduler.groups,'load_config',return_value=self.cfg),\
             patch.object(scheduler.accounts,'account_numbers',return_value=[1]),\
             patch.object(scheduler.accounts,'database_for_account',return_value=self.path),\
             patch.object(scheduler.accounts,'effective_policy',return_value=cfg),\
             patch.object(scheduler.accounts,'account_enabled',side_effect=lambda account:account==1),\
             patch.object(scheduler.actions,'execute_action',side_effect=AssertionError('no action')),redirect_stdout(output):
            self.assertEqual(scheduler.app.main(['--schedule-preview','--automation-config',str(settings)]),0)
        report=json.loads(output.getvalue())
        self.assertIn('adaptive',report)
        self.assertTrue(report['slots'])
        import sqlite3
        with closing(sqlite3.connect(state_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scheduler_adaptive_forecasts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)

    def test_tick_uses_dynamic_target_without_fixed_grace_or_six_cap(self):
        self.state.start=self.now-timedelta(hours=3)
        with patch.object(self.worker,'pick',side_effect=self.choice),patch.object(scheduler.actions,'execute_action',side_effect=self.execute):
            result=self.worker.tick(execute=True)
        self.assertEqual(result['attempted'],8)
        self.assertEqual(self.state.usage(self.now)[(1,'like',0)],8)
        self.assertEqual(self.state.db.execute('SELECT slot FROM scheduler_batches').fetchone()[0],80)
        self.assertEqual(result['adaptive']['next_target'],8)

    def test_rest_starts_after_full_batch_and_monitor_does_not_scan(self):
        with patch.object(self.worker,'pick',side_effect=self.choice),patch.object(scheduler.actions,'execute_action',side_effect=self.execute):
            self.worker.tick(execute=True)
            finish=self.now
            self.now+=timedelta(seconds=539)
            waiting=self.worker.tick(execute=True)
            monitor=self.worker.tick(monitor=True)
            self.assertEqual(waiting['phase'],'waiting_batch')
            self.assertEqual(monitor['phase'],'dry_run_monitoring')
            self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],1)
            self.now=finish+timedelta(seconds=540)
            self.worker.tick(execute=True)
        rows=self.state.db.execute('SELECT started_at,finished_at FROM scheduler_batches ORDER BY started_at').fetchall()
        self.assertEqual((datetime.fromisoformat(rows[1]['started_at'])-datetime.fromisoformat(rows[0]['finished_at'])).total_seconds(),540)
        self.assertEqual(self.worker.refresh_batch.call_count,2)

    def test_long_batch_is_not_cut_at_old_next_forecast(self):
        day=str(policy.local_day(self.now));old_cfg=dict(policy.DEFAULT_CONFIG)
        self.state.db.execute('INSERT INTO schedule_days VALUES(?,?,?)',(day,json.dumps(old_cfg),json.dumps([self.now.isoformat(),(self.now+timedelta(minutes=2)).isoformat()])))
        self.state.db.commit();self.state.target=3
        original=self.execute
        def slow(*args,**kwargs):
            result=original(*args,**kwargs);self.now+=timedelta(seconds=280);return result
        with patch.object(self.worker,'pick',side_effect=self.choice),patch.object(scheduler.actions,'execute_action',side_effect=slow):
            result=self.worker.tick(execute=True)
        self.assertEqual(result['attempted'],3);self.assertEqual(result['phase'],'batch_complete')
        saved=self.state.db.execute('SELECT config,slots FROM schedule_days WHERE day=?',(day,)).fetchone()
        self.assertEqual(json.loads(saved['config']),old_cfg)
        self.assertEqual(len(json.loads(saved['slots'])),2)
        self.assertEqual(self.worker.batch_boundary(80),self.state.calendar_end(self.now))

    def test_stop_block_and_uncertain_prevent_refresh_or_actions(self):
        for meta,value in [('stop_requested',True),('paused','manual_block'),('technical_backoff_until',self.now.timestamp()+10),('resume_not_before',self.now.timestamp()+10)]:
            with self.subTest(meta=meta):
                self.state.set(meta,value)
                with patch.object(scheduler.actions,'execute_action') as execute:
                    result=self.worker.tick(execute=True)
                self.assertIn(result['phase'],('stopped','attention','technical_error_backoff','waiting_batch'))
                self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)
                self.state.set(meta,False if meta in ('stop_requested','paused') else 0)
        self.worker.refresh_batch.assert_not_called()

    def test_uncertain_preserves_spent_attempt_and_halts_current_batch(self):
        def uncertain(account,kind,row,**kwargs):
            self.execute(account,kind,row,**kwargs)
            raise engine.StopRun('simulated ambiguous result')
        with patch.object(self.worker,'pick',side_effect=self.choice),patch.object(scheduler.actions,'execute_action',side_effect=uncertain) as execute:
            result=self.worker.tick(execute=True)
        self.assertEqual(result['phase'],'attention');self.assertEqual(execute.call_count,1)
        self.assertEqual(self.state.usage(self.now)[(1,'like',0)],1)
        self.assertEqual(self.state.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'uncertain')

    def test_no_capacity_or_zero_quota_does_not_claim_or_scan(self):
        self.state.target=0
        result=self.worker.tick(execute=True)
        self.assertEqual(result['phase'],'insufficient_capacity')
        for cfg in self.state.config['accounts'].values():
            for field in ('likes','personal_reposts','community_reposts'):cfg[field]=0
        self.assertEqual(self.worker.tick(execute=True)['phase'],'queue_complete')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)
        self.worker.refresh_batch.assert_not_called()

    def test_delayed_api_start_remains_unclaimed_without_fixed_slot_scan(self):
        self.state.update_forecast=lambda now,reason='tick':{'next_start':(now+timedelta(seconds=300)).isoformat(),'next_target':8}
        self.assertEqual(self.worker.tick(execute=True)['phase'],'waiting_batch')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)
        self.worker.refresh_batch.assert_not_called()

    def test_activity_lease_busy_does_not_claim_or_release_any_profile(self):
        with patch.object(scheduler.accounts,'exclusive_activity',side_effect=scheduler.app.RunAlreadyActive('busy')):
            result=self.worker.tick(execute=True)
        self.assertEqual(result['phase'],'waiting_profile_lock')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)
        self.worker.refresh_batch.assert_not_called()

    def test_calendar_safety_reserve_and_preview_do_not_create_jobs(self):
        self.assertEqual(self.worker.reserve_refresh_deadline(80),self.state.calendar_end(self.now)-timedelta(seconds=120))
        self.assertEqual(self.worker.identity_read_deadline(80),self.state.calendar_end(self.now)-timedelta(seconds=120))
        result=self.worker.tick()
        self.assertEqual(result['phase'],'preview')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],0)
        self.worker.refresh_batch.assert_not_called()


if __name__=='__main__':unittest.main()
