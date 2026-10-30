"""Fresh pre-batch activity and conservative overlays of our verified actions."""
from __future__ import annotations

import sqlite3
import subprocess
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

import vk_api_read as api
import vk_batch as engine
import vk_schedule_policy as policy
import vk_unified as app
import vk_groups as public_groups
import vk_diagnostics as diagnostics
import vk_read_trace as read_trace


def _public_read_failure_chain(exc):
    """Fail closed on hidden guards, codes, unknown wrappers or cyclic chains."""
    pending=[exc];seen=set();chain=[]
    while pending:
        current=pending.pop()
        if id(current) in seen or len(seen)>=8:return None
        seen.add(id(current));chain.append(current)
        # TimeoutExpired is the suppressed context of the real bounded helper.
        # No generic exception class or action/platform guard grants recovery.
        if type(current) not in (api.VkApiReadDeadline,api.VkApiTransientRead,subprocess.TimeoutExpired):return None
        if getattr(current,'code',None) is not None:return None
        detail=read_trace.sanitize_summary(getattr(current,'read_diagnostics',None))
        if ('vk_error_code' in detail or
                ('http_status' in detail and 400<=detail['http_status']<500)):
            return None
        # Inspect suppressed contexts too: a wrapper must not hide a guard.
        for linked in (current.__cause__,current.__context__):
            if linked is not None:pending.append(linked)
    return chain


def _retryable_public_read(exc,chain):
    if type(exc) not in (api.VkApiReadDeadline,api.VkApiTransientRead) or chain is None:return False
    network_phases={'dns','tcp_connect','tls_handshake','request_send',
                    'response_headers','response_body','http_open'}
    network_errors={'helper_deadline','dns_error','connection_refused','connection_reset',
                    'network_unreachable','timeout','transport_error'}
    for current in chain:
        detail=read_trace.sanitize_summary(getattr(current,'read_diagnostics',None))
        if current is exc or detail:
            if (detail.get('read_phase') not in network_phases or
                    detail.get('read_error') not in network_errors):return False
    return True


