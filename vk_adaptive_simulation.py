"""Deterministic virtual execution of adaptive forecasts. No network or runtime state.

The simulator consumes attempts on failures too. Uncertainty is an accounting
scenario only: production's protection stops are not granted an exemption here.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vk_adaptive_metrics import estimate_costs
from vk_adaptive_planner import plan_day


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Aware simulation time required')
    return value.astimezone(timezone.utc)


def simulate_day(now, end, remaining, action_seconds, refresh_seconds,
                 refresh_requests, api_attempts, *, last_finished_at=None,
                 changes=(), slow_batches=None, outcomes=('verified',),
                 rest_seconds=540, api_limit=5000, api_pause_seconds=5,
                 identity_reads_per_attempt=1, reserved_reads=0):
    """Replan at every real finish, with optional daily quota changes at boundaries.

    ``changes`` entries have ``at`` and ``quotas`` (absolute calendar-day quota
    totals for supplied categories). Already spent attempts are subtracted. A
    zero quota disables a category; changing it never refunds earlier attempts.
    ``slow_batches`` scales actual elapsed time without changing the forecast.
    Output contains aggregate measurements only, without actor or destination IDs.
    """
    now, end = _time(now), _time(end)
    if end <= now or (end-now).total_seconds() > 86400:
        raise ValueError('Simulation is limited to one calendar day')
    if not outcomes or any(outcome not in ('verified','failed','uncertain','date_skipped') for outcome in outcomes):
        raise ValueError('Invalid virtual outcomes')
    quotas = dict(remaining)
    spent = Counter()
    reads = sorted(_time(stamp) for stamp in api_attempts)
    change_queue = sorted([(_time(row['at']),dict(row['quotas'])) for row in changes], key=lambda row: row[0])
    slow_batches = slow_batches or {}
    if any(type(factor) not in (float,int) or not 0 < factor < float('inf') for factor in slow_batches.values()):
        raise ValueError('Positive finite duration multipliers required')
    batches, outcome_counts = [], Counter()
    reason, peak_reads, initial_forecast = 'quota_complete', 0, None
    last_finished_at = _time(last_finished_at) if last_finished_at is not None else None

    def rolling(at):
        return bisect_right(reads,at)-bisect_right(reads,at-timedelta(days=1))

    peak_reads=rolling(now)

    def reserve_reads(count, at):
        nonlocal peak_reads
        for _ in range(count):
            if rolling(at) >= api_limit-reserved_reads:
                return None
            reads.append(at)
            peak_reads = max(peak_reads,rolling(at))
            at += timedelta(seconds=api_pause_seconds)
        return at

    while now < end:
        if last_finished_at is not None:
            now = max(now,last_finished_at+timedelta(seconds=rest_seconds))
        if now >= end:
            break
        while change_queue and change_queue[0][0] <= now:
            _, updates = change_queue.pop(0)
            quotas.update(updates)
        pending = {key:max(0,quota-spent[key]) for key,quota in quotas.items()}
        if not any(pending.values()):
            if change_queue and change_queue[0][0] < end:
                now=max(now,change_queue[0][0])
                continue
            break
        forecast = plan_day(now,end,pending,action_seconds,refresh_seconds,refresh_requests,
            reads,last_finished_at=last_finished_at,rest_seconds=rest_seconds,api_limit=api_limit,
            api_pause_seconds=api_pause_seconds,identity_reads_per_attempt=identity_reads_per_attempt,
            reserved_reads=reserved_reads)
        if initial_forecast is None:
            initial_forecast = {key:forecast.get(key) for key in
                ('feasible','reason','remaining_attempts','batch_count','next_target','maximum_attempts_fit','api_required','api_available')}
        target = forecast['next_target']
        if target <= 0 or not forecast.get('next_start'):
            if change_queue and change_queue[0][0] < end:
                now=max(now,change_queue[0][0])
                continue
            reason=forecast.get('reason','no_capacity')
            break
        started=max(now,_time(forecast['next_start']))
        if change_queue and change_queue[0][0] <= started:
            now=started
            continue
        if started >= end:
            reason='calendar_end'
            break
        scale=slow_batches.get(len(batches),1)
        refresh_elapsed=max(refresh_seconds*scale,api_pause_seconds*max(0,refresh_requests-1))
        if started+timedelta(seconds=refresh_elapsed)>=end:
            reason='calendar_end'
            break
        refreshed=reserve_reads(refresh_requests,started)
        if refreshed is None:
            reason='api_budget'
            break
        cursor=max(started+timedelta(seconds=refresh_elapsed),refreshed)
        attempted, kinds = 0, Counter()
        for _ in range(target):
            key=next((key for key in quotas if spent[key]<quotas[key]),None)
            if key is None:
                break
            duration=max(20,action_seconds[key[1]]*scale,
                         identity_reads_per_attempt*api_pause_seconds)
            if cursor+timedelta(seconds=duration)>end:
                reason='calendar_end'
                break
            if reserve_reads(identity_reads_per_attempt,cursor) is None:
                reason='api_budget'
                break
            # A failed/uncertain attempt occupies the same quota and elapsed time.
            outcome_counts[outcomes[sum(spent.values())%len(outcomes)]]+=1
            spent[key]+=1
            kinds[key[1]]+=1
            attempted+=1
            cursor+=timedelta(seconds=duration)
        batches.append({'started_at':started.isoformat(),'finished_at':cursor.isoformat(),
                        'forecast_target':target,'attempted':attempted,'kinds':dict(kinds)})
        now=last_finished_at=cursor
        if not attempted:
            break
    pending_total=sum(max(0,quota-spent[key]) for key,quota in quotas.items())
    if pending_total and reason=='quota_complete':
        reason='calendar_end'
    return {'attempted':sum(spent.values()),'remaining_attempts':pending_total,
            'actor_count':len({key[0] for key in quotas}), 'batch_count':len(batches),
            'batches':batches,'outcomes':dict(outcome_counts),'reason':reason,
            'peak_rolling_api_reads':peak_reads,'initial_forecast':initial_forecast,
            'assumptions':['virtual_duration_model','full_refresh_each_pack',
                           'replan_after_actual_finish','quota_spent_on_all_attempts',
                           'protection_stop_not_simulated','forecast_not_verified_live_guarantee']}


def _daily_quotas(actors=5):
    quotas={}
    for actor in range(1,actors+1):
        quotas[(actor,'like',0)]=50
        quotas[(actor,'personal',0)]=0 if actor==5 else 10
        quotas[(actor,'community',1)]=10
        quotas[(actor,'community',2)]=10
    return quotas


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scheduler-db',type=Path,help='Optional read-only measured scheduler database')
    args=parser.parse_args(argv)
    start=datetime(2026,10,29,19,tzinfo=timezone.utc)  # midnight YEKT
    if args.scheduler_db:
        with sqlite3.connect(args.scheduler_db.resolve().as_uri()+'?mode=ro',uri=True) as db:
            model=estimate_costs(db,datetime.now(timezone.utc))
    else:
        with sqlite3.connect(':memory:') as db:
            model=estimate_costs(db,start)
    scenarios={}
    cases={'full_day_390':{},'slow_pack_replan':{'slow_batches':{2:3}},
           'sixth_actor_at_midday':{'changes':[{'at':start+timedelta(hours=12),'quotas':_daily_quotas(6)}]},
           'errors_and_uncertain_accounting':{'outcomes':('verified','failed','uncertain')}}
    for name,options in cases.items():
        result=simulate_day(start,start+timedelta(days=1),_daily_quotas(),
            model['action_seconds'],model['refresh_seconds'],model['refresh_requests'],[],**options)
        scenarios[name]={key:value for key,value in result.items() if key!='batches'}
    print(json.dumps({'model':model,'scenarios':scenarios,
                      'notice':'Simulation forecast; not a verified live guarantee.'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
