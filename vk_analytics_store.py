"""Separate analytics database. No actor/action database is opened here."""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

RULES_VERSION='omsu-111-2026-09-14/v1.1'
COUNTERS=('likes','comments','reposts','views')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def dumps(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False)


def open_db(path):
    path=Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10)
    db.row_factory=sqlite3.Row
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if any(not t.startswith(('analytics_','sqlite_')) for t in tables):
        db.close()
        raise ValueError('Separate analytics database required; existing foreign tables refused')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS analytics_runs(
      id TEXT PRIMARY KEY,period_start TEXT NOT NULL,period_end TEXT NOT NULL,
      started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,
      groups_checked INTEGER NOT NULL DEFAULT 0,posts_received INTEGER NOT NULL DEFAULT 0,
      errors INTEGER NOT NULL DEFAULT 0,rules_version TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS analytics_run_groups(
      run_id TEXT NOT NULL REFERENCES analytics_runs(id),group_id INTEGER NOT NULL,
      name TEXT NOT NULL,included INTEGER NOT NULL,business_card INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',pages INTEGER NOT NULL DEFAULT 0,
      next_offset INTEGER NOT NULL DEFAULT 0,reason TEXT,
      PRIMARY KEY(run_id,group_id));
    CREATE TABLE IF NOT EXISTS analytics_publications(
      group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,published_at INTEGER NOT NULL,
      url TEXT NOT NULL,post_type TEXT,is_pinned INTEGER,is_repost INTEGER NOT NULL,
      source TEXT NOT NULL,availability TEXT NOT NULL,
      PRIMARY KEY(group_id,post_id));
    CREATE TABLE IF NOT EXISTS analytics_snapshots(
      id INTEGER PRIMARY KEY,run_id TEXT NOT NULL REFERENCES analytics_runs(id),
      group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,measured_at TEXT NOT NULL,
      likes INTEGER,comments INTEGER,reposts INTEGER,views INTEGER,
      source TEXT NOT NULL,status TEXT NOT NULL,published_at INTEGER NOT NULL,
      post_type TEXT,is_pinned INTEGER,is_repost INTEGER NOT NULL,
      UNIQUE(run_id,group_id,post_id),
      FOREIGN KEY(group_id,post_id) REFERENCES analytics_publications(group_id,post_id));
    CREATE TABLE IF NOT EXISTS analytics_errors(
      id INTEGER PRIMARY KEY,run_id TEXT NOT NULL REFERENCES analytics_runs(id),
      group_id INTEGER,at TEXT NOT NULL,code TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS analytics_reports(
      id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES analytics_runs(id),
      report_month TEXT NOT NULL,variant TEXT NOT NULL,fixed_at TEXT NOT NULL,
      likes INTEGER,comments INTEGER,reposts INTEGER,views INTEGER,er REAL,p33 REAL,p32 REAL,
      completeness TEXT NOT NULL,rules_version TEXT NOT NULL,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS analytics_official(
      id INTEGER PRIMARY KEY,import_id TEXT NOT NULL,imported_at TEXT NOT NULL,
      source TEXT NOT NULL,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS analytics_capabilities(
      id INTEGER PRIMARY KEY,at TEXT NOT NULL,method TEXT NOT NULL,
      batch_size INTEGER NOT NULL,status TEXT NOT NULL,code INTEGER);
    CREATE TABLE IF NOT EXISTS analytics_page_diagnostics(
      id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,group_id INTEGER NOT NULL,
      observed_at TEXT NOT NULL,offset INTEGER NOT NULL,code TEXT NOT NULL,metadata TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS analytics_catalog_captures(
      run_id TEXT PRIMARY KEY REFERENCES analytics_runs(id),captured_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS analytics_catalog_references(
      run_id TEXT NOT NULL REFERENCES analytics_runs(id),group_id INTEGER NOT NULL,
      post_id INTEGER NOT NULL,published_at INTEGER NOT NULL,seen_at TEXT NOT NULL,
      source TEXT NOT NULL,PRIMARY KEY(run_id,group_id,post_id));
    ''')
    columns={r[1] for r in db.execute('PRAGMA table_info(analytics_run_groups)')}
    if 'expected_count' not in columns:
        db.execute('ALTER TABLE analytics_run_groups ADD COLUMN expected_count INTEGER')
    if 'observed_from' not in columns:
        db.execute('ALTER TABLE analytics_run_groups ADD COLUMN observed_from TEXT')
    db.commit()
    return db


def valid_period(start,end):
    a,b=date.fromisoformat(start),date.fromisoformat(end)
    if a>=b:raise ValueError('Period is [start,end); end must follow start')
    return a,b


def new_run(db,start,end,groups,*,run_id=None):
    valid_period(start,end)
    ids=[g['id'] for g in groups]
    if not ids or len(ids)!=len(set(ids)) or any(type(g) is not int or g<=0 for g in ids):
        raise ValueError('Nonempty registry of unique positive community IDs required')
    for g in groups:
        if any(k in g and type(g[k]) is not bool for k in ('included','business_card')):
            raise ValueError('Registry flags must be booleans')
    rid=uuid.uuid4().hex if run_id is None else run_id
    if not isinstance(rid,str) or len(rid)!=32 or any(c not in '0123456789abcdef' for c in rid):
        raise ValueError('Invalid run identity')
    existing=db.execute('SELECT period_start,period_end FROM analytics_runs WHERE id=?',(rid,)).fetchone()
    selected=sorted((g['id'],g.get('name',str(g['id'])),int(g.get('included',True)),int(g.get('business_card',False))) for g in groups)
    if existing:
        frozen=[tuple(r) for r in db.execute('SELECT group_id,name,included,business_card FROM analytics_run_groups WHERE run_id=? ORDER BY group_id',(rid,))]
        if tuple(existing)!=(start,end) or frozen!=selected:raise ValueError('Run identity selection conflict')
        return rid
    with db:
        db.execute('INSERT INTO analytics_runs(id,period_start,period_end,started_at,status,rules_version) VALUES(?,?,?,?,?,?)',
                   (rid,start,end,now(),'pending',RULES_VERSION))
        db.executemany('INSERT INTO analytics_run_groups(run_id,group_id,name,included,business_card) VALUES(?,?,?,?,?)',
            [(rid,g['id'],g.get('name',str(g['id'])),int(g.get('included',True)),int(g.get('business_card',False))) for g in groups])
    return rid


def counter(item,key):
    block=item.get(key)
    n=block.get('count') if isinstance(block,dict) else None
    return n if type(n) is int and 0<=n<2**63 else None


def capture_catalog_references(db,run,references):
    """Freeze only public exact API identities; no actor history is imported."""
    from vk_analytics import stamp
    row=db.execute('SELECT period_start,period_end FROM analytics_runs WHERE id=?',(run,)).fetchone()
    if row is None:raise ValueError('Unknown analytics run')
    if db.execute('SELECT 1 FROM analytics_catalog_captures WHERE run_id=?',(run,)).fetchone():return
    allowed={r[0] for r in db.execute('SELECT group_id FROM analytics_run_groups WHERE run_id=? AND included=1',(run,))}
    valid=[]
    for r in references:
        if (any(type(r.get(k)) is not int or not 0<r[k]<2**63 for k in ('group_id','post_id','published_at'))
            or not isinstance(r.get('seen_at'),str) or datetime.fromisoformat(r['seen_at']).tzinfo is None):
            raise ValueError('Invalid public catalog reference')
        if r['group_id'] in allowed and stamp(row['period_start'])<=r['published_at']<stamp(row['period_end']):
            valid.append((run,r['group_id'],r['post_id'],r['published_at'],r['seen_at'],'historical-api-wall.get'))
    with db:
        db.executemany('INSERT OR IGNORE INTO analytics_catalog_references VALUES(?,?,?,?,?,?)',valid)
        db.execute('INSERT INTO analytics_catalog_captures VALUES(?,?)',(run,now()))


def record_missing(db,run,p,source,*,status='not_returned'):
    """Unavailable historical post: retain unknown type/pin/counters honestly."""
    db.execute('''INSERT OR IGNORE INTO analytics_snapshots(run_id,group_id,post_id,measured_at,
      likes,comments,reposts,views,source,status,published_at,post_type,is_pinned,is_repost)
      VALUES(?,?,?,?,NULL,NULL,NULL,NULL,?,?,?,?,?,?)''',
      (run,p['group_id'],p['post_id'],now(),source,status,p['published_at'],p['post_type'],p['is_pinned'],p['is_repost']))


def observe(db,run,gid,item,source,*,status=None,at=None):
    if (not isinstance(item,dict) or type(item.get('owner_id')) is not int or item['owner_id']!=-gid
        or type(item.get('id')) is not int or not 0<item['id']<2**63
        or type(item.get('date')) is not int or not 0<item['date']<2**63
        or item['date']>int(datetime.now(timezone.utc).timestamp())+3600
        or type(item.get('is_pinned',0)) not in (int,bool) or item.get('is_pinned',0) not in (0,1)):
        raise ValueError('Invalid public post identity/date/pinned flag')
    counts=[counter(item,k) for k in COUNTERS]
    status=status or ('complete' if all(v is not None for v in counts) else 'missing_counters')
    if status!='complete' and status!='missing_counters':counts=[None]*4
    pid=item['id'];published=item['date'];kind=item.get('post_type');pin=int(item.get('is_pinned',0))
    if kind is not None and (not isinstance(kind,str) or len(kind)>40):raise ValueError('Invalid post type')
    copied=int(bool(item.get('copy_history')) or kind=='copy')
    db.execute('INSERT OR IGNORE INTO analytics_publications VALUES(?,?,?,?,?,?,?,?,?)',
        (gid,pid,published,f'https://vk.com/wall-{gid}_{pid}',kind,pin,copied,source,
         'available' if status in ('complete','missing_counters') else status))
    db.execute('''INSERT OR IGNORE INTO analytics_snapshots(run_id,group_id,post_id,measured_at,
      likes,comments,reposts,views,source,status,published_at,post_type,is_pinned,is_repost)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (run,gid,pid,at or now(),*counts,source,status,published,kind,pin,copied))


def finish(db,run):
    states=[r[0] for r in db.execute('SELECT status FROM analytics_run_groups WHERE run_id=? AND included=1',(run,))]
    status='complete' if states and all(s=='complete' for s in states) else 'partial'
    with db:
        db.execute('''UPDATE analytics_runs SET finished_at=?,status=?,
          groups_checked=(SELECT COUNT(*) FROM analytics_run_groups WHERE run_id=? AND included=1 AND pages>0),
          posts_received=(SELECT COUNT(*) FROM analytics_snapshots WHERE run_id=?),
          errors=(SELECT COUNT(*) FROM analytics_errors WHERE run_id=?) WHERE id=?''',
          (now(),status,run,run,run,run))


def freeze(db,report):
    rid=uuid.uuid4().hex;d=report['district']
    with db:
        db.execute('INSERT INTO analytics_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (rid,report['run_id'],report['period_start'][:7],report['variant'],now(),
             *(d[k] for k in COUNTERS),d['er'],d['p33'],d['p32'],d['completeness'],RULES_VERSION,dumps(report)))
    return rid


def import_official(db,rows,*,source):
    iid=uuid.uuid4().hex
    for r in rows:
        for key in (*COUNTERS,'posts','er','p33','p32','p3','p31_ratio'):
            value=r.get(key)
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or (value<0 and key!='p3')):
                raise ValueError('Invalid official numeric measurement')
            if key in (*COUNTERS,'posts') and value is not None and type(value) is not int:
                raise ValueError('Official counters must be integer counts')
        if r.get('measured_at'):
            dt=datetime.fromisoformat(r['measured_at'])
            if dt.tzinfo is None:raise ValueError('Official measurement time requires timezone')
        if r.get('as_of_date'):date.fromisoformat(r['as_of_date'])
        if r.get('period_start') and r.get('period_end'):valid_period(r['period_start'],r['period_end'])
        if r.get('group_id') is not None and (type(r['group_id']) is not int or r['group_id']<=0):
            raise ValueError('Official group_id must be positive or null for district')
        dumps(r)
    with db:
        db.executemany('INSERT INTO analytics_official(import_id,imported_at,source,payload) VALUES(?,?,?,?)',
                       [(iid,now(),source,dumps(r)) for r in rows])
    return iid
