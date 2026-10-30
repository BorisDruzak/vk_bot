"""Adaptive dispatch; all actions, leases and safety guards use the legacy executor."""
from datetime import datetime, timedelta
import os

import vk_action_retry as action_retry
import vk_schedule_policy as policy
from vk_scheduler import Worker


class AdaptiveWorker(Worker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state.source_path=self.source_path
        self.state.groups_count=len(self.groups)
        self.state.planning_clock=self.clock

    def attempt_limit(self, slot):
        value=self.state.target_for(self.clock(),slot)
        if type(value) is not int or not 1<=value<=4320:
            raise ValueError('Adaptive claim has no valid bounded target')
        return value

    def batch_boundary(self, slot):
        # A future prediction can never shorten a running batch.
        return getattr(self.state,'_batch_boundary',None) or self.state.calendar_end(self.clock())

    def reserve_refresh_deadline(self, slot):
        return self.batch_boundary(slot)-timedelta(seconds=120)

    def identity_read_deadline(self, slot):
        return self.reserve_refresh_deadline(slot)

    def wait_batch_spacing(self, slot):
        # The adaptive claim already proved post-finish rest. Recheck the owned
        # claim without the fixed schedule's jitter/grace or first-action gap.
        return None if self.state.batch_spacing_ready(self.clock(),slot) else 'batch_spacing_wait'

    def status(self, phase, **extra):
        now=self.clock();view=self.state.adaptive_view(now)
        forecast=view.get('forecast',view)
        report={
            'at':now.isoformat(),'phase':phase,'day':str(policy.local_day(now)),
            'schedule_mode':'adaptive','adaptive':view,
            'block':self.state.block_status(),'mode':self.state.get('execution_mode','preview'),
            'action_retry':action_retry.RetryLedger(self.state).summary(),
            'next_batch':forecast.get('next_start'),'pid':os.getpid(),'active_slot':self._active_slot,
            'usage':{f'{a}:{k}:{c}':n for (a,k,c),n in self.state.usage(now).items()},
            'remaining':{f'{a}:{k}:{c}':n for (a,k,c),n in self.state.remaining(now).items()},**extra,
        }
        boundary=getattr(self.state,'_batch_boundary',None)
        if self._active_slot is not None and boundary is not None:
            report['batch_deadline_at']=(boundary-timedelta(seconds=120)).isoformat()
        self.state.set('heartbeat',report)
        refresh=extra.get('counter_refresh',{})
        key=(phase,refresh.get('requests',0)//10)
        if self._diagnostic_phase!=key:
            self.state.log_event('counter_progress' if phase=='refreshing_counters' else 'phase',now,phase=phase,
                **{k:v for k,v in refresh.items() if k in ('requests','groups','eligible_posts','unknown_counts','missing_posts')})
            self._diagnostic_phase=key
        return report

    def save_batch_timing(self, started, slot, phase):
        # Dynamic IDs do not index the immutable legacy slots array. Retain the
        # same closed timing vocabulary while omitting an imagined next-slot cutoff.
        try:
            import vk_scheduler_timing as timing
            batch=self.state.db.execute('SELECT * FROM scheduler_batches WHERE day=? AND slot=?',
                (str(policy.local_day(started)),slot)).fetchone()
            if not batch:return
            marks={**(self._timing_marks or {}),'claimed_at':batch['started_at'],'finished_at':batch['finished_at']}
            last=self.state.get('last_action_batch')
            if last and (last.get('day'),last.get('slot'))==(str(policy.local_day(started)),slot):marks['first_action_at']=last['at']
            end=self.state.calendar_end(started)
            window=timing.effective_window(batch['started_at'],None,end,observations=marks,
                now=self.clock(),phase=phase,
                reason='quota' if phase=='queue_empty' and not any(self.state.remaining(self.clock()).values()) else self.state.get('paused'))
            window['Dintent']=(end-timedelta(seconds=120)).isoformat()
            window['remaining_before_intent_seconds']=max(0,(end-timedelta(seconds=120)-self.clock()).total_seconds())
            window['intent_closed']=self.clock()>=end-timedelta(seconds=120)
            ready=timing.moment(marks.get('spacing_ready_at'))
            if ready:window['intent_seconds_available']=max(0,(end-timedelta(seconds=120)-ready).total_seconds())
            window.update(attempts=len(self._timing_attempts),verified=len(self._timing_verified))
            self.state.log_event('batch_timing',self.clock(),slot=slot,effective_window=window)
        except Exception:pass  # Diagnostic failure cannot authorize or reject an action.

    def tick(self, *, execute=False, monitor=False):
        now=self.clock();self.state.refresh_policy(now);self.state.ensure_day(now)
        if self.state.get('stop_requested',False):return self.status('stopped')
        if not execute and not monitor:
            self.state.update_forecast(now,reason='preview')
            return self.status('preview')
        self.state.expire_automatic_block(now)
        if execute:action_retry.RetryLedger(self.state).expire_pending(now)
        if self.state.blocked():return self.status('attention',reason=self.state.get('paused','uncertain job'))
        if not policy.in_work_window(now,self.state.config):return self.status('waiting_window')
        if now.timestamp()<self.state.get('technical_backoff_until',0):return self.status('technical_error_backoff')
        forecast=self.state.update_forecast(now,reason='monitor' if monitor else 'tick')
        if monitor:
            # Full pre-batch engagement reads discover posts as well as counters.
            # Idle catalog reads cannot consume the queue's forecast reservation.
            return self.status('dry_run_monitoring')
        ready=self.state.rest_ready_at(now)
        if now<ready or now.timestamp()<self.state.get('resume_not_before',0):
            return self.status('waiting_batch')
        if not any(self.state.remaining(now).values()):return self.status('queue_complete')
        target=forecast.get('next_target',0);start=forecast.get('next_start')
        if not target or not start:
            return self.status('insufficient_capacity',reason=forecast.get('reason'))
        start=datetime.fromisoformat(start) if isinstance(start,str) else start
        if now<start:return self.status('waiting_batch')
        if now>=self.state.calendar_end(now)-timedelta(seconds=120):return self.status('waiting_window')
        return self.run_batch(self.state.next_slot(now))
