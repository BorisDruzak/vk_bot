"""Durable all-group API refresh, one bounded page per scheduler step.

All heads are visited before continuation pages. Each fresh cycle starts at
offset zero; a process restart resumes its active cycle. Only public IDs,
timestamps and sanitized status codes are stored, never access tokens.
"""
from __future__ import annotations

import time

import vk_api_read as api
import vk_batch as engine


def init_catalog(db):
    api.init_api(db)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS catalog_cycles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,started_at TEXT NOT NULL,
        finished_at TEXT,state TEXT NOT NULL,days INTEGER NOT NULL,
        max_pages_per_group INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS catalog_cycle_groups(
        cycle_id INTEGER NOT NULL,group_id INTEGER NOT NULL,position INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',pages INTEGER NOT NULL DEFAULT 0,
        fetched INTEGER NOT NULL DEFAULT 0,recent INTEGER NOT NULL DEFAULT 0,
        next_offset INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '',
        updated_at TEXT,PRIMARY KEY(cycle_id,group_id));
    ''')


def start_cycle(db,selected,*,days=7,max_pages_per_group=10,fresh=False):
    """Start a new full refresh, or reuse its identical unfinished checkpoint."""
    if type(days) is not int or not 1<=days<=365:
        raise ValueError('days должен быть 1..365')
    if type(max_pages_per_group) is not int or not 1<=max_pages_per_group<=100:
        raise ValueError('max_pages_per_group должен быть 1..100')
    ids=[group['id'] for group in selected if group.get('enabled',True)]
    if not ids or len(ids)!=len(set(ids)) or any(type(gid) is not int or gid<=0 for gid in ids):
        raise ValueError('Нужен непустой список уникальных положительных ID групп')
    init_catalog(db)
    active=db.execute("SELECT * FROM catalog_cycles WHERE state='running' ORDER BY id DESC LIMIT 1").fetchone()
    if active:
        previous=[row['group_id'] for row in db.execute(
            'SELECT group_id FROM catalog_cycle_groups WHERE cycle_id=? ORDER BY position',(active['id'],))]
        if not fresh:
            if previous!=ids or active['days']!=days or active['max_pages_per_group']!=max_pages_per_group:
                raise ValueError('Параметры активного API-цикла изменились; нужен явный fresh=True')
            return active['id']
        db.execute("UPDATE catalog_cycles SET state='superseded',finished_at=? WHERE id=?",(engine.moment(),active['id']))
    cursor=db.execute("INSERT INTO catalog_cycles(started_at,state,days,max_pages_per_group) VALUES(?,'running',?,?)",
                      (engine.moment(),days,max_pages_per_group))
    cycle=cursor.lastrowid
    db.executemany('INSERT INTO catalog_cycle_groups(cycle_id,group_id,position) VALUES(?,?,?)',
                   [(cycle,gid,position) for position,gid in enumerate(ids)])
    db.commit()
    return cycle


def coverage(db,cycle_id):
    init_catalog(db)
    cycle=db.execute('SELECT * FROM catalog_cycles WHERE id=?',(cycle_id,)).fetchone()
    if cycle is None:
        raise ValueError('Неизвестный API-цикл')
    result=dict(cycle_id=cycle_id,state=cycle['state'],selected=0,complete=0,
                partial=0,error=0,pending=0,heads_checked=0,pages=0,fetched=0)
    for row in db.execute('SELECT * FROM catalog_cycle_groups WHERE cycle_id=?',(cycle_id,)):
        result['selected']+=1
        result['heads_checked']+=int(row['state']!='pending')
        result['pages']+=row['pages'];result['fetched']+=row['fetched']
        result['partial' if row['state'] in ('partial','incomplete') else row['state']]+=1
    return result


def _finish_if_done(db,cycle_id):
    remaining=db.execute("SELECT 1 FROM catalog_cycle_groups WHERE cycle_id=? AND state IN ('pending','partial') LIMIT 1",
                         (cycle_id,)).fetchone()
    if not remaining:
        other=db.execute("SELECT 1 FROM catalog_cycle_groups WHERE cycle_id=? AND state!='complete' LIMIT 1",
                         (cycle_id,)).fetchone()
        db.execute('UPDATE catalog_cycles SET state=?,finished_at=? WHERE id=?',
                   ('partial' if other else 'complete',engine.moment(),cycle_id))
    db.commit()


def step(db,token,cycle_id,*,daily_limit=2000,pause_seconds=20,
         transport=None,sleeper=time.sleep,clock=time.time):
    """At most one request. Terminal cycle returns None; depleted budget pauses.

    The scheduler's explicit daily API ceiling is independent of action quotas;
    old callers retain VKReadClient defaults 3/run and30/24h. HTTP429/rate codes
    stop and latch. Invalid key stops; denied group code7 is recorded and the
    next *different* group remains eligible. No failed group is retried here.
    """
    if type(daily_limit) is not int or not 1<=daily_limit<=5000:
        raise ValueError('daily_limit должен быть 1..5000')
    if not 5<=pause_seconds<=300:
        raise ValueError('Пауза каталога должна быть 5..300 секунд')
    init_catalog(db)
    cycle=db.execute('SELECT * FROM catalog_cycles WHERE id=?',(cycle_id,)).fetchone()
    if cycle is None:
        raise ValueError('Неизвестный API-цикл')
    if cycle['state']!='running':
        return None
    engine.require_platform_unlocked(db)
    row=db.execute("""SELECT * FROM catalog_cycle_groups WHERE cycle_id=?
      AND state IN ('pending','partial') ORDER BY pages,position LIMIT 1""",(cycle_id,)).fetchone()
    if row is None:
        _finish_if_done(db,cycle_id)
        return None
    client=api.VKReadClient(db,token,pause_seconds=pause_seconds,per_run=1,
        per_24h=daily_limit,transport=transport,sleeper=sleeper,clock=clock)
    progress=api._progress(db,row['group_id'])
    cache_offset=(progress['next_offset'] if row['state']!='pending' and progress
                  and progress['status']=='partial' and progress['reason'] not in api.PARTIAL_RETRY_LATER else 0)
    if client.remaining()<1 and not client.has_cached_wall(row['group_id'],count=api.PAGE_SIZE,offset=cache_offset):
        return dict(group_id=row['group_id'],status='budget',requests=0,coverage=coverage(db,cycle_id))
    gid=row['group_id']
    try:
        result=api.scan_one_group(db,client,dict(id=gid),days=cycle['days'],max_pages=1,
                                 restart=row['state']=='pending')
    except api.VkApiTransientRead:
        # A transport outage is not evidence that this group is inaccessible.
        # Keep its cursor/state and let the worker apply bounded read backoff.
        raise
    except api.VkApiFailure as exc:
        # Never store arbitrary exception text (a transport may contain secrets).
        reason='api-code-'+str(exc.code) if type(exc.code) is int else type(exc).__name__
        api.mark_api_group_error(db,gid,reason)
        db.execute("""UPDATE catalog_cycle_groups SET state='error',reason=?,updated_at=?
          WHERE cycle_id=? AND group_id=?""",(reason,engine.moment(),cycle_id,gid))
        global_stop=isinstance(exc,api.VkApiLimited) or (isinstance(exc,api.VkApiAuthFailure) and exc.code!=7)
        if global_stop:
            db.execute("UPDATE catalog_cycles SET state='stopped',finished_at=? WHERE id=?",(engine.moment(),cycle_id))
        db.commit()
        if global_stop:
            raise
        _finish_if_done(db,cycle_id)
        return dict(group_id=gid,status='error',reason=reason,requests=client.used,coverage=coverage(db,cycle_id))
    pages=row['pages']+result['pages']
    resumable=(result['status']=='partial' and result['next_offset']>0 and
               result['reason'] not in api.PARTIAL_RETRY_LATER and pages<cycle['max_pages_per_group'])
    state='complete' if result['status']=='complete' else 'partial' if resumable else 'incomplete'
    reason='cycle-page-cap' if state=='incomplete' and pages>=cycle['max_pages_per_group'] else result['reason']
    db.execute('''UPDATE catalog_cycle_groups SET state=?,pages=?,fetched=fetched+?,
      recent=?,next_offset=?,reason=?,updated_at=? WHERE cycle_id=? AND group_id=?''',
      (state,pages,result['fetched'],result['recent'],result['next_offset'],reason,engine.moment(),cycle_id,gid))
    db.commit();_finish_if_done(db,cycle_id)
    return {**result,'requests':client.used,'cycle_group_state':state,'coverage':coverage(db,cycle_id)}
