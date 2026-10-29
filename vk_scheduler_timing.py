"""Read-only explanations of saved scheduling windows. Never grants permission."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta

import vk_schedule_policy as policy

TIMES={'S','N','E','B','Dread','Dintent','click_deadline','claimed_at',
       'refresh_finished_at','spacing_ready_at','first_action_at','finished_at'}
NUMBERS={'claim_delay_seconds','spacing_wait_seconds','intent_seconds_available',
         'remaining_before_intent_seconds','next_slot_delay_seconds'}
CAUSES={'late_claim','spacing_wait','counter_refresh_deadline','action_deadline',
        'no_candidate','platform_block','guard_block','quota','stop_requested','day_changed'}


def moment(value):
    if isinstance(value,str):
        try:value=datetime.fromisoformat(value)
        except ValueError:return None
    return value if isinstance(value,datetime) and value.tzinfo is not None and value.utcoffset() is not None else None


def sanitize_window(value):
    """Closed vocabulary, no arbitrary strings or nested runtime objects."""
    if not isinstance(value,dict):return {}
    out={}
    for key,val in value.items():
        if key in TIMES:
            stamp=moment(val)
            if stamp:out[key]=stamp.isoformat()
            elif val is None:out[key]=None
        elif key in NUMBERS and (val is None or type(val) in (int,float) and math.isfinite(val) and val>=0):out[key]=val
        elif key in ('attempts','verified') and type(val) is int and 0<=val<=2**53-1:out[key]=val
        elif key in ('intent_closed','claim_grace_exceeded') and type(val) is bool:out[key]=val
        elif key=='causes' and isinstance(val,list):out[key]=[x for x in val if type(x) is str and x in CAUSES]
    return out


def effective_window(S,N,E,*,observations=None,now,phase=None,reason=None):
    """Recompute fixed deadlines, preserving missing observations as null."""
    S,N,E,now=moment(S),moment(N),moment(E),moment(now)
    if S is None or E is None or now is None:raise ValueError('Aware window timestamps required')
    observations=observations or {}
    B=min(N,E) if N is not None else E
    read,intent,click=B-timedelta(seconds=120),B-timedelta(seconds=60),min(B-timedelta(seconds=20),E-timedelta(seconds=5))
    marks={k:moment(observations.get(k)) for k in TIMES if k.endswith('_at')}
    claimed,refresh,ready=marks['claimed_at'],marks['refresh_finished_at'],marks['spacing_ready_at']
    delay=max(0,(claimed-S).total_seconds()) if claimed else None
    wait=max(0,(ready-refresh).total_seconds()) if ready and refresh else None
    causes=[]
    if delay:causes.append('late_claim')
    if wait:causes.append('spacing_wait')
    if phase in ('batch_spacing_wait','batch_spacing_deadline') and 'spacing_wait' not in causes:causes.append('spacing_wait')
    if phase and 'counter_refresh_deadline' in phase:causes.append('counter_refresh_deadline')
    if phase in ('batch_action_deadline','batch_spacing_deadline','batch_work_window_closed'):causes.append('action_deadline')
    if phase=='queue_empty':causes.append('quota' if reason=='quota' else 'no_candidate')
    if phase=='batch_daily_quota_reached':causes.append('quota')
    if phase=='batch_stop_requested':causes.append('stop_requested')
    if phase=='batch_day_changed':causes.append('day_changed')
    if phase=='attention':causes.append('platform_block' if reason in ('PlatformRateLimited','VkApiLimited','VkApiProtection') else 'guard_block')
    values={'S':S,'N':N,'E':E,'B':B,'Dread':read,'Dintent':intent,'click_deadline':click,**marks,
            'claim_delay_seconds':delay,'spacing_wait_seconds':wait,
            'intent_seconds_available':max(0,(intent-ready).total_seconds()) if ready else None,
            'remaining_before_intent_seconds':max(0,(intent-now).total_seconds()),
            'next_slot_delay_seconds':max(0,(marks['finished_at']-N).total_seconds()) if N and marks['finished_at'] else None,
            'intent_closed':now>=intent,'claim_grace_exceeded':claimed>S+timedelta(seconds=120) if claimed else False,
            'causes':causes}
    return sanitize_window(values)


def _rows(db,sql,args=()):
    cursor=db.execute(sql,args)
    names=[col[0] for col in cursor.description]
    return [dict(zip(names,row)) for row in cursor]


def report(db,day,now):
    """Accept an existing *read-only* connection. No State init/migrations/writes."""
    saved=_rows(db,'SELECT config,slots FROM schedule_days WHERE day=?',(str(day),))
    if not saved:return {'day':str(day),'slots':[],'summary':{},'stages':{}}
    cfg=json.loads(saved[0]['config']);slots=json.loads(saved[0]['slots'])
    day_start=datetime.fromisoformat(str(day)).replace(tzinfo=policy.LOCAL_TZ)
    E=day_start.replace(hour=cfg.get('end_hour',19))
    batches={r['slot']:r for r in _rows(db,'SELECT * FROM scheduler_batches WHERE day=?',(str(day),))}
    jobs=_rows(db,'SELECT id,slot,status,attempt_id FROM scheduler_jobs WHERE day=?',(str(day),))
    by_slot={};stage_samples=[];job_ids={j['id'] for j in jobs}
    # Index by event in the existing diagnostics store; filter timestamps after parsing
    # (UTC/local offsets and mixed precision must compare as instants, not strings).
    truncated=False
    diagnostics_rows=_rows(db,"""SELECT snapshot FROM scheduler_diagnostics
        WHERE event IN ('batch_timing','stage_timing')
        AND julianday(at)>=julianday(?) AND julianday(at)<julianday(?)
        ORDER BY id DESC LIMIT 20001""",(day_start.isoformat(),(day_start+timedelta(days=1)).isoformat()))
    if len(diagnostics_rows)>20000:truncated=True;diagnostics_rows=diagnostics_rows[:20000]
    for item in reversed(diagnostics_rows):
        try:r=json.loads(item['snapshot'])
        except (TypeError,ValueError):continue
        if not isinstance(r,dict):continue
        stamp=moment(r.get('at'))
        if stamp is None or stamp.astimezone(policy.LOCAL_TZ).date()!=day_start.date():continue
        if r.get('event')=='batch_timing' and type(r.get('slot')) is int:
            by_slot[r['slot']]=sanitize_window(r.get('effective_window'))
        elif r.get('event')=='stage_timing' and type(r.get('job')) is int and r['job'] in job_ids:stage_samples.append(r)
    result=[]
    for i,S in enumerate(slots):
        batch=batches.get(i);N=slots[i+1] if i+1<len(slots) else None
        observed=by_slot.get(i,{})
        marks={**observed,**({k:batch.get(k) for k in ('finished_at',)} if batch else {})}
        if batch:marks['claimed_at']=batch['started_at']
        status=batch['status'] if batch else ('scheduled' if now<moment(S) else 'claimable' if now<=moment(S)+timedelta(seconds=120) else 'missed')
        window=effective_window(S,N,E,observations=marks,now=now,phase=status,
            reason='quota' if 'quota' in observed.get('causes',[]) else None)
        # Preserve measured cause flags, even after the state that explained them changed.
        window['causes']=list(dict.fromkeys(observed.get('causes',[])+window['causes']))
        selected=[j for j in jobs if j['slot']==i]
        attempts=observed.get('attempts',sum(j['attempt_id'] is not None or j['status']=='attempting' for j in selected))
        counts=Counter(j['status'] for j in selected)
        result.append({**window,'slot':i,'status':status,'telemetry_available':i in by_slot,
                       'attempts':attempts,'verified':observed.get('verified',counts['verified']),'uncertain':counts['uncertain'],
                       'unfilled':max(0,cfg.get('batch_size',4)-attempts),
                       'fourth_not_started_before_intent':status=='batch_action_deadline' and attempts==3,
                       'unclaimed_after_grace':status=='missed'})
    from vk_action_timing import summarize
    counts=Counter(r['status'] for r in result)
    return {'day':str(day),'slots':result,'stages':summarize(stage_samples),
            'diagnostics_truncated':truncated,
            'summary':{'theoretical_attempts':len(slots)*cfg.get('batch_size',4),
                       'attempts':sum(r['attempts'] for r in result),'verified':sum(j['status']=='verified' for j in jobs),
                       'missed':counts['missed'],'empty':sum(r['attempts']==0 and r['finished_at'] is not None for r in result),
                       'partial':sum(0<r['attempts']<cfg.get('batch_size',4) and r['finished_at'] is not None for r in result)}}
