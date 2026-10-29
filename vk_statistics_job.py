"""Background public API observations; one request per allowlisted group.

The global profile/API lock and existing request ledger remain authoritative.
"""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from datetime import datetime,timezone

import vk_accounts as accounts
import vk_api_read as api
from vk_statistics_refresh import refresh_one

ACTIVE={'queued','running','waiting_lock'}
STALE_SECONDS=1900


def path():return accounts.ROOT/'.vk-statistics-refresh.json'


def status():
    if not path().exists():return {'state':'idle'}
    row=json.loads(path().read_text(encoding='utf-8'))
    if row.get('state') not in ACTIVE:return row
    try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(row['at'])).total_seconds()
    except (KeyError,TypeError,ValueError):return row
    if age<=STALE_SECONDS:return row
    from vk_scheduler import singleton
    import vk_unified as app
    try:
        with singleton(accounts.ROOT/'.vk-statistics-refresh.guard'):pass
    except (app.RunAlreadyActive,OSError):return row
    # Report process absence without rewriting the saved job or clearing locks.
    return {**row,'state':'error','reason':'statistics_process_not_reporting'}


def configured_ids():
    import vk_statistics
    return sorted(vk_statistics._names())


def launch(job_id):
    python=Path(sys.executable)
    if python.name.lower()=='python.exe' and python.with_name('pythonw.exe').exists():
        python=python.with_name('pythonw.exe')
    subprocess.Popen([str(python),'-X','utf8',str(Path(__file__).resolve()),'--job',job_id],
        cwd=accounts.ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def start(group_id=None):
    ids=configured_ids()
    if group_id is not None:
        if type(group_id) is not int or group_id not in ids:raise ValueError('Unconfigured statistics group')
        ids=[group_id]
    if not ids:raise ValueError('No configured groups')
    with accounts.management_lock():
        current=status()
        if current.get('state') in ACTIVE:return current
        job={'job_id':uuid.uuid4().hex,'state':'queued','group_ids':ids,'groups_done':0,
             'total':len(ids),'requests':0,'cache_hits':0,'failures':0,'at':datetime.now(timezone.utc).isoformat()}
        accounts.atomic_json(path(),job)
        try:launch(job['job_id'])
        except Exception:
            accounts.atomic_json(path(),{**job,'state':'error','reason':'statistics_worker_launch_failed'})
            raise
        return job


def run(job_id):
    job=status()
    if job.get('job_id')!=job_id:return
    job={'groups_done':0,'requests':0,'cache_hits':0,'failures':0,'rejected_posts':0,'total':len(job.get('group_ids',[])),**job}
    def save(state,**extra):
        nonlocal job
        current=status()
        if current.get('job_id')!=job_id:raise RuntimeError('Statistics job replaced')
        job={**job,'state':state,'at':datetime.now(timezone.utc).isoformat(),**extra}
        accounts.atomic_json(path(),job)
    until=time.monotonic()+1800
    try:
        for group_id in job['group_ids']:
            while True:
                if time.monotonic()>until:save('error',reason='statistics_wait_timeout');return
                save('running',current_group=group_id)
                try:result=refresh_one(accounts.database_for_account(1),group_id)
                except api.VkApiAuthFailure as exc:
                    if exc.code==7:
                        save('running',groups_done=job['groups_done']+1,failures=job['failures']+1,
                             requests=job['requests']+1,reason='group_access_unavailable')
                        break
                    raise
                if result['state']=='deferred':
                    save('waiting_lock',reason='profile_or_worker_busy');time.sleep(5);continue
                if result['state']=='budget':save('budget',reason='api_budget_exhausted');return
                if result['state'] not in {'complete','partial'}:raise RuntimeError('Unexpected statistics outcome')
                save('running',groups_done=job['groups_done']+1,requests=job['requests']+result.get('requests',1),
                     cache_hits=job['cache_hits']+int(bool(result.get('cache_hit',False))),
                     failures=job['failures']+int(result['state']=='partial'),
                     rejected_posts=job['rejected_posts']+result.get('rejected_posts',0),
                     last_group=group_id,last_observed_posts=result.get('observed_posts',0),reason='')
                break
        save('partial' if job.get('failures') else 'complete',reason='incomplete_statistics_coverage' if job.get('failures') else '')
    except api.VkApiLimited as exc:save('error',reason='vk_rate_limit',error_code=exc.code)
    except api.VkApiAuthFailure as exc:save('error',reason='vk_api_authentication',error_code=exc.code)
    except Exception as exc:
        code=getattr(exc,'code',None)
        save('error',reason='statistics_read_failed',error_type=type(exc).__name__,
             error_code=code if type(code) is int else None)


if __name__=='__main__':
    import argparse
    from vk_scheduler import singleton
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',required=True)
    args=parser.parse_args()
    with singleton(accounts.ROOT/'.vk-statistics-refresh.guard'):run(args.job)
