"""Durable day plans, quota accounting and crash-safe scheduler intents."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path

import vk_schedule_policy as policy
import vk_batch_engagement as engagement
import vk_diagnostics as diagnostics


class BatchSpacingWait(RuntimeError):pass


class State:
    def __init__(self,path,account_paths,config=None):
        self.config=policy.validate_config(config)
        self.paths={int(k):Path(v) for k,v in account_paths.items()}
        self.db=sqlite3.connect(path,timeout=20)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS schedule_days(day TEXT PRIMARY KEY,config TEXT NOT NULL,slots TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS scheduler_batches(day TEXT NOT NULL,slot INTEGER NOT NULL,
            started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,PRIMARY KEY(day,slot));
          CREATE TABLE IF NOT EXISTS scheduler_jobs(id INTEGER PRIMARY KEY,day TEXT NOT NULL,slot INTEGER NOT NULL,
            account INTEGER NOT NULL,kind TEXT NOT NULL,community_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,published_at INTEGER NOT NULL,
            status TEXT NOT NULL,created_at TEXT NOT NULL,attempt_id INTEGER,baseline INTEGER,
            reason TEXT NOT NULL DEFAULT '',UNIQUE(day,account,kind,community_id,group_id,post_id));
          CREATE TABLE IF NOT EXISTS scheduler_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS scheduler_diagnostics(id INTEGER PRIMARY KEY,at TEXT NOT NULL,event TEXT NOT NULL,snapshot TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS scheduler_priority_evidence(job_id INTEGER PRIMARY KEY,snapshot TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS scheduler_skip_evidence(job_id INTEGER PRIMARY KEY,snapshot TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS scheduler_post_isolations(
            account INTEGER NOT NULL,group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL UNIQUE,attempt_id INTEGER NOT NULL,reason TEXT NOT NULL,
            authorized_at TEXT NOT NULL,authorization TEXT NOT NULL,
            PRIMARY KEY(account,group_id,post_id));
        ''')
        self.db.commit()
        import vk_action_retry
        vk_action_retry.init_schema(self.db)

    def close(self):self.db.close()

    def refresh_policy(self,now):
        """Keep any persisted day immutable; apply pending quotas on an unplanned day."""
        provider=getattr(self,'policy_provider',None)
        if provider is None:return
        paths_provider=getattr(self,'paths_provider',None)
        if paths_provider:self.paths.update({int(k):Path(v) for k,v in paths_provider().items()})
        row=self.db.execute('SELECT config FROM schedule_days WHERE day=?',(str(policy.local_day(now)),)).fetchone()
        self.config=policy.validate_config(json.loads(row[0]) if row else provider())

    def account_enabled(self,account):
        provider=getattr(self,'enabled_provider',None)
        return provider(account) if provider else True

    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM scheduler_meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value):
        self.db.execute('INSERT INTO scheduler_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key,json.dumps(value,ensure_ascii=False)))
        self.db.commit()

    def pause(self,reason):self.set('paused',str(reason))

    def log_event(self,event,now,**data):
        try:
            record=diagnostics.make_record(event,now=now,**data)
            diagnostics.write(record)
            if self.db.in_transaction:return
            try:
                with self.db:
                    self.db.execute('INSERT INTO scheduler_diagnostics(at,event,snapshot) VALUES(?,?,?)',
                                    (record['at'],record['event'],json.dumps(record,ensure_ascii=False)))
                    if event in ('action_failed','scheduler_failed'):
                        self.db.execute('INSERT INTO scheduler_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                        ('last_diagnostic_error',json.dumps(record,ensure_ascii=False)))
            except Exception:diagnostics.write(diagnostics.make_record('diagnostics_write_failed',now=now))
        except Exception:pass

    def blocked(self):
        return bool(self.get('paused') or self.active_uncertain_count())

    def active_uncertain_count(self):
        rows=self.db.execute('''SELECT j.* FROM scheduler_jobs j WHERE j.status='uncertain'
          AND NOT EXISTS(SELECT 1 FROM scheduler_post_isolations i WHERE i.job_id=j.id
            AND i.account=j.account AND i.group_id=j.group_id AND i.post_id=j.post_id
            AND i.attempt_id=j.attempt_id AND i.reason=j.reason AND j.kind='like'
            AND j.community_id=0 AND i.authorization='explicit_user_post_isolation')''').fetchall()
        import vk_action_retry
        book=vk_action_retry.RetryLedger(self)
        return sum(not book.exempts(j) for j in rows)

    def require_not_isolated(self,account,group_id,post_id):
        if self.db.execute('SELECT 1 FROM scheduler_post_isolations WHERE account=? AND group_id=? AND post_id=?',
                           (account,group_id,post_id)).fetchone():
            raise RuntimeError('Post permanently isolated by explicit user instruction')

    def isolate_uncertain_like(self,job,now,*,authorization):
        """Operator-only exception; never invoked by the worker or a failure handler.

        Preserve the uncertain job and actor ledger verbatim. No automatic unpause,
        successful result, new attempt, or permission for other uncertain jobs.
        """
        if authorization!='explicit_user_post_isolation':raise ValueError('Explicit user authorization required')
        row=self.db.execute('SELECT * FROM scheduler_jobs WHERE id=?',(job,)).fetchone()
        if (not row or row['status']!='uncertain' or row['kind']!='like' or row['community_id']!=0
                or row['attempt_id'] is None or row['reason'] not in
                ('action_stopped_personal_action','like_confirmation_timeout')):
            raise RuntimeError('No supported uncertain like to isolate')
        with self.account_db(row['account']) as db:
            attempt=db.execute('SELECT group_id,post_id,action FROM action_attempts WHERE id=?',
                               (row['attempt_id'],)).fetchone()
            post=db.execute('SELECT like_status FROM posts WHERE group_id=? AND post_id=?',
                            (row['group_id'],row['post_id'])).fetchone()
        if attempt!=(row['group_id'],row['post_id'],'like') or post!=('uncertain',):
            raise RuntimeError('Uncertain actor evidence does not match job')
        self.db.execute('INSERT INTO scheduler_post_isolations VALUES(?,?,?,?,?,?,?,?)',
            (row['account'],row['group_id'],row['post_id'],job,row['attempt_id'],row['reason'],
             now.isoformat(),authorization));self.db.commit()

    def day_plan(self,now):
        day=str(policy.local_day(now));config=json.dumps(self.config,sort_keys=True)
        row=self.db.execute('SELECT * FROM schedule_days WHERE day=?',(day,)).fetchone()
        if row:
            if row['config']!=config:raise RuntimeError('Daily policy changed; existing plan is retained')
            return [datetime.fromisoformat(t) for t in json.loads(row['slots'])]
        slots=policy.build_schedule(now,self.config)
        self.db.execute('INSERT OR IGNORE INTO schedule_days VALUES(?,?,?)',
                        (day,config,json.dumps([t.isoformat() for t in slots])))
        self.db.commit()
        return self.day_plan(now)

    def claim_batch(self,now,slot):
        day=str(policy.local_day(now))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            saved=self.db.execute('SELECT slots FROM schedule_days WHERE day=?',(day,)).fetchone()
            last=self.db.execute('SELECT started_at,finished_at FROM scheduler_batches ORDER BY started_at DESC LIMIT 1').fetchone()
            now=self.fresh_time(now)
            if str(policy.local_day(now))!=day:
                self.db.rollback();return False
            if saved:
                slots=[datetime.fromisoformat(t) for t in json.loads(saved[0])]
                if (type(slot) is not int or not 0<=slot<len(slots) or not
                        slots[slot]<=now<=slots[slot]+timedelta(seconds=policy.MAX_JITTER_SECONDS)):
                    self.db.rollback();return False
            if last:
                if (now-datetime.fromisoformat(last['started_at'])).total_seconds()<policy.MIN_BATCH_GAP_SECONDS:
                    self.db.rollback();return False
                if last['finished_at'] and (now-datetime.fromisoformat(last['finished_at'])).total_seconds()<20:
                    self.db.rollback();return False
            cur=self.db.execute("INSERT OR IGNORE INTO scheduler_batches VALUES(?,?,?,NULL,'running')",
                                (str(policy.local_day(now)),slot,now.isoformat()))
            self.db.commit()
        except BaseException:self.db.rollback();raise
        return cur.rowcount==1

    def finish_batch(self,now,slot,status,*,day=None):
        self.db.execute('UPDATE scheduler_batches SET finished_at=?,status=? WHERE day=? AND slot=?',
                        (now.isoformat(),status,str(policy.local_day(now) if day is None else day),slot));self.db.commit()

    def account_db(self,account):
        return closing(sqlite3.connect(self.paths[account].resolve().as_uri()+'?mode=ro',uri=True,timeout=20))

    @staticmethod
    def table(db,name):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())

    def usage(self,now):
        start,end=policy.day_bounds(now)
        bounds=[datetime.fromtimestamp(t,timezone.utc).isoformat(timespec='seconds') for t in (start,end)]
        used=Counter()
        for account in self.paths:
            with self.account_db(account) as db:
                for aid,action in db.execute('SELECT id,action FROM action_attempts WHERE attempted_at>=? AND attempted_at<?',bounds):
                    community=0
                    if action=='repost' and self.table(db,'community_run_attempts'):
                        row=db.execute('''SELECT r.community_id FROM community_run_attempts c
                          JOIN community_runs r ON r.id=c.run_id WHERE c.attempt_id=?''',(aid,)).fetchone()
                        if row:community=row[0]
                    kind='like' if action=='like' else 'community' if community else 'personal'
                    used[(account,kind,community)]+=1
        for row in self.db.execute("SELECT account,kind,community_id FROM scheduler_jobs WHERE day=? AND attempt_id IS NULL AND status IN ('attempting','uncertain')",
                                   (str(policy.local_day(now)),)):
            used[tuple(row)]+=1
        # A second pass keeps the first attempt pointer. Its separately durable
        # intent also spends quota if the process died before actor reservation.
        if self.table(self.db,'scheduler_action_passes'):
            for row in self.db.execute('''SELECT j.account,j.kind,j.community_id,j.group_id,j.post_id,p.baseline
              FROM scheduler_action_passes p JOIN scheduler_jobs j ON j.id=p.job_id
              WHERE j.day=? AND j.attempt_id IS NOT NULL AND j.status IN ('attempting','uncertain')
              AND p.pass=2 AND p.intent=1 AND p.attempt_id IS NULL''',(str(policy.local_day(now)),)):
                with self.account_db(row[0]) as db:
                    existing=db.execute('SELECT 1 FROM action_attempts WHERE id>? AND group_id=? AND post_id=? AND action=?',
                        (row[5],row[3],row[4],'like' if row[1]=='like' else 'repost')).fetchone()
                if not existing:used[tuple(row[:3])]+=1
        return used

    def categories(self):
        result=[]
        for number,cfg in self.config['accounts'].items():
            account=int(number)
            if not self.account_enabled(account):continue
            result.extend([(account,'like',0,cfg['likes']),(account,'personal',0,cfg['personal_reposts'])])
            result.extend((account,'community',gid,cfg['community_reposts']//len(cfg['communities'])) for gid in cfg['communities'])
        return result

    def remaining(self,now):
        used=self.usage(now);result={}
        for account,kind,target,limit in self.categories():
            left=max(0,limit-used[(account,kind,target)])
            if kind=='community':
                total=sum(n for (a,k,_),n in used.items() if a==account and k=='community')
                left=min(left,max(0,self.config['accounts'][str(account)]['community_reposts']-total))
            result[(account,kind,target)]=left
        return result

    def guard(self,now,account,kind,community_id):
        day=str(policy.local_day(now))
        if not self.account_enabled(account):raise RuntimeError('Account disabled from queue')
        if self.get('stop_requested',False):raise RuntimeError('Scheduler stop requested')
        if self.blocked():raise RuntimeError('Scheduler paused or has an uncertain action')
        # Leave30s for bounded UI checks/click after the final reservation.
        start,end=policy.work_bounds(now,self.config)
        if not start<=now.astimezone(timezone.utc)<end-timedelta(seconds=30):
            raise RuntimeError('Outside final-click work window')
        if self.action_time_closed(now):raise RuntimeError('Batch intent window closed')
        key=(account,kind,community_id)
        if self.remaining(now).get(key,0)<=0:
            raise RuntimeError('Calendar-day quota reached')
        return self.action_now(now,day=day)

    def click_timeout(self,job,now):
        row=self.db.execute('SELECT day,status,account,group_id,post_id FROM scheduler_jobs WHERE id=?',(job,)).fetchone()
        if row:self.require_not_isolated(row['account'],row['group_id'],row['post_id'])
        if (not row or row['day']!=str(policy.local_day(now)) or row['status']!='attempting'
                or self.get('stop_requested',False) or self.blocked()):
            raise RuntimeError('Final click has no active authorized day/job')
        if not self.account_enabled(row['account']):raise RuntimeError('Account disabled before final click')
        now=self.fresh_time(now)
        if row['day']!=str(policy.local_day(now)):raise RuntimeError('Final click day changed')
        start,end=policy.work_bounds(now,self.config)
        remaining=int((end-now.astimezone(timezone.utc)).total_seconds()*1000)-5000
        boundary=getattr(self,'_batch_boundary',None)
        if boundary is not None:remaining=min(remaining,int((boundary-now).total_seconds()*1000)-20000)
        if now<start or remaining<=0:raise RuntimeError('Final click deadline expired')
        return min(9000,remaining)

    def action_time_closed(self,now):
        boundary=getattr(self,'_batch_boundary',None)
        return boundary is not None and now>=boundary-timedelta(seconds=60)

    def fresh_time(self,now):
        clock=getattr(self,'_batch_clock',None)
        return clock() if callable(clock) else now

    def action_now(self,now,*,day):
        now=self.fresh_time(now)
        start,end=policy.work_bounds(now,self.config)
        if str(policy.local_day(now))!=str(day) or not start<=now<end-timedelta(seconds=30):
            raise RuntimeError('Action calendar window closed')
        if self.action_time_closed(now):raise RuntimeError('Batch intent window closed')
        return now

    def create_job(self,now,slot,account,kind,community_id,row):
        self.require_not_isolated(account,row['group_id'],row['post_id'])
        now=self.guard(now,account,kind,community_id)
        with self.account_db(account) as db:
            baseline=db.execute('SELECT COALESCE(MAX(id),0) FROM action_attempts').fetchone()[0]
        now=self.action_now(now,day=policy.local_day(now))
        cur=self.db.execute('''INSERT INTO scheduler_jobs
          (day,slot,account,kind,community_id,group_id,post_id,published_at,status,created_at,baseline)
          VALUES(?,?,?,?,?,?,?,?,'dispatching',?,?)''',
          (str(policy.local_day(now)),slot,account,kind,community_id,row['group_id'],row['post_id'],row['published_at'],now.isoformat(),baseline))
        job=cur.lastrowid
        evidence={key:row.get(key) for key in ('observed_at','like_count','repost_count','comment_count',
            'raw_like_count','raw_repost_count','own_like_delta','own_repost_delta','own_like_floor',
            'own_repost_floor','selection_minimum','refresh_started_at')}
        self.db.execute('INSERT INTO scheduler_priority_evidence VALUES(?,?)',(job,json.dumps(evidence)))
        self.db.commit();return job

    def before_attempt(self,job,now):
        row=self.db.execute('SELECT * FROM scheduler_jobs WHERE id=?',(job,)).fetchone()
        if not row or row['status']!='dispatching' or row['day']!=str(policy.local_day(now)):
            raise RuntimeError('Job already reserved or belongs to another day')
        self.require_not_isolated(row['account'],row['group_id'],row['post_id'])
        now=self.guard(now,row['account'],row['kind'],row['community_id'])
        if not self.batch_spacing_ready(now,row['slot']):
            raise BatchSpacingWait('Actual batch spacing has not elapsed')
        with self.account_db(row['account']) as db:
            baseline=db.execute('SELECT COALESCE(MAX(id),0) FROM action_attempts').fetchone()[0]
        saved=self.get('last_action_batch')
        now=self.action_now(now,day=row['day'])
        self.db.execute("UPDATE scheduler_jobs SET status='attempting',baseline=? WHERE id=?",(baseline,job));self.db.commit()
        if not saved or (saved['day'],saved['slot'])!=(row['day'],row['slot']):
            self.set('last_action_batch',{'day':row['day'],'slot':row['slot'],'at':now.isoformat()})

    def batch_spacing_ready(self,now,slot):
        saved=self.get('last_action_batch')
        now=self.fresh_time(now)
        if not saved or (saved['day'],saved['slot'])==(str(policy.local_day(now)),slot):return True
        return (now-datetime.fromisoformat(saved['at'])).total_seconds()>=policy.MIN_BATCH_GAP_SECONDS

    def record_attempt(self,job,aid):
        row=self.db.execute('SELECT * FROM scheduler_jobs WHERE id=?',(job,)).fetchone()
        if not row or row['status']!='attempting' or row['attempt_id'] is not None:
            raise RuntimeError('No unique pending attempt intent')
        with self.account_db(row['account']) as db:
            attempt=db.execute('SELECT group_id,post_id,action FROM action_attempts WHERE id=?',(aid,)).fetchone()
        action='like' if row['kind']=='like' else 'repost'
        if aid<=row['baseline'] or attempt!=(row['group_id'],row['post_id'],action):
            raise RuntimeError('Account action ledger does not match scheduled job')
        self.db.execute('UPDATE scheduler_jobs SET attempt_id=? WHERE id=?',(aid,job));self.db.commit()

    def finish_job(self,job,status,reason=''):
        if status not in ('verified','already','failed','uncertain'):raise ValueError('Unknown result')
        self.db.execute('UPDATE scheduler_jobs SET status=?,reason=? WHERE id=?',(status,reason,job));self.db.commit()

    def finish_vk_skip(self,job,evidence,now):
        row=self.db.execute('SELECT * FROM scheduler_jobs WHERE id=?',(job,)).fetchone()
        unreserved_retry=False
        if row and row['status']=='failed' and row['attempt_id'] is None:
            import vk_action_retry
            book=vk_action_retry.RetryLedger(self)
            r=book.row(job)
            if r and r['state']=='running' and r['passes']==2 and r['owner']==getattr(self,'_retry_owner',None):
                p=self.db.execute('SELECT intent,attempt_id FROM scheduler_action_passes WHERE job_id=? AND pass=2',(job,)).fetchone()
                unreserved_retry=book.enabled and tuple(p)==(0,None) and r['identity']==vk_action_retry._identity(row)
        if not row or (row['status']!='dispatching' and not unreserved_retry) or row['attempt_id'] is not None:
            raise RuntimeError('Cannot skip a reserved or completed action')
        reason='vk_like_denied' if row['kind']=='like' else 'vk_repost_denied'
        allowed={'likes.can_like'} if row['kind']=='like' else {'likes.can_publish','likes.repost_disabled'}
        if (not isinstance(evidence,dict) or evidence.get('reason')!=reason or evidence.get('permission') not in allowed
                or type(evidence.get('observations')) is not int or evidence['observations']!=2
                or evidence.get('value') is not (evidence.get('permission')=='likes.repost_disabled')):
            raise RuntimeError('No strict VK prohibition evidence')
        with self.account_db(row['account']) as db:
            import vk_batch as engine
            db.row_factory=sqlite3.Row
            engine.require_platform_unlocked(db)
            post=db.execute('SELECT like_status,repost_status FROM posts WHERE group_id=? AND post_id=?',
                            (row['group_id'],row['post_id'])).fetchone()
            if not post or any(s in ('uncertain','error') for s in post):
                raise RuntimeError('Cannot skip unresolved actor state')
            if row['kind']!='community' and post[0 if row['kind']=='like' else 1]!='pending':
                raise RuntimeError('Cannot skip completed actor state')
            if row['kind']=='community' and self.table(db,'community_reposts'):
                saved=db.execute('SELECT status FROM community_reposts WHERE community_id=? AND group_id=? AND post_id=?',
                                 (row['community_id'],row['group_id'],row['post_id'])).fetchone()
                if saved and saved[0]!='pending':raise RuntimeError('Cannot skip existing community result')
            if db.execute('SELECT 1 FROM action_attempts WHERE id>? AND group_id=? AND post_id=? AND action=?',
                (row['baseline'],row['group_id'],row['post_id'],'like' if row['kind']=='like' else 'repost')).fetchone():
                raise RuntimeError('Cannot skip an action with an existing attempt')
        snapshot={'reason':reason,'permission':evidence['permission'],'value':evidence['value'],'observations':2,'at':now.isoformat()}
        with self.db:
            self.db.execute('INSERT INTO scheduler_skip_evidence VALUES(?,?)',(job,json.dumps(snapshot)))
            self.db.execute("UPDATE scheduler_jobs SET status='failed',reason=? WHERE id=?",(reason,job))
            self.db.execute('INSERT INTO scheduler_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            ('last_vk_skip',json.dumps({'job':job,'account':row['account'],'kind':row['kind'],**snapshot})))

    def recover(self):
        """Adopt only unique durable attempts; never submit a recovered job again."""
        import vk_action_retry
        vk_action_retry.RetryLedger(self).recover()
        for row in self.db.execute("SELECT * FROM scheduler_jobs WHERE status IN ('dispatching','attempting')").fetchall():
            if row['status']=='dispatching':
                self.finish_job(row['id'],'failed','interrupted before reservation');continue
            if row['attempt_id'] is None:
                with self.account_db(row['account']) as db:
                    matches=db.execute('SELECT id FROM action_attempts WHERE id>? AND group_id=? AND post_id=? AND action=?',
                      (row['baseline'],row['group_id'],row['post_id'],'like' if row['kind']=='like' else 'repost')).fetchall()
                if len(matches)==1:self.record_attempt(row['id'],matches[0][0])
            self.finish_job(row['id'],'uncertain','interrupted after durable send intent; inspect manually')

    def prioritize(self,rows,now):
        return sorted((dict(r) for r in rows if policy.classify_post(r['published_at'],now,self.config) is not None),
                      key=lambda r:policy.priority_key(r['published_at'],now,self.config,
                          like_count=r.get('like_count'),repost_count=r.get('repost_count'),
                          comment_count=r.get('comment_count',0)))

    def selection_context(self,now,source_path,groups,*,pool=None):
        """Read one selection's inputs; callers must discard after that selection.

        This is deliberately not stored on State or retained between actions.
        Each fresh selection re-reads actor results, jobs and public observations.
        """
        selected={g['id']:g for g in groups if g.get('enabled',True)}
        with closing(sqlite3.connect(Path(source_path).resolve().as_uri()+'?mode=ro',uri=True)) as source:
            source.row_factory=sqlite3.Row
            stats=self.table(source,'api_post_engagement')
            columns=',e.like_count,e.repost_count,e.observed_at' if stats else ',NULL AS like_count,NULL AS repost_count,NULL AS observed_at'
            has_comments=stats and 'comment_count' in {r[1] for r in source.execute('PRAGMA table_info(api_post_engagement)')}
            columns+=',e.comment_count' if has_comments else ',NULL AS comment_count'
            join='LEFT JOIN api_post_engagement e ON e.group_id=a.group_id AND e.post_id=a.post_id' if stats else ''
            rows=source.execute('SELECT a.group_id,a.post_id,a.published_at'+columns+''' FROM api_seen_posts a
              JOIN post_publication d ON d.group_id=a.group_id AND d.post_id=a.post_id
              LEFT JOIN api_group_scan_progress s ON s.group_id=a.group_id
              '''+join+'''
              WHERE a.valid=1 AND d.exact=1 AND a.published_at=d.published_at
              AND (s.status IS NULL OR s.status!='error')''').fetchall()
            rows=[dict(r) for r in rows]
            # Keep the original query order for equal priority keys. A pool is
            # permission to narrow this selection, never to reuse old counters.
            if pool is not None:rows=[r for r in rows if (r['group_id'],r['post_id']) in pool]
            for row in rows:
                try:age=(now-datetime.fromisoformat(row['observed_at'])).total_seconds()
                except (TypeError,ValueError):age=-1
                if not 0<=age<=7200:row['like_count']=row['repost_count']=row['comment_count']=None
            own=engagement.local_activity(self.paths)
            rows=[engagement.effective_counts(row,own) for row in rows]
        return {'now':now,'source':Path(source_path).resolve(),'owner':self,
                'selected':selected,'rows':self.prioritize(rows,now),'actors':{},
                'isolated':{tuple(r) for r in self.db.execute('SELECT account,group_id,post_id FROM scheduler_post_isolations')},
                'jobs':{tuple(r) for r in self.db.execute('''SELECT account,kind,community_id,group_id,post_id
                    FROM scheduler_jobs WHERE day=?''',(str(policy.local_day(now)),))}}

    def _selection_actor(self,account,context):
        if account in context['actors']:return context['actors'][account]
        with self.account_db(account) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            posts={(gid,pid):(like,repost) for gid,pid,like,repost in
                   db.execute('SELECT group_id,post_id,like_status,repost_status FROM posts')}
            access=({tuple(r) for r in db.execute('SELECT group_id,post_id FROM post_access')}
                    if 'post_access' in tables else set())
            reposts=({(c,g,p):s for c,g,p,s in db.execute('SELECT community_id,group_id,post_id,status FROM community_reposts')}
                     if 'community_reposts' in tables else {})
            refs=({(g,p):(cg,cp) for g,p,cg,cp in db.execute('SELECT group_id,post_id,canonical_group_id,canonical_post_id FROM community_source_refs')}
                  if 'community_reposts' in tables and 'community_source_refs' in tables else None)
            # Preserve the sign of personal originals and direct one-hop lookup.
            canonical_blocked=({(c,*refs.get((g,p),(g,p))) for (c,g,p),status in reposts.items() if status!='pending'}
                               if refs is not None else set())
        actor={'posts':posts,'access':access,'reposts':reposts,'refs':refs,'canonical_blocked':canonical_blocked}
        context['actors'][account]=actor
        return actor

    def candidates(self,now,account,kind,community_id,source_path,groups,*,context=None):
        context=self.selection_context(now,source_path,groups) if context is None else context
        if context['owner'] is not self or context['now']!=now or context['source']!=Path(source_path).resolve():
            raise ValueError('Selection context belongs to another selection')
        actor=self._selection_actor(account,context);output=[]
        for row in context['rows']:
            gid,pid=row['group_id'],row['post_id'];cfg=context['selected'].get(gid)
            if (account,gid,pid) in context['isolated']:continue
            if not cfg or not cfg.get('like_enabled' if kind=='like' else 'repost_enabled'):continue
            if (account,kind,community_id,gid,pid) in context['jobs']:continue
            post=actor['posts'].get((gid,pid))
            if post and any(status in ('uncertain','error') for status in post):continue
            if kind!='community' and post and post[0 if kind=='like' else 1]!='pending':continue
            if (gid,pid) in actor['access']:continue
            if kind=='community':
                if actor['reposts'].get((community_id,gid,pid),'pending')!='pending':continue
                if actor['refs'] is not None:
                    canonical=actor['refs'].get((gid,pid),(gid,pid))
                    if (community_id,*canonical) in actor['canonical_blocked']:continue
            output.append(dict(row))
        return output
