"""One local durable worker: API refresh and independent calendar-limited jobs."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager,closing
from datetime import datetime,timedelta,timezone
from pathlib import Path

import vk_account_smoke as smoke
import vk_api_read as api
import vk_batch as engine
import vk_catalog_scan as catalog
import vk_groups as groups
import vk_schedule_policy as policy
import vk_scheduled_action as actions
import vk_scheduler_state as ledger
import vk_batch_engagement as engagement
import vk_unified as app
import vk_diagnostics as diagnostics
import vk_scheduler_recovery as recovery
import vk_accounts as accounts
import vk_action_retry as action_retry
import vk_scheduler_timing as timing

ROOT=Path(__file__).resolve().parent
STATE_PATH=ROOT/'vk_scheduler.sqlite3'
AUTOMATION_CONFIG=ROOT/'vk_automation.json'


def utcnow():return datetime.now(timezone.utc)


def safe_failure_reason(exc):
    if type(exc) is actions.community.ui.CommunityPreparationTimeout:
        if exc.stage=='community_prepare' and exc.reason_code=='community_prepare_timeout':return exc.reason_code
    if isinstance(exc,engine.ActionTransportError):
        return exc.reason_code if exc.reason_code in ('like_transport_error','repost_transport_error') else type(exc).__name__
    if isinstance(exc,engine.ActionGuardError):
        return exc.reason_code if exc.reason_code in diagnostics.REASONS else type(exc).__name__
    if isinstance(exc,engine.ActionConfirmationError):
        return exc.reason_code if exc.reason_code in ('like_confirmation_timeout','repost_confirmation_timeout') else type(exc).__name__
    if isinstance(exc,(actions.ScheduledActionStopped,actions.community.ui.CommunityConfirmationError)):
        allowed={'action_stopped_'+stage for stage in actions.ScheduledActionStopped.STAGES}
        allowed.update(('community_confirmation_timeout','community_confirmation_ambiguous'))
        if type(exc.reason_code) is str and exc.reason_code in allowed:
            return exc.reason_code
    # Match the reason family to bounded recovery. Eligibility still checks
    # the complete exception chain; this label cannot authorize a retry.
    if isinstance(exc,OSError):return 'TimeoutError' if isinstance(exc,TimeoutError) else 'OSError'
    return type(exc).__name__


def load_settings(path=AUTOMATION_CONFIG):
    raw=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw,dict) or set(raw)-{'policy','scan'}:raise ValueError('Unknown automation settings')
    cfg=policy.validate_config(raw.get('policy'))
    scan={'daily_requests':5000,'refresh_minutes':60,'pages_per_group':10,'pause_seconds':5}
    overrides=raw.get('scan',{})
    if not isinstance(overrides,dict) or set(overrides)-set(scan):raise ValueError('Unknown scan settings')
    scan.update(overrides)
    for key,low,high in [('daily_requests',1,5000),('refresh_minutes',30,1440),('pages_per_group',1,10),('pause_seconds',5,300)]:
        if type(scan[key]) is not int or not low<=scan[key]<=high:raise ValueError('Invalid bounded scanner setting: '+key)
    return cfg,scan


@contextmanager
def singleton(path):
    """OS-held lock releases on crash; an existing file alone is not a lock."""
    stream=Path(path).open('a+b');locked=False
    try:
        if stream.seek(0,2)==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise app.RunAlreadyActive('Scheduler process is already running') from None
        else:
            import fcntl
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise app.RunAlreadyActive('Scheduler process is already running') from None
        locked=True;yield
    finally:
        if locked:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)
        stream.close()


class Worker:
    def __init__(self,state,source_path,cfg,*,clock=utcnow,scan_settings=None,config_path=None):
        self.state,self.source_path,self.cfg,self.clock=state,Path(source_path),cfg,clock
        self.groups=[g for g in cfg['groups'] if g.get('enabled',True)]
        self.scan_settings=scan_settings or {'daily_requests':5000,'refresh_minutes':60,'pages_per_group':10,'pause_seconds':5}
        self.config_path=config_path
        self._batch_pool=None
        self._batch_refresh={}
        self._diagnostic_phase=None
        self.sleeper=time.sleep
        self._active_slot=None
        self._reserve_deadline=None
        self._timing_marks=None
        self._timing_attempts=set()
        self._timing_verified=set()

    def observe_timing_event(self,event,job,**data):
        """Memory only; telemetry cannot reserve, commit or authorize an action."""
        try:
            if self._timing_marks is None:return
            if event=='attempt_reserved' and type(data.get('attempt_id')) is int:
                self._timing_attempts.add((job,data['attempt_id']))
            if event=='action_result' and data.get('outcome')=='verified':self._timing_verified.add(job)
        except Exception:pass

    def record_batch_attempt(self,job,aid):
        self.state.record_attempt(job,aid)
        self.observe_timing_event('attempt_reserved',job,attempt_id=aid)

    def save_batch_timing(self,started,slot,phase):
        try:
            day=str(policy.local_day(started))
            saved=self.state.db.execute('SELECT config,slots FROM schedule_days WHERE day=?',(day,)).fetchone()
            if not saved:return
            slots=json.loads(saved['slots'])
            if type(slot) is not int or not 0<=slot<len(slots):return
            batch=self.state.db.execute('SELECT * FROM scheduler_batches WHERE day=? AND slot=?',(day,slot)).fetchone()
            if not batch:return
            marks={**(self._timing_marks or {}),'claimed_at':batch['started_at'],'finished_at':batch['finished_at']}
            last=self.state.get('last_action_batch')
            if last and (last.get('day'),last.get('slot'))==(day,slot):marks['first_action_at']=last['at']
            config=json.loads(saved['config'])
            end=policy.work_bounds(started,config)[1]
            window=timing.effective_window(slots[slot],slots[slot+1] if slot+1<len(slots) else None,end,
                observations=marks,now=self.clock(),phase=phase,
                reason='quota' if phase=='queue_empty' and not any(self.state.remaining(self.clock()).values()) else self.state.get('paused'))
            window.update(attempts=len(self._timing_attempts),verified=len(self._timing_verified))
            self.state.log_event('batch_timing',self.clock(),slot=slot,effective_window=window)
        except Exception:pass  # Missing diagnostics never change an execution outcome.

    def status(self,phase,**extra):
        now=self.clock();plan=self.state.day_plan(now)
        future=[t for t in plan if t>now]
        if not future:
            tomorrow=now+timedelta(days=1)
            saved=self.state.db.execute('SELECT slots FROM schedule_days WHERE day=?',(str(policy.local_day(tomorrow)),)).fetchone()
            future=([datetime.fromisoformat(t) for t in json.loads(saved[0])] if saved
                    else [policy.work_bounds(tomorrow,self.state.config)[0]])
        report={'at':now.isoformat(),'phase':phase,'day':str(policy.local_day(now)),
                'mode':self.state.get('execution_mode','preview'),
                'action_retry':action_retry.RetryLedger(self.state).summary(),
                'next_batch':future[0].astimezone(policy.LOCAL_TZ).isoformat(),'pid':os.getpid(),'active_slot':self._active_slot,
                'usage':{f'{a}:{k}:{c}':n for (a,k,c),n in self.state.usage(now).items()},
                'remaining':{f'{a}:{k}:{c}':n for (a,k,c),n in self.state.remaining(now).items()},**extra}
        boundary=getattr(self.state,'_batch_boundary',None)
        if self._active_slot is not None and boundary is not None:
            report['batch_deadline_at']=(boundary-timedelta(seconds=60)).isoformat()
        self.state.set('heartbeat',report)
        refresh=extra.get('counter_refresh',{})
        key=(phase,refresh.get('requests',0)//10)
        if self._diagnostic_phase!=key:
            self.state.log_event('counter_progress' if phase=='refreshing_counters' else 'phase',now,phase=phase,
                **{k:v for k,v in refresh.items() if k in ('requests','groups','eligible_posts','unknown_counts','missing_posts')})
            self._diagnostic_phase=key
        return report

    def pulse_action(self,event,*,job=None,stage=None):
        """Best-effort liveness from real progress, never a timer or intent commit."""
        if event not in ('action_started','action_stage','cooldown_started','cooldown_finished','final_click_check','action_result'):
            return
        try:
            if self._active_slot is None or self.state.db.in_transaction:return
            heartbeat={**self.state.get('heartbeat',{}),'at':self.clock().isoformat(),
                'phase':'executing_actions','active_slot':self._active_slot,'pid':os.getpid()}
            if type(job) is int:heartbeat['active_job']=job
            if stage in diagnostics.STAGES:heartbeat['stage']=stage
            self.state.set('heartbeat',heartbeat)
        except Exception:pass  # Liveness is not permission to change an action outcome.

    def queue_snapshot(self):
        now=self.clock();output={};remaining=self.state.remaining(now)
        for account,kind,target,_ in self.state.categories():
            rows=self.state.candidates(now,account,kind,target,self.source_path,self.groups)
            bands=[policy.classify_post(row['published_at'],now,self.state.config) for row in rows]
            output[f'{account}:{kind}:{target}']={
                'today':bands.count('new'),'reserve':bands.count('old'),
                'remaining_daily':remaining[(account,kind,target)],'next':rows[:2]}
        return output

    def pick(self,now):
        choices=[];used=self.state.usage(now);last=self.state.get('last_account',2)
        context=self.state.selection_context(now,self.source_path,self.groups,pool=self._batch_pool)
        for (account,kind,target),remaining in self.state.remaining(now).items():
            if remaining<=0:continue
            rows=self.state.candidates(now,account,kind,target,self.source_path,self.groups,context=context)
            if self._batch_pool is not None:
                rows=[r for r in rows if (r['group_id'],r['post_id']) in self._batch_pool and
                      all(type(r.get(c)) is int and r[c]>=0 for c in ('like_count','repost_count','comment_count'))]
                band=self._batch_refresh.get('selected_band')
                if band in ('new','old'):rows=[r for r in rows if policy.classify_post(r['published_at'],now,self.state.config)==band]
            if not rows:continue
            row=rows[0]
            priority=policy.priority_key(row['published_at'],now,self.state.config,
                like_count=row.get('like_count'),repost_count=row.get('repost_count'),comment_count=row.get('comment_count'))
            row['selection_minimum']=list(priority[:3])
            row['refresh_started_at']=self._batch_refresh.get('started_at')
            limit=next(n for a,k,c,n in self.state.categories() if (a,k,c)==(account,kind,target))
            choices.append((priority[:3],used[(account,kind,target)]/limit,account==last,
                            priority[-1],account,kind,target,row))
        if not choices:return None
        chosen=min(choices,key=lambda r:r[:7]);return chosen[4],chosen[5],chosen[6],chosen[7]

    def batch_stop_phase(self,now,account=None,kind=None,target=0,*,day=None):
        """Explain an already rejected operation; never authorize another one."""
        if self.state.get('stop_requested',False):return 'batch_stop_requested'
        if day is not None and str(policy.local_day(now))!=str(day):return 'batch_day_changed'
        _,end=policy.work_bounds(now,self.state.config)
        if not policy.in_work_window(now,self.state.config) or now>=end-timedelta(seconds=30):
            return 'batch_work_window_closed'
        if self.state.action_time_closed(now):return 'batch_action_deadline'
        if account is not None:
            if not self.state.account_enabled(account):return 'batch_account_disabled'
            if self.state.remaining(now).get((account,kind,target),0)<=0:return 'batch_daily_quota_reached'
        return 'batch_guard_rejected'

    def batch_boundary(self,slot):
        now=self.clock()
        row=self.state.db.execute('SELECT slots FROM schedule_days WHERE day=?',(str(policy.local_day(now)),)).fetchone()
        if not row:return None
        slots=[datetime.fromisoformat(t) for t in json.loads(row[0])]
        if type(slot) is not int or not 0<=slot<len(slots):return None
        boundary=policy.work_bounds(now,self.state.config)[1]
        if slot+1<len(slots):boundary=min(boundary,slots[slot+1])
        return boundary

    def reserve_refresh_deadline(self,slot):
        boundary=self.batch_boundary(slot)
        return boundary-timedelta(seconds=120) if boundary is not None else self.clock()

    def refresh_batch(self,*,reserve=False,deadline=None):
        if deadline is None and self._active_slot is not None:
            deadline=self.reserve_refresh_deadline(self._active_slot)
        self._reserve_deadline=deadline
        return engagement.refresh(self.state,self.source_path,self.groups,self.scan_settings,clock=self.clock,
            progress=lambda report:self.status('refreshing_counters',counter_refresh=report),reserve=reserve,deadline=deadline,
            diagnostic=lambda event,**data:self.state.log_event(event,self.clock(),slot=self._active_slot,**data))

    def prepare_batch_pool(self,slot,*,reserve=False):
        # Never retain a previous pool after a partial/failed second refresh.
        self._batch_pool=set();self._batch_refresh={}
        try:
            if reserve:
                self._reserve_deadline=self.reserve_refresh_deadline(slot)
                refreshed=self.refresh_batch(reserve=True,deadline=self._reserve_deadline)
            else:refreshed=self.refresh_batch()
        except app.RunAlreadyActive:
            return 'waiting_profile_lock'
        except Exception as exc:
            self.state.log_event('scheduler_failed',self.clock(),stage='counter_refresh',reason=safe_failure_reason(exc),error=exc,slot=slot)
            deferred=self.defer_technical(exc,'read')
            if deferred:return deferred
            self.state.pause(safe_failure_reason(exc));return 'attention'
        self._batch_pool=refreshed['pool'];self._batch_refresh=refreshed['report']
        if refreshed['state']=='ready' and refreshed['report'].get('requests',0)>0:self.technical_succeeded('read')
        self.state.set('last_counter_refresh',refreshed['report'])
        return None if refreshed['state']=='ready' else 'counter_refresh_'+refreshed['state']

    def defer_technical(self,exc,scope,job=None):
        """A failed job stays failed; resume only future scheduled work."""
        # This new case belongs exclusively to the explicit two-pass policy.
        if isinstance(exc,actions.community.ui.CommunityPreparationTimeout):return None
        try:
            if not recovery.is_technical(exc,scope):return None
            if not recovery.safe_to_defer(self.state,job):return None
            failures=self.state.get('technical_failures',{})
            count=int(failures.get(scope,0))+1;failures[scope]=count
            self.state.set('technical_failures',failures)
            until=self.clock().timestamp()+min(300,120*count)
            self.state.set('technical_backoff_until',until)
            self.state.set('last_technical_recovery',{'scope':scope,'job':job['id'] if job else None,
                'at':self.clock().isoformat(),'failures':count,'state':'blocked' if count>=3 else 'deferred','not_before':until})
            self.state.log_event('technical_recovery',self.clock(),job=job['id'] if job else None,
                reason='technical_error_repeated' if count>=3 else 'technical_before_send',error=exc,
                outcome='stopped' if count>=3 else 'ready')
            if count>=3:self.state.pause('technical_error_repeated');return 'attention'
            return 'technical_error_deferred'
        except Exception:return None  # Failure to prove safety retains the original stop.

    def technical_succeeded(self,scope):
        saved=self.state.get('technical_failures',{})
        if not saved.get(scope):return
        saved[scope]=0;self.state.set('technical_failures',saved)
        old=self.state.get('last_technical_recovery',{})
        if old.get('scope')==scope:self.state.set('last_technical_recovery',{**old,'state':'recovered','at':self.clock().isoformat()})

    def wait_batch_spacing(self,slot):
        """Only an on-time owned claim may wait beyond claim grace, never next slot."""
        now=self.clock();day=str(policy.local_day(now))
        saved=self.state.db.execute('SELECT slots FROM schedule_days WHERE day=?',(day,)).fetchone()
        if not saved:return None if self.state.batch_spacing_ready(now,slot) else 'batch_spacing_wait'
        slots=[datetime.fromisoformat(t) for t in json.loads(saved[0])]
        if type(slot) is not int or not 0<=slot<len(slots):return 'batch_spacing_wait'
        claimed=self.state.db.execute('SELECT started_at,finished_at FROM scheduler_batches WHERE day=? AND slot=?',(day,slot)).fetchone()
        if (not claimed or claimed['finished_at'] is not None or not
                slots[slot]<=datetime.fromisoformat(claimed['started_at'])<=slots[slot]+timedelta(seconds=policy.MAX_JITTER_SECONDS)):
            return 'batch_spacing_wait'
        if self.state.batch_spacing_ready(self.clock(),slot):return None
        _,end=policy.work_bounds(now,self.state.config)
        deadline=end-timedelta(seconds=60)
        if slot+1<len(slots):deadline=min(deadline,slots[slot+1]-timedelta(seconds=60))
        slept=0
        while True:
            now=self.clock()
            if (str(policy.local_day(now))!=day or self.state.get('stop_requested',False)
                    or not policy.in_work_window(now,self.state.config) or now>=end-timedelta(seconds=30)):
                return self.batch_stop_phase(now,day=day)
            if self.state.blocked():return 'attention'
            for account in self.state.paths:
                with self.state.account_db(account) as db:
                    db.row_factory=sqlite3.Row
                    engine.require_platform_unlocked(db)
            last=self.state.get('last_action_batch')
            if not last:return 'batch_spacing_wait'  # Never infer that spacing state disappeared safely.
            ready=datetime.fromisoformat(last['at'])+timedelta(seconds=policy.MIN_BATCH_GAP_SECONDS)
            # SQLite reads above may block. Deadline decisions must use the
            # time after those checks, not the time before opening databases.
            now=self.clock()
            if (str(policy.local_day(now))!=day or not policy.in_work_window(now,self.state.config)
                    or now>=end-timedelta(seconds=30)):return self.batch_stop_phase(now,day=day)
            if now>deadline or ready>deadline:return 'batch_spacing_deadline'
            if now>=ready:return None  # before_attempt still rechecks the durable spacing state.
            delay=min(5,(ready-now).total_seconds(),(deadline-now).total_seconds(),policy.MIN_BATCH_GAP_SECONDS-slept)
            if delay<=0:return 'batch_spacing_deadline'
            self.status('batch_spacing_wait',spacing_ready_at=ready.isoformat(),
                spacing_remaining_seconds=max(0,int((ready-now).total_seconds()+.999)))
            self.sleeper(delay);slept+=delay

    def run_batch(self,slot):
        try:
            with accounts.exclusive_activity(self.source_path.parent):
                return self._run_batch(slot)
        except app.RunAlreadyActive:
            return self.status('waiting_profile_lock',attempted=0)

    def _run_batch(self,slot):
        self.state._batch_clock=self.clock
        started=self.clock()
        if not self.state.claim_batch(started,slot):
            self.state._batch_clock=None
            return self.status('batch_already_claimed',attempted=0)
        self._active_slot=slot
        self.state._batch_boundary=self.batch_boundary(slot)
        self._timing_marks={};self._timing_attempts=set();self._timing_verified=set()
        attempted=0;skipped=0;phase='batch_complete';reserve_refreshed=False
        self.state.log_event('batch_started',started,slot=slot)
        try:
            refresh_phase=self.prepare_batch_pool(slot)
            if refresh_phase=='counter_refresh_empty' and self._batch_refresh.get('band')=='new':
                reserve_refreshed=True;refresh_phase=self.prepare_batch_pool(slot,reserve=True)
            self._timing_marks['refresh_finished_at']=self.clock()
            if refresh_phase:
                phase=refresh_phase;return self.status(phase,attempted=0)
            try:wait_phase=self.wait_batch_spacing(slot)
            except Exception as exc:
                reason=safe_failure_reason(exc)
                self.state.log_event('scheduler_failed',self.clock(),stage='tick',reason=reason,error=exc,slot=slot)
                self.state.pause(reason);wait_phase='attention'
            if wait_phase:
                phase=wait_phase;return self.status(phase,attempted=0)
            self._timing_marks['spacing_ready_at']=self.clock()
            for _ in range(8):
                if attempted>=self.state.config['batch_size']:break
                if policy.local_day(self.clock())!=policy.local_day(started):
                    phase='batch_day_changed';break
                if self.state.action_time_closed(self.clock()):
                    phase='batch_action_deadline';break
                if (self.state.get('stop_requested',False) or attempted>=self.state.config['batch_size']
                        or not policy.in_work_window(self.clock(),self.state.config)):
                    phase=self.batch_stop_phase(self.clock(),day=policy.local_day(started));break
                retries=action_retry.RetryLedger(self.state)
                pending=retries.next_pending(self.clock(),self._batch_pool) if retries.enabled else None
                if self.state.blocked():phase='attention';break
                selected=((pending['account'],pending['kind'],pending['community_id'],pending) if pending else self.pick(self.clock()))
                if (selected is None and not reserve_refreshed and self._batch_refresh.get('selected_band')=='new'
                        and any(self.state.remaining(self.clock()).values())):
                    reserve_refreshed=True
                    refresh_phase=self.prepare_batch_pool(slot,reserve=True)
                    if refresh_phase:
                        phase=('reserve_'+refresh_phase if attempted and refresh_phase in
                            ('counter_refresh_budget','counter_refresh_empty','counter_refresh_stopped','counter_refresh_deadline','waiting_profile_lock')
                            else refresh_phase);break
                    if self.state.blocked():phase='attention';break
                    if (self.state.get('stop_requested',False) or policy.local_day(self.clock())!=policy.local_day(started)
                            or not policy.in_work_window(self.clock(),self.state.config)):
                        phase=self.batch_stop_phase(self.clock(),day=policy.local_day(started));break
                    pending=retries.next_pending(self.clock(),self._batch_pool) if retries.enabled else None
                    selected=((pending['account'],pending['kind'],pending['community_id'],pending) if pending else self.pick(self.clock()))
                if selected is None:phase='queue_empty';break
                if self.state.action_time_closed(self.clock()):
                    phase='batch_action_deadline';break
                account,kind,target,row=selected
                try:job=pending['id'] if pending else self.state.create_job(self.clock(),slot,account,kind,target,row)
                except RuntimeError:
                    phase=self.batch_stop_phase(self.clock(),account,kind,target,day=policy.local_day(started));break
                if retries.enabled:
                    result=action_retry.execute_job(self,job,row,slot,self.state.config['batch_size']-attempted)
                    attempted+=result['attempted'];skipped+=result['skipped']
                    if result['phase']:
                        phase=('reserve_counter_refresh_deadline' if attempted and result['phase']=='counter_refresh_deadline'
                               else result['phase']);break
                    continue
                try:
                    context={'job':job,'account':account,'kind':kind,'community':target,'source_group':row['group_id'],'source_post':row['post_id'],'slot':slot}
                    def log_action(event,**data):
                        self.observe_timing_event(event,job,**data)
                        self.pulse_action(event,job=job,stage=data.get('stage'))
                        self.state.log_event(event,self.clock(),**context,**data)
                    log_action('action_started')
                    result=actions.execute_action(account,kind,{**row,'community_id':target},
                        before_attempt=lambda:self.state.before_attempt(job,self.clock()),
                        record_attempt=lambda aid:self.record_batch_attempt(job,aid),
                        before_click=lambda:self.state.click_timeout(job,self.clock()),config_path=self.config_path,diagnostic=log_action)
                    status=result['status']
                    if status=='skipped_vk':
                        if result.get('attempt_id') is not None:raise RuntimeError('VK skip cannot contain an attempt')
                        self.state.finish_vk_skip(job,result.get('skip_evidence'),self.clock())
                        log_action('action_result',outcome='skipped_vk',reason=result['skip_evidence']['reason'])
                        skipped+=1;continue
                    self.state.finish_job(job,'failed' if status=='unavailable' else status)
                    log_action('action_result',outcome=status,attempt_id=result.get('attempt_id'))
                    if result.get('attempt_id') is not None:attempted+=1
                    if status in ('verified','already'):self.technical_succeeded('action')
                    self.state.set('last_account',account)
                except Exception as exc:
                    saved=self.state.db.execute('SELECT * FROM scheduler_jobs WHERE id=?',(job,)).fetchone()
                    uncertain=saved['status']=='attempting' or saved['attempt_id'] is not None
                    reason=safe_failure_reason(exc)
                    if isinstance(exc,engine.ActionConfirmationError):
                        self.state.set('last_action_confirmation',{'job':job,'at':self.clock().isoformat(),
                            'reason':reason,'observation':exc.observation})
                    self.state.finish_job(job,'uncertain' if uncertain else 'failed',reason)
                    log_action('action_failed',reason=reason,outcome='uncertain' if uncertain else 'failed',attempt_id=saved['attempt_id'],
                               stage=getattr(exc,'stage',None),observation=getattr(exc,'observation',None),error=exc)
                    _,end=policy.work_bounds(self.clock(),self.state.config)
                    normal_stop=(self.state.get('stop_requested',False) or
                        self.state.action_time_closed(self.clock()) or
                        isinstance(exc,ledger.BatchSpacingWait) or self.clock()>=end-timedelta(seconds=30)
                        or self.state.remaining(self.clock()).get((account,kind,target),0)<=0)
                    if not uncertain and normal_stop:
                        phase=('batch_spacing_deadline' if isinstance(exc,ledger.BatchSpacingWait) else
                               self.batch_stop_phase(self.clock(),account,kind,target,day=policy.local_day(started)));break
                    if not uncertain:
                        deferred=self.defer_technical(exc,'action',dict(saved))
                        if deferred:phase=deferred;break
                    # No exception text or authenticated data is logged/persisted.
                    self.state.pause(reason);phase='attention';break
        finally:
            self._batch_pool=None;self._batch_refresh={};self._reserve_deadline=None;self.state._batch_boundary=None;self.state._batch_clock=None
            self.state.finish_batch(self.clock(),slot,phase,day=policy.local_day(started))
            self.state.log_event('batch_result',self.clock(),slot=slot,phase=phase)
            self.save_batch_timing(started,slot,phase)
            self._timing_marks=None
            self._active_slot=None
        return self.status(phase,attempted=attempted,skipped_vk=skipped)

    def scan_step(self):
        now=self.clock();settings=self.scan_settings
        if now.timestamp()<self.state.get('scan_not_before',0):return None
        token=api.configured_token()
        if not token:raise api.VkApiAuthFailure('No local API key configured')
        with app.exclusive_run(),closing(engine.db_open(self.source_path)) as db:
            catalog.init_catalog(db)
            saved=self.state.get('scan_cycle')
            cycle=db.execute('SELECT * FROM catalog_cycles WHERE id=?',(saved,)).fetchone() if saved else None
            if cycle and cycle['state']=='stopped':raise RuntimeError('API cycle stopped; manual inspection required')
            if cycle and cycle['state']!='running':
                last=datetime.fromisoformat(cycle['started_at']).timestamp()
                if now.timestamp()-last<settings['refresh_minutes']*60:return catalog.coverage(db,cycle['id'])
                cycle=None
            if cycle is None:
                cid=catalog.start_cycle(db,self.groups,days=self.state.config['old_days']+1,
                    max_pages_per_group=settings['pages_per_group'])
                self.state.set('scan_cycle',cid)
            else:cid=cycle['id']
            result=catalog.step(db,token,cid,daily_limit=settings['daily_requests'],pause_seconds=settings['pause_seconds'])
            if result and result.get('requests',0)>0 and result.get('status') not in ('budget','error'):
                self.technical_succeeded('read')
            if result and result.get('status')=='budget':self.state.set('scan_not_before',now.timestamp()+3600)
            coverage=catalog.coverage(db,cid);self.state.set('scan_coverage',coverage)
            return coverage

    def tick(self,*,execute=False,monitor=False):
        now=self.clock();self.state.refresh_policy(now);plan=self.state.day_plan(now)
        if not execute and not monitor:return self.status('preview')
        if execute:action_retry.RetryLedger(self.state).expire_pending(now)
        if self.state.blocked():return self.status('attention',reason=self.state.get('paused','uncertain job'))
        if not policy.in_work_window(now,self.state.config):return self.status('waiting_window')
        if execute and now.timestamp()<self.state.get('resume_not_before',0):return self.status('waiting_batch')
        if now.timestamp()<self.state.get('technical_backoff_until',0):return self.status('technical_error_backoff')
        if monitor:
            try:coverage=self.scan_step()
            except app.RunAlreadyActive:return self.status('waiting_profile_lock')
            except Exception as exc:
                self.state.log_event('scheduler_failed',self.clock(),stage='scan',reason=safe_failure_reason(exc),error=exc)
                deferred=self.defer_technical(exc,'read')
                if deferred:return self.status(deferred)
                self.state.pause(type(exc).__name__)
                return self.status('attention',reason=type(exc).__name__)
            return self.status('dry_run_monitoring',coverage=coverage,queue=self.queue_snapshot())
        slot=policy.due_slot(now,plan,config=self.state.config)
        if slot is not None:
            exists=self.state.db.execute('SELECT 1 FROM scheduler_batches WHERE day=? AND slot=?',(str(policy.local_day(now)),slot)).fetchone()
            if not exists:return self.run_batch(slot)
        future=[t for t in plan if t>now]
        # Do not start a potentially30s network operation close to a batch.
        if future and (future[0]-now).total_seconds()<60:return self.status('waiting_batch')
        try:
            coverage=self.scan_step()
        except app.RunAlreadyActive:return self.status('waiting_profile_lock')
        except Exception as exc:
            self.state.log_event('scheduler_failed',self.clock(),stage='scan',reason=safe_failure_reason(exc),error=exc)
            deferred=self.defer_technical(exc,'read')
            if deferred:return self.status(deferred)
            self.state.pause(type(exc).__name__)
            return self.status('attention',reason=type(exc).__name__)
        return self.status('monitoring',coverage=coverage)


def scan_all(cfg,settings):
    token=api.configured_token()
    if not token:raise api.VkApiAuthFailure('No local API key configured')
    selected=[g for g in cfg['groups'] if g.get('enabled',True)]
    with app.exclusive_run(),closing(engine.db_open(engine.DB_PATH)) as db:
        cycle=catalog.start_cycle(db,selected,days=8,max_pages_per_group=settings['pages_per_group'])
        while True:
            result=catalog.step(db,token,cycle,daily_limit=settings['daily_requests'],pause_seconds=settings['pause_seconds'])
            report=catalog.coverage(db,cycle)
            print('[CATALOG]',json.dumps(report,ensure_ascii=False),flush=True)
            if result is None or result.get('status')=='budget' or report['state']!='running':break
        return report


def run_cli(args,parser):
    if args.repost_community is not None or args.likes is not None or args.reposts is not None or args.test_actions:
        parser.error('Scheduler uses vk_automation.json daily quotas, not command action counts')
    if args.dry_run and (not args.scheduler_daemon or args.confirm):
        parser.error('--dry-run is only for --scheduler-daemon and cannot combine with --confirm')
    cfg=groups.load_config(args.config)
    policy_cfg,scan=load_settings(args.automation_config)
    if args.scan_all:
        cfg['groups']=groups.selected_groups(cfg,args.only,0)
        report=scan_all(cfg,scan);return 0 if report['state']=='complete' else 2
    if args.engagement_report:
        selected=groups.selected_groups(cfg,args.only,0);start,_=policy.day_bounds(utcnow())
        with closing(engine.db_open(engine.DB_PATH)) as db:
            api.init_api(db)
            print(json.dumps({'today':api.engagement_report(db,selected,since=start),
                'reserve':api.engagement_report(db,selected,since=start-7*86400,until=start)},ensure_ascii=False,indent=2))
        return 0
    if args.only:parser.error('Scheduler scans all configured groups; --only is for --scan-all/--engagement-report')
    paths={n:accounts.database_for_account(n) for n in accounts.account_numbers()}
    # Initialize only empty additional account ledgers; public catalog is not cloned.
    for number,path in paths.items():
        if number>2 and not path.exists():
            with closing(engine.db_open(path)):pass
    with closing(ledger.State(STATE_PATH,paths,policy_cfg)) as state:
        state.policy_provider=lambda:accounts.effective_policy(policy_cfg)
        state.enabled_provider=accounts.account_enabled
        state.paths_provider=lambda:{n:accounts.database_for_account(n) for n in accounts.account_numbers()}
        state.refresh_policy(utcnow())
        worker=Worker(state,engine.DB_PATH,cfg,scan_settings=scan,config_path=args.config)
        if args.scheduler_status:
            print(json.dumps({'heartbeat':state.get('heartbeat'),'paused':state.get('paused'),
                'active_uncertain':state.active_uncertain_count(),
                'action_retry':action_retry.RetryLedger(state).summary(),
                'isolated_posts':state.db.execute('SELECT COUNT(*) FROM scheduler_post_isolations').fetchone()[0],
                'vk_skips':state.db.execute('''SELECT COUNT(*) FROM scheduler_skip_evidence s
                    JOIN scheduler_jobs j ON j.id=s.job_id WHERE j.day=?''',(str(policy.local_day(utcnow())),)).fetchone()[0],
                'last_vk_skip':state.get('last_vk_skip'),
                'last_diagnostic_error':state.get('last_diagnostic_error'),
                'technical_failures':state.get('technical_failures',{}),
                'technical_backoff_until':state.get('technical_backoff_until',0),
                'last_technical_recovery':state.get('last_technical_recovery'),
                'coverage':state.get('scan_coverage'),'jobs':state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0]},ensure_ascii=False,indent=2));return 0
        if args.scheduler_stop:
            state.set('stop_requested',True);print('[SCHEDULER] Stop requested; no new jobs');return 0
        if args.schedule_preview:
            now=utcnow();plan=state.day_plan(now)
            print(json.dumps({'policy':state.config,'slots':[t.astimezone(policy.LOCAL_TZ).isoformat() for t in plan],
                'remaining':{f'{a}:{k}:{c}':n for (a,k,c),n in state.remaining(now).items()},
                'queue':worker.queue_snapshot()},ensure_ascii=False,indent=2));return 0
        if args.scheduler_check:
            import vk_scheduler_probe as probe
            with closing(engine.db_open(engine.DB_PATH)) as source:
                row=source.execute('''SELECT a.group_id,a.post_id,a.published_at FROM api_seen_posts a
                  JOIN post_publication d ON d.group_id=a.group_id AND d.post_id=a.post_id
                  WHERE a.valid=1 AND d.exact=1 AND a.published_at=d.published_at AND a.group_id IN ('''+
                  ','.join('?' for _ in worker.groups)+''') ORDER BY a.published_at DESC LIMIT 1''',
                  [g['id'] for g in worker.groups]).fetchone()
                if row is None:raise RuntimeError('No exact API source for read-only browser checks')
                row=dict(row)
            reports=[]
            for account in paths:
                if str(account) not in state.config['accounts'] or not state.account_enabled(account):continue
                for kind in ('like','personal','community'):
                    if reports:
                        print('[READ ONLY PAUSE] 20 секунд между проверками',flush=True)
                        time.sleep(20)
                    selected={**row,'community_id':state.config['accounts'][str(account)]['communities'][0]}
                    reports.append(probe.probe(account,kind,selected,config_path=args.config))
                    print('[READ ONLY CHECK]',json.dumps(reports[-1],ensure_ascii=False),flush=True)
            print('[READ ONLY CHECK COMPLETE]',len(reports),'no action clicks');return 0
        if args.scheduler_daemon and not (args.confirm or args.dry_run):
            parser.error('--scheduler-daemon requires explicit --dry-run or --confirm')
        if args.scheduler_once and not args.confirm:
            print(json.dumps(worker.tick(execute=False),ensure_ascii=False,indent=2));return 0
        with singleton(ROOT/'.vk-scheduler.guard'):
            state.recover();state.set('stop_requested',False)
            state.set('execution_mode','live' if args.confirm else 'dry_run')
            if args.scheduler_once:
                result=worker.tick(execute=True);print(json.dumps(result,ensure_ascii=False));return 2 if result['phase']=='attention' else 0
            with (ROOT/'vk_scheduler.log').open('a',encoding='utf-8',buffering=1) as log:
                previous_out,previous_err=sys.stdout,sys.stderr
                try:
                    sys.stdout=sys.stderr=log
                    print('[SCHEDULER START]',utcnow().isoformat(),flush=True)
                    last=None
                    while not state.get('stop_requested',False):
                        try:result=worker.tick(execute=bool(args.confirm),monitor=bool(args.dry_run))
                        except Exception as exc:
                            state.log_event('scheduler_failed',utcnow(),stage='tick',reason=safe_failure_reason(exc),error=exc)
                            state.pause(type(exc).__name__);result=worker.status('attention',reason=type(exc).__name__)
                        if result['phase']!=last:print('[SCHEDULER]',json.dumps(result,ensure_ascii=False),flush=True);last=result['phase']
                        time.sleep(5)
                    worker.status('stopped')
                finally:sys.stdout,sys.stderr=previous_out,previous_err
    return 0