def _table(db,name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


def local_activity(paths):
    """One like/personal repost per actor; community reposts per destination.

    Only verified results with a durable attempt count. Unknown outcomes still
    consume quotas elsewhere, but never masquerade as confirmed engagement.
    """
    result=defaultdict(lambda:{'like':[],'repost':[]})
    for path in paths.values():
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            clubs=_table(db,'community_run_attempts')
            for action in ('like','repost'):
                exclude=(' AND NOT EXISTS(SELECT 1 FROM community_run_attempts c WHERE c.attempt_id=a.id)'
                         if clubs and action=='repost' else '')
                rows=db.execute(f'''SELECT p.group_id,p.post_id,MAX(a.attempted_at)
                    FROM posts p JOIN action_attempts a USING(group_id,post_id)
                    WHERE p.{action}_status='verified' AND a.action=? {exclude}
                    GROUP BY p.group_id,p.post_id''',(action,))
                for gid,pid,at in rows:result[(gid,pid)][action].append(at)
            if clubs and _table(db,'community_reposts'):
                rows=db.execute('''SELECT p.group_id,p.post_id,MAX(a.attempted_at)
                    FROM community_reposts p JOIN community_runs r ON r.community_id=p.community_id
                    JOIN community_run_attempts c ON c.run_id=r.id
                    JOIN action_attempts a ON a.id=c.attempt_id AND a.group_id=p.group_id AND a.post_id=p.post_id
                    WHERE p.status='verified' AND a.action='repost'
                    GROUP BY p.community_id,p.group_id,p.post_id''')
                for gid,pid,at in rows:result[(gid,pid)]['repost'].append(at)
    return dict(result)


def effective_counts(row,own):
    result=dict(row)
    events=own.get((row['group_id'],row['post_id']),{})
    try:observed=datetime.fromisoformat(row['observed_at'])
    except (TypeError,ValueError,KeyError):observed=None
    for action in ('like','repost'):
        field=action+'_count';raw=row.get(field);dates=events.get(action,[]);delta=0
        if observed is not None and observed.tzinfo is not None:
            for value in dates:
                try:delta+=int(datetime.fromisoformat(value)>observed)
                except (TypeError,ValueError):pass
        result['raw_'+field]=raw
        result['own_'+action+'_delta']=delta
        result['own_'+action+'_floor']=len(dates)
        if type(raw) is int and raw>=0:
            result[field]=max(raw+delta,len(dates))
    return result


def _observe_page(db,gid,items,now,*,observed_at=None):
    """Install exact public observations without changing catalog cursors."""
    stamp=engine.moment() if observed_at is None else observed_at
    try:
        observed=datetime.fromisoformat(stamp)
        if observed.tzinfo is None:raise ValueError('Missing timezone')
        stamp=observed.astimezone(timezone.utc).isoformat()
    except (TypeError,ValueError):
        raise api.VkApiFailure('Invalid public observation timestamp') from None
    accepted=set()
    for item in items:
        if not api._valid_post(item,gid,now) or item['id'] in accepted:continue
        pid=item['id'];accepted.add(pid)
        engine.add_posts(db,gid,[pid])
        db.execute('''INSERT INTO api_seen_posts(group_id,post_id,published_at,seen_at)
            VALUES(?,?,?,?) ON CONFLICT(group_id,post_id) DO UPDATE SET
            valid=CASE WHEN api_seen_posts.published_at=excluded.published_at THEN api_seen_posts.valid ELSE 0 END,
            seen_at=excluded.seen_at''',(gid,pid,item['date'],stamp))
        previous=db.execute('SELECT observed_at FROM api_post_engagement WHERE group_id=? AND post_id=?',
                            (gid,pid)).fetchone()
        try:
            previous_at=datetime.fromisoformat(previous['observed_at']) if previous else None
            newer=previous_at is not None and previous_at.tzinfo is not None and previous_at>observed
        except (TypeError,ValueError):newer=False
        if not newer:
            db.execute('''INSERT INTO api_post_engagement
                (group_id,post_id,like_count,repost_count,comment_count,view_count,observed_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(group_id,post_id) DO UPDATE SET like_count=excluded.like_count,
                repost_count=excluded.repost_count,comment_count=excluded.comment_count,
                view_count=excluded.view_count,observed_at=excluded.observed_at''',
                (gid,pid,api._engagement_count(item,'likes'),api._engagement_count(item,'reposts'),
                 api._engagement_count(item,'comments'),api._engagement_count(item,'views'),stamp))
        public_groups.save_date(db,gid,pid,item['date'],api.API_SOURCE,True,date_checked=True)
    db.commit()
    return accepted


def _pending_eligible(state,source_path,groups,now):
    """Read exact pending recovery sources, including quota-free confirmation.

    Pending attempts are deliberately absent from ordinary State.candidates.
    This adds only public sources for fresh counters, never action permission.
    """
    import vk_action_retry as retry
    book=retry.RetryLedger(state)
    if not book.enabled or not book.exists():return {}
    selected={g['id']:g for g in groups if g.get('enabled',True)}
    jobs=state.db.execute('''SELECT j.* FROM scheduler_jobs j
        JOIN scheduler_action_recovery r ON r.job_id=j.id
        WHERE r.state='pending' AND j.status IN ('failed','uncertain') AND j.day=?''',
        (str(policy.local_day(now)),)).fetchall()
    result={}
    with closing(sqlite3.connect(Path(source_path).resolve().as_uri()+'?mode=ro',uri=True)) as source:
        source.row_factory=sqlite3.Row
        for job in jobs:
            account,gid,pid=job['account'],job['group_id'],job['post_id']
            cfg=selected.get(gid);record=book.row(job['id'])
            if (account not in state.paths or not state.account_enabled(account) or not cfg
                    or job['kind'] not in ('like','personal','community')
                    or not cfg.get('like_enabled' if job['kind']=='like' else 'repost_enabled')
                    or policy.classify_post(job['published_at'],now,state.config) is None
                    or record['identity']!=retry._identity(job) or record['current_attempt']!=job['attempt_id']
                    or record['reason']!=job['reason'] or record['reason'] not in retry.SAFE_REASONS
                    or record['passes'] not in (1,2)):
                continue
            execution=state.db.execute('SELECT outcome,intent,attempt_id FROM scheduler_action_passes WHERE job_id=? AND pass=?',
                                       (job['id'],record['passes'])).fetchone()
            if not execution:continue
            if record['passes']==2 and tuple(execution)!=('deferred',0,None):continue
            if job['status']=='uncertain':
                if not book.exempts(job):continue
            elif (job['attempt_id'] is not None or
                  (record['passes']==1 and tuple(execution)!=('failed',0,None))):continue
            if state.db.execute('SELECT 1 FROM scheduler_post_isolations WHERE account=? AND group_id=? AND post_id=?',
                                (account,gid,pid)).fetchone():continue
            row=source.execute('''SELECT a.group_id,a.post_id,a.published_at FROM api_seen_posts a
                JOIN post_publication d USING(group_id,post_id)
                LEFT JOIN api_group_scan_progress s USING(group_id)
                WHERE a.group_id=? AND a.post_id=? AND a.published_at=? AND a.valid=1
                AND d.exact=1 AND d.published_at=a.published_at AND (s.status IS NULL OR s.status!='error')''',
                (gid,pid,job['published_at'])).fetchone()
            if row:result[(gid,pid)]={**dict(row),'retry_pending':True}
    return result


def _eligible(state,source_path,groups,now):
    result={}
    context=state.selection_context(now,source_path,groups)
    for (account,kind,target),left in state.remaining(now).items():
        if left:
            for row in state.candidates(now,account,kind,target,source_path,groups,context=context):
                result[(row['group_id'],row['post_id'])]={**row,'ordinary_eligible':True}
    for key,row in _pending_eligible(state,source_path,groups,now).items():
        result[key]={**result.get(key,{}),**row}
    return result


def refresh(state,source_path,groups,settings,*,clock,transport=None,sleeper=time.sleep,progress=None,reserve=False,deadline=None,diagnostic=None):
    """Refresh the highest eligible age layer plus exact pending recovery sources.

    This key supports wall.get, not wall.getById. Read group heads and bounded
    continuation pages with the existing request ledger/pacing. Only this
    refresh's exact, fully known observations enter the action pool. Pending
    recovery may cross layers; this grants no quota or additional send. No actions,
    batch claims, warning acknowledgements, or profile operations occur here.
    reserve=True explicitly refreshes the old layer for the remaining batch
    places. Fresh new posts discovered by that read retain their priority.
    """
    started=clock().astimezone(timezone.utc).isoformat();day=policy.local_day(clock())
    report={'started_at':started,'groups':0,'requests':0,'unknown_counts':0,'missing_posts':0}
    if deadline is not None:
        if deadline.tzinfo is None:raise ValueError('Refresh deadline must be timezone aware')
        report['deadline_at']=deadline.astimezone(timezone.utc).isoformat()
    deadline_hit=False
    class RefreshStopped(Exception):pass
    def finish(status,pool=()):
        if status=='stopped' and deadline_hit:status='deadline'
        report.update(finished_at=engine.moment(),eligible_posts=len(pool))
        return {'state':status,'pool':set(pool),'report':report}
    def permitted():
        nonlocal deadline_hit
        for account in state.paths:
            with state.account_db(account) as account_db:
                account_db.row_factory=sqlite3.Row
                engine.require_platform_unlocked(account_db)
        if (state.get('stop_requested',False) or state.blocked()
                or policy.local_day(clock())!=day or not policy.in_work_window(clock(),state.config)):return False
        if deadline is not None and clock()>=deadline:
            deadline_hit=True;return False
        return True
    def paced_sleep(seconds):
        nonlocal deadline_hit
        if not permitted():raise RefreshStopped()
        # Do not shorten API pacing. Stop before a wait that leaves no room for
        # the existing20s network timeout; no request has been reserved yet.
        if deadline is not None and clock().timestamp()+seconds+20>=deadline.timestamp():
            deadline_hit=True;raise RefreshStopped()
        sleeper(seconds)
        if not permitted():raise RefreshStopped()
    def before_request():
        nonlocal deadline_hit
        if not permitted():raise RefreshStopped()
        if deadline is not None and clock().timestamp()+20>=deadline.timestamp():
            deadline_hit=True;raise RefreshStopped()
    def read_page(db,gid,offset):
        nonlocal deadline_hit
        previous_failure=None
        for attempt in range(1,4):
            report.update(read_attempt=attempt,read_attempt_limit=3)
            report.pop('read_retry_wait_until',None)
            if not permitted():
                if previous_failure is not None and deadline_hit:raise previous_failure
                return 'stopped',None
            client=api.VKReadClient(db,token,per_run=1,per_24h=settings['daily_requests'],
                pause_seconds=settings['pause_seconds'],transport=transport or (lambda p:api.bounded_transport(p,diagnostic=diagnostic)),sleeper=paced_sleep,
                clock=lambda:clock().timestamp(),fresh_after=started,before_request=before_request)
            if client.remaining()<1 and not client.has_cached_wall(gid,count=api.PAGE_SIZE,offset=offset):
                if previous_failure is not None:raise previous_failure
                return 'budget',None
            if previous_failure is not None:
                delay=max(5,settings['pause_seconds'])*(attempt-1)
                # An unfinished technical read remains a technical failure when
                # another full request cannot fit. Never convert it to success,
                # a partial pool, or the benign pre-request cutoff result.
                if deadline is not None and clock().timestamp()+delay+20>=deadline.timestamp():
                    raise previous_failure
                report['read_retry_wait_until']=(clock()+timedelta(seconds=delay)).astimezone(timezone.utc).isoformat()
                diagnostics.emit(diagnostic,'public_read_retry',source_group=gid,
                    reason=type(previous_failure).__name__,read_attempt=attempt,read_attempt_limit=3,
                    retry_delay_seconds=int(delay),
                    **read_trace.sanitize_summary(getattr(previous_failure,'read_diagnostics',None)))
                if progress:progress(dict(report))
                left=delay
                try:
                    while left>0:
                        if not permitted():
                            if deadline_hit:raise previous_failure
                            return 'stopped',None
                        if deadline is not None and clock().timestamp()+left+20>=deadline.timestamp():
                            raise previous_failure
                        step=min(5,left);sleeper(step);left-=step
                        if not permitted():
                            if deadline_hit:raise previous_failure
                            return 'stopped',None
                    if deadline is not None and clock().timestamp()+20>=deadline.timestamp():
                        raise previous_failure
                finally:
                    report.pop('read_retry_wait_until',None)
            if progress:progress(dict(report))
            if (previous_failure is not None and client.remaining()<1 and
                    not client.has_cached_wall(gid,count=api.PAGE_SIZE,offset=offset)):
                raise previous_failure
            failure=None
            try:
                payload=client.wall_get(gid,count=api.PAGE_SIZE,offset=offset)
            except api.VkApiTransientRead as exc:
                failure=exc
            except RefreshStopped:
                if previous_failure is not None and deadline_hit:raise previous_failure
                return 'stopped',None
            finally:
                report['requests']+=client.used
            if failure is not None:
                chain=_public_read_failure_chain(failure)
                if (type(failure) is api.VkApiReadDeadline and chain is not None
                        and deadline is not None and clock()>=deadline):
                    deadline_hit=True;return 'stopped',None
                if attempt==3 or not _retryable_public_read(failure,chain):
                    if progress:progress(dict(report))
                    raise failure
                previous_failure=failure
                continue
            for key in ('read_attempt','read_attempt_limit','read_retry_wait_until'):
                report.pop(key,None)
            if progress:progress(dict(report))
            return 'ok',payload
    if not permitted():return finish('stopped')
    candidates=_eligible(state,source_path,groups,clock())
    adaptive=state.config.get('schedule_mode')=='adaptive'
    if not candidates and not adaptive:return finish('empty')
    # Pending-only sources must not steer the layer for new logical actions.
    # When there are no ordinary candidates, use pending ages for the read itself.
    ordinary=[r for r in candidates.values() if r.get('ordinary_eligible')]
    band='new' if not reserve and any(policy.classify_post(r['published_at'],clock(),state.config)=='new'
                      for r in (ordinary or list(candidates.values()))) else 'old'
    wanted=defaultdict(set)
    for key,row in candidates.items():
        if row.get('retry_pending') or policy.classify_post(row['published_at'],clock(),state.config)==band:
            wanted[key[0]].add(key[1])
    if adaptive:
        # Adaptive dispatch reserves a full allowlist refresh instead of idle
        # catalog scanning. Exhausted/empty groups must still discover new posts.
        # Empty wanted IDs read one fresh first page through the same client;
        # known candidates retain the existing bounded pagination and guards.
        for group in groups:
            if group.get('enabled',True):wanted.setdefault(group['id'],set())
    report.update(band=band,requested_posts=sum(map(len,wanted.values())))
    band_floor=policy.day_bounds(clock())[0]-(state.config['old_days']*86400 if band=='old' else 0)
    band_floor=min([band_floor]+[r['published_at'] for r in candidates.values() if r.get('retry_pending')])
    token=api.configured_token()
    if not token:raise api.VkApiAuthFailure('No local API key configured')
    observed_keys=set()
    with app.exclusive_run(),closing(engine.db_open(source_path)) as db:
        api.init_api(db)
        for gid,pids in sorted(wanted.items()):
            denied=state.get('counter_denied_until',{})
            if denied.get(str(gid),0)>clock().timestamp():
                report.setdefault('denied_groups',[]).append(gid);continue
            report['groups']+=1
            offset=0;total=None;seen=set();last_unpinned=None;ordered=True
            for page in range(settings['pages_per_group']):
                if not permitted():return finish('stopped')
                try:
                    read_status,payload=read_page(db,gid,offset)
                except api.VkApiAuthFailure as exc:
                    if exc.code!=7:raise
                    denied[str(gid)]=clock().timestamp()+3600
                    state.set('counter_denied_until',denied)
                    report.setdefault('denied_groups',[]).append(gid)
                    observed_keys={key for key in observed_keys if key[0]!=gid}
                    break
                if read_status!='ok':return finish(read_status)
                if not permitted():return finish('stopped')
                count=payload.get('count');items=payload['items']
                if count is not None and (type(count) is not int or count<0):
                    raise api.VkApiFailure('Invalid wall count during counter refresh')
                if len(items)>api.PAGE_SIZE:raise api.VkApiFailure('Oversized counter refresh page')
                if page and total is not None and count!=total:break
                total=count
                ids={item['id'] for item in items if api._valid_post(item,gid,int(clock().timestamp()))}
                if seen & ids:break
                observed_at=payload.get('_observed_at')
                if observed_at is None:observed_at=clock().astimezone(timezone.utc).isoformat()
                accepted=_observe_page(db,gid,items,int(clock().timestamp()),observed_at=observed_at)
                seen.update(accepted)
                if datetime.fromisoformat(observed_at)>=datetime.fromisoformat(started):
                    observed_keys.update((gid,pid) for pid in accepted)
                if pids<=seen:break
                valid=all(api._valid_post(item,gid,int(clock().timestamp())) for item in items)
                dates=[item['date'] for item in items if api._valid_post(item,gid,int(clock().timestamp())) and not api._pinned(item)]
                for stamp in dates:
                    if last_unpinned is not None and stamp>last_unpinned:ordered=False
                    last_unpinned=stamp
                if valid and ordered and dates and dates[-1]<band_floor:
                    report['older_boundary_groups']=report.get('older_boundary_groups',0)+1
                    break
                offset+=len(items)
                if len(items)<api.PAGE_SIZE or (total is not None and offset>=total):break
        fresh=[dict(r) for r in db.execute('''SELECT a.group_id,a.post_id,a.published_at,
            e.like_count,e.repost_count,e.comment_count FROM api_seen_posts a
            JOIN post_publication d USING(group_id,post_id)
            JOIN api_post_engagement e USING(group_id,post_id)
            WHERE a.valid=1 AND d.exact=1 AND d.published_at=a.published_at AND e.observed_at>=?''',(started,))
            if (r['group_id'],r['post_id']) in observed_keys]
    if not permitted():return finish('stopped')
    known={}
    observed={(r['group_id'],r['post_id']) for r in fresh}
    for row in fresh:
        key=(row['group_id'],row['post_id'])
        if all(type(row[c]) is int and row[c]>=0 for c in ('like_count','repost_count','comment_count')):
            known[key]=row
        else:report['unknown_counts']+=1
    report['missing_posts']=len({(gid,pid) for gid,pids in wanted.items() for pid in pids}-observed)
    # Newly discovered today's posts take precedence even if preparation began
    # in the reserve layer. Never fall back to unrefreshed reserve mid-batch.
    eligible=_eligible(state,source_path,groups,clock())
    known={key:row for key,row in known.items() if key in eligible}
    has_new=any(eligible[key].get('ordinary_eligible') and
                              policy.classify_post(row['published_at'],clock(),state.config)=='new'
                              for key,row in known.items())
    # A full old-layer scan may discover today's new posts. Keep the reserve
    # observations from that SAME fresh scan instead of reading every group twice.
    selected_band='mixed' if reserve or (band=='old' and has_new) else 'new' if has_new else band
    # Recovery may cross age layers, but only this response's exact known rows
    # qualify. The explicit reserve refill admits both freshly observed layers;
    # policy priority still places today's posts before reserve posts.
    pool={key for key,r in known.items() if eligible[key].get('retry_pending') or
          (eligible[key].get('ordinary_eligible') and
           (selected_band=='mixed' or policy.classify_post(r['published_at'],clock(),state.config)==selected_band))}
    report['selected_band']=selected_band
    return finish('ready' if pool else 'empty',pool)
