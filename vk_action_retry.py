"""Opt-in, durable two-pass action recovery. No platform protection is relaxed."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from urllib.error import HTTPError

import vk_batch as engine
import vk_schedule_policy as policy
import vk_scheduler_recovery as technical


IDENTITY=('day','slot','account','kind','community_id','group_id','post_id','published_at','created_at')
SAFE_REASONS={'like_confirmation_timeout','repost_confirmation_timeout','community_confirmation_timeout','community_prepare_timeout',
              'like_transport_error','repost_transport_error',
              'action_stopped_personal_action','action_stopped_community_submit','action_stopped_community_confirm',
              'action_stopped_browser_open','action_stopped_source_navigation','action_stopped_source_date_read',
              'OSError','TimeoutError','Error','KeyError','TypeError','AttributeError','OperationalError'}


def init_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS scheduler_action_recovery(
        job_id INTEGER PRIMARY KEY,identity TEXT NOT NULL,current_attempt INTEGER,
        state TEXT NOT NULL,passes INTEGER NOT NULL,reason TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL,counted INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS scheduler_action_passes(
        job_id INTEGER NOT NULL,pass INTEGER NOT NULL,attempt_id INTEGER,
        outcome TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',baseline INTEGER,
        intent INTEGER NOT NULL DEFAULT 0,started_at TEXT NOT NULL,finished_at TEXT,
        PRIMARY KEY(job_id,pass));
      CREATE TABLE IF NOT EXISTS scheduler_date_skips(
        job_id INTEGER PRIMARY KEY,evidence TEXT NOT NULL);
    ''')


def _identity(job):return json.dumps({k:job[k] for k in IDENTITY},sort_keys=True)


class RetryLedger:
    def __init__(self,state):self.state=state;self.db=state.db

    @property
    def enabled(self):return type(self.state.get('action_retry_policy')) is int and self.state.get('action_retry_policy')==1

    def exists(self):return self.state.table(self.db,'scheduler_action_recovery') and self.state.table(self.db,'scheduler_action_passes')

    def owner(self):
        if not getattr(self.state,'_retry_owner',None):self.state._retry_owner=uuid.uuid4().hex
        return self.state._retry_owner

    def row(self,job):
        return self.db.execute('SELECT * FROM scheduler_action_recovery WHERE job_id=?',(job,)).fetchone() if self.exists() else None

    def job(self,job):return self.db.execute('SELECT * FROM scheduler_jobs WHERE id=?',(job,)).fetchone()

    def summary(self):
        counts={r[0]:r[1] for r in self.db.execute('SELECT state,COUNT(*) FROM scheduler_action_recovery GROUP BY state')} if self.exists() else {}
        return {'enabled':self.enabled,'consecutive_failures':self.state.get('action_failure_streak',0),
                'held':counts.get('held',0),'pending':counts.get('pending',0)}

    def job_snapshot(self,job):
        r=self.row(job)
        if r is None:return None
        return {'state':r['state'],'passes':r['passes'],'attempts':[dict(p) for p in self.db.execute(
            'SELECT pass,attempt_id,outcome,reason FROM scheduler_action_passes WHERE job_id=? ORDER BY pass',(job,))]}

    def begin(self,job,now):
        j=self.job(job)
        if not self.enabled or not j or j['status']!='dispatching' or self.row(job):raise RuntimeError('Invalid first execution')
        with self.db:
            self.db.execute('INSERT INTO scheduler_action_recovery VALUES(?,?,NULL,\'running\',1,\'\',?,0,?)',
                (job,_identity(j),self.owner(),now.isoformat()))
            self.db.execute("INSERT INTO scheduler_action_passes(job_id,pass,outcome,started_at) VALUES(?,1,'running',?)",(job,now.isoformat()))

    def exempts(self,j):
        if self.date_skipped(j):return True
        r=self.row(j['id'])
        if (not self.enabled or not r or j['status']!='uncertain' or j['attempt_id'] is None
                or r['identity']!=_identity(j) or r['current_attempt']!=j['attempt_id'] or r['reason']!=j['reason']
                or r['reason'] not in SAFE_REASONS or r['passes'] not in (1,2)):return False
        if r['state'] not in ('pending','held') and not (r['state']=='running' and r['owner']==getattr(self.state,'_retry_owner',None)):return False
        if r['state']=='pending' and r['passes']==2:
            pending_pass=self.db.execute('SELECT outcome,intent,attempt_id FROM scheduler_action_passes WHERE job_id=? AND pass=2',(j['id'],)).fetchone()
            if not pending_pass or tuple(pending_pass)!=('deferred',0,None):return False
        p=self.db.execute('SELECT 1 FROM scheduler_action_passes WHERE job_id=? AND attempt_id=? AND outcome=\'uncertain\'',(j['id'],j['attempt_id'])).fetchone()
        return bool(p)

    def date_skipped(self,j):
        """Only an exact durable date-skip receipt exempts preserved uncertainty."""
        if not self.state.table(self.db,'scheduler_date_skips'):return False
        saved=self.db.execute('SELECT evidence FROM scheduler_date_skips WHERE job_id=?',(j['id'],)).fetchone()
        r=self.row(j['id'])
        if not saved or not r or r['state']!='date_skipped':return False
        try:e=json.loads(saved[0])
        except (ValueError,TypeError):return False
        p=self.db.execute('SELECT * FROM scheduler_action_passes WHERE job_id=? AND pass=?',(j['id'],r['passes'])).fetchone()
        return bool(p and e=={'identity':_identity(j),'reason':j['reason'],'attempt_id':j['attempt_id'],
            'baseline':j['baseline'],'pass':r['passes'],'intent':p['intent'],'pass_attempt_id':p['attempt_id'],
            'read_attempts':4} and r['identity']==_identity(j) and r['current_attempt']==j['attempt_id']
            and r['reason']==j['reason'] and p['outcome']=='date_skipped' and p['reason']=='action_stopped_source_date_unavailable')

    def skip_date(self,job,now):
        legacy=not self.enabled and self.row(job) is None
        if legacy:
            j=self.job(job)
            if not j or j['status'] not in ('dispatching','attempting'):return False
            r={'passes':1}
            p={'baseline':j['baseline'],'attempt_id':j['attempt_id'],
               'intent':int(j['status']=='attempting' or j['attempt_id'] is not None)}
        else:
            r,j=self._running(job)
            p=self.db.execute('SELECT * FROM scheduler_action_passes WHERE job_id=? AND pass=?',(job,r['passes'])).fetchone()
        # A hidden reservation cannot gain an exemption through a date error.
        with self.state.account_db(j['account']) as actor:
            baseline=p['baseline'] if p['baseline'] is not None else j['baseline']
            fresh=actor.execute('SELECT id FROM action_attempts WHERE id>?',(baseline,)).fetchall()
        if fresh!=([] if p['attempt_id'] is None else [(p['attempt_id'],)]):return False
        uncertain=j['attempt_id'] is not None or j['status']=='attempting' or bool(p['intent'])
        reason='action_stopped_source_date_unavailable'
        evidence={'identity':_identity(j),'reason':reason,'attempt_id':j['attempt_id'],'baseline':j['baseline'],
                  'pass':r['passes'],'intent':p['intent'],'pass_attempt_id':p['attempt_id'],'read_attempts':4}
        with self.db:
            if legacy:
                self.db.execute("INSERT INTO scheduler_action_recovery VALUES(?,?,?,'running',1,'',?,0,?)",
                    (job,_identity(j),j['attempt_id'],self.owner(),now.isoformat()))
                self.db.execute("INSERT INTO scheduler_action_passes(job_id,pass,attempt_id,outcome,baseline,intent,started_at) VALUES(?,1,?,'running',?,?,?)",
                    (job,j['attempt_id'],p['baseline'],p['intent'],now.isoformat()))
            self.db.execute('UPDATE scheduler_jobs SET status=?,reason=? WHERE id=?',('uncertain' if uncertain else 'failed',reason,job))
            self.db.execute("UPDATE scheduler_action_passes SET outcome='date_skipped',reason=?,finished_at=? WHERE job_id=? AND pass=?",
                (reason,now.isoformat(),job,r['passes']))
            self.db.execute("UPDATE scheduler_action_recovery SET state='date_skipped',reason=?,updated_at=? WHERE job_id=?",
                (reason,now.isoformat(),job))
            self.db.execute('INSERT INTO scheduler_date_skips VALUES(?,?)',(job,json.dumps(evidence,sort_keys=True)))
        return True

    def _running(self,job):
        r=self.row(job);j=self.job(job)
        if not self.enabled or not r or not j or r['identity']!=_identity(j) or r['state']!='running' or r['owner']!=self.owner():
            raise RuntimeError('No owned action execution pass')
        return r,j

    def before_identity_read(self,job,now):
        """A confirmation read needs protection/window proof, not a new quota."""
        r,j=self._running(job)
        self.state.require_not_isolated(j['account'],j['group_id'],j['post_id'])
        if r['passes']==2 and j['attempt_id'] is not None:
            self.state.require_unblocked()
            if (not self.state.account_enabled(j['account']) or self.state.get('stop_requested',False)
                    or self.state.blocked()):
                raise RuntimeError('Confirmation identity read is blocked')
            return self.state.action_now(now,day=j['day'])
        return self.state.guard(now,j['account'],j['kind'],j['community_id'])

    def before_attempt(self,job,now,slot):
        r,j=self._running(job)
        p=self.db.execute('SELECT * FROM scheduler_action_passes WHERE job_id=? AND pass=?',(job,r['passes'])).fetchone()
        if p['intent'] or p['attempt_id'] is not None:raise RuntimeError('Execution pass already reserved')
        if r['passes']==1:
            self.state.before_attempt(job,now)
            baseline=self.job(job)['baseline']
        else:
            if j['day']!=str(policy.local_day(now)) or j['status'] not in ('failed','uncertain'):raise RuntimeError('Retry cannot reserve this job')
            self.state.require_not_isolated(j['account'],j['group_id'],j['post_id'])
            now=self.state.guard(now,j['account'],j['kind'],j['community_id'])
            if not self.state.batch_spacing_ready(now,slot):raise RuntimeError('Retry batch spacing')
            with self.state.account_db(j['account']) as db:baseline=db.execute('SELECT COALESCE(MAX(id),0) FROM action_attempts').fetchone()[0]
            saved=self.state.get('last_action_batch')
            now=self.state.action_now(now,day=j['day'])
            with self.db:
                self.db.execute("UPDATE scheduler_jobs SET status='attempting',baseline=? WHERE id=?",(baseline,job))
                self.db.execute('UPDATE scheduler_action_passes SET intent=1,baseline=? WHERE job_id=? AND pass=2',(baseline,job))
            if not saved or (saved['day'],saved['slot'])!=(j['day'],slot):self.state.set('last_action_batch',{'day':j['day'],'slot':slot,'at':now.isoformat()})
        with self.db:self.db.execute('UPDATE scheduler_action_passes SET intent=1,baseline=? WHERE job_id=? AND pass=?',(baseline,job,r['passes']))

    def record_attempt(self,job,aid):
        r,j=self._running(job)
        p=self.db.execute('SELECT * FROM scheduler_action_passes WHERE job_id=? AND pass=?',(job,r['passes'])).fetchone()
        if j['status']!='attempting' or not p['intent'] or p['attempt_id'] is not None or type(aid) is not int or aid<=p['baseline']:
            raise RuntimeError('No unique retry reservation')
        with self.state.account_db(j['account']) as db:
            attempt=db.execute('SELECT group_id,post_id,action FROM action_attempts WHERE id=?',(aid,)).fetchone()
            newer=db.execute('SELECT id FROM action_attempts WHERE id>?',(p['baseline'],)).fetchall()
        if attempt!=(j['group_id'],j['post_id'],'like' if j['kind']=='like' else 'repost') or newer!=[(aid,)]:
            raise RuntimeError('Actor attempt differs from owned pass')
        with self.db:
            self.db.execute('UPDATE scheduler_jobs SET attempt_id=? WHERE id=?',(aid,job))
            self.db.execute('UPDATE scheduler_action_recovery SET current_attempt=? WHERE job_id=?',(aid,job))
            self.db.execute('UPDATE scheduler_action_passes SET attempt_id=? WHERE job_id=? AND pass=?',(aid,job,r['passes']))

    def fail_pass(self,job,reason,now):
        r,j=self._running(job)
        if reason not in SAFE_REASONS:raise RuntimeError('Failure not eligible for bounded recovery')
        status='uncertain' if j['status']=='attempting' or j['attempt_id'] is not None else 'failed'
        with self.db:
            self.db.execute('UPDATE scheduler_jobs SET status=?,reason=? WHERE id=?',(status,reason,job))
            self.db.execute('UPDATE scheduler_action_passes SET outcome=?,reason=?,finished_at=? WHERE job_id=? AND pass=?',
                (status,reason,now.isoformat(),job,r['passes']))
            self.db.execute('UPDATE scheduler_action_recovery SET reason=?,updated_at=? WHERE job_id=?',(reason,now.isoformat(),job))

    def pending(self,job,now):
        r=self.row(job)
        if not r or r['passes']!=1:raise RuntimeError('Only first pass can await retry')
        with self.db:self.db.execute("UPDATE scheduler_action_recovery SET state='pending',updated_at=? WHERE job_id=?",(now.isoformat(),job))

    def begin_retry(self,job,now):
        r=self.row(job);j=self.job(job)
        reusable=(r is not None and r['passes']==2 and self.db.execute("SELECT 1 FROM scheduler_action_passes WHERE job_id=? AND pass=2 AND outcome='deferred' AND intent=0 AND attempt_id IS NULL",(job,)).fetchone())
        if (not r or r['state']!='pending' or (r['passes']!=1 and not reusable) or r['identity']!=_identity(j)
                or j['status'] not in ('failed','uncertain') or r['current_attempt']!=j['attempt_id']):raise RuntimeError('Retry exhausted or mismatched')
        with self.state.account_db(j['account']) as db:
            baseline=db.execute('SELECT COALESCE(MAX(id),0) FROM action_attempts').fetchone()[0]
        with self.db:
            self.db.execute("UPDATE scheduler_action_recovery SET state='running',passes=2,owner=?,updated_at=? WHERE job_id=?",(self.owner(),now.isoformat(),job))
            if reusable:self.db.execute("UPDATE scheduler_action_passes SET outcome='running',baseline=?,finished_at=NULL WHERE job_id=? AND pass=2",(baseline,job))
            else:self.db.execute("INSERT INTO scheduler_action_passes(job_id,pass,outcome,baseline,started_at) VALUES(?,2,'running',?,?)",(job,baseline,now.isoformat()))

    def defer_unreserved(self,job,now):
        r,j=self._running(job)
        p=self.db.execute('SELECT intent,attempt_id FROM scheduler_action_passes WHERE job_id=? AND pass=?',(job,r['passes'])).fetchone()
        if tuple(p)!=(0,None):raise RuntimeError('Cannot defer durable intent')
        if r['passes']==1:
            self.state.finish_job(job,'failed','batch_window_or_budget_closed');self.interrupt(job,now)
        else:
            with self.db:
                self.db.execute("UPDATE scheduler_action_passes SET outcome='deferred',finished_at=? WHERE job_id=? AND pass=2",(now.isoformat(),job))
                self.db.execute("UPDATE scheduler_action_recovery SET state='pending',updated_at=? WHERE job_id=?",(now.isoformat(),job))

    def success(self,job,status,now):
        r,j=self._running(job)
        if status not in ('verified','already'):raise RuntimeError('Success requires positive outcome')
        with self.db:
            self.db.execute('UPDATE scheduler_jobs SET status=?,reason=\'\' WHERE id=?',(status,job))
            self.db.execute('UPDATE scheduler_action_passes SET outcome=?,finished_at=? WHERE job_id=? AND pass=?',(status,now.isoformat(),job,r['passes']))
            self.db.execute("UPDATE scheduler_action_recovery SET state='complete',reason='',updated_at=? WHERE job_id=?",(now.isoformat(),job))
            self.db.execute("INSERT INTO scheduler_meta VALUES('action_failure_streak','0') ON CONFLICT(key) DO UPDATE SET value='0'")

    def hold(self,job,now,*,count_failure=True):
        r=self.row(job)
        if not r or r['state'] not in ('running','pending','held'):raise RuntimeError('Cannot hold unknown recovery')
        if r['counted']:return
        count=self.state.get('action_failure_streak',0)+(1 if count_failure else 0)
        with self.db:
            self.db.execute("UPDATE scheduler_action_recovery SET state='held',counted=1,updated_at=? WHERE job_id=?",(now.isoformat(),job))
            self.db.execute("INSERT INTO scheduler_meta VALUES('action_failure_streak',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(count),))
        if count_failure and count>=3:self.state.pause('action_failures_repeated',now=now)

    def interrupt(self,job,now):
        if not self.row(job):return
        with self.db:
            self.db.execute("UPDATE scheduler_action_recovery SET state='interrupted',updated_at=? WHERE job_id=?",(now.isoformat(),job))
            self.db.execute("UPDATE scheduler_action_passes SET outcome='interrupted',finished_at=? WHERE job_id=? AND outcome='running'",(now.isoformat(),job))

    def recover(self):
        if not self.exists():return
        # No unfinished pass is replayed after process loss, even before a recorded click.
        for r in self.db.execute("SELECT job_id FROM scheduler_action_recovery WHERE state='running'").fetchall():
            saved=self.row(r['job_id']);j=self.job(r['job_id'])
            p=self.db.execute('SELECT * FROM scheduler_action_passes WHERE job_id=? AND pass=?',(j['id'],saved['passes'])).fetchone()
            if p['intent'] and p['attempt_id'] is None and p['baseline'] is not None:
                with self.state.account_db(j['account']) as db:
                    fresh=db.execute('SELECT id,group_id,post_id,action FROM action_attempts WHERE id>?',(p['baseline'],)).fetchall()
                if len(fresh)==1 and fresh[0][1:]==(j['group_id'],j['post_id'],'like' if j['kind']=='like' else 'repost'):
                    aid=fresh[0][0]
                    with self.db:
                        self.db.execute('UPDATE scheduler_jobs SET attempt_id=? WHERE id=?',(aid,j['id']))
                        self.db.execute('UPDATE scheduler_action_recovery SET current_attempt=? WHERE job_id=?',(aid,j['id']))
                        self.db.execute('UPDATE scheduler_action_passes SET attempt_id=? WHERE job_id=? AND pass=?',(aid,j['id'],saved['passes']))
            self.interrupt(r['job_id'],datetime.now().astimezone())
            j=self.job(r['job_id'])
            if j and j['attempt_id'] is not None and j['status'] in ('attempting','uncertain'):
                self.state.finish_job(j['id'],'uncertain','recovery_interrupted')

    def next_pending(self,now,pool):
        if not self.enabled:return None
        self.expire_pending(now)
        for r in self.db.execute("SELECT job_id FROM scheduler_action_recovery WHERE state='pending' ORDER BY job_id").fetchall():
            j=self.job(r['job_id'])
            if j['day']!=str(policy.local_day(now)):continue
            if not self.state.account_enabled(j['account']):continue
            if pool is not None and (j['group_id'],j['post_id']) not in pool:continue
            return dict(j)
        return None

    def expire_pending(self,now):
        if not self.enabled or not self.exists():return
        for r in self.db.execute('''SELECT r.job_id FROM scheduler_action_recovery r JOIN scheduler_jobs j ON j.id=r.job_id
          WHERE r.state='pending' AND j.day<? ORDER BY r.job_id''',(str(policy.local_day(now)),)).fetchall():
            self.hold(r['job_id'],now,count_failure=False)


def retryable(error,job):
    """An explicit confirmation timeout or proven transport fault, never a guard."""
    chain=[];seen=set();exc=error
    while exc is not None and id(exc) not in seen and len(chain)<8:
        seen.add(id(exc));chain.append(exc);exc=exc.__cause__ or exc.__context__
    if exc is not None:return False
    import vk_scheduled_action as action
    from vk_scheduler_blocks import ManualBlockActive
    import vk_api_read as api
    for exc in chain:
        if isinstance(exc,ManualBlockActive):return False
        if isinstance(exc,action.community.ui.CommunityPreparationTimeout):
            if (type(exc) is not action.community.ui.CommunityPreparationTimeout or exc is not error
                    or exc.stage!='community_prepare' or exc.reason_code!='community_prepare_timeout'
                    or job['kind']!='community' or job['status'] not in ('dispatching','failed')
                    or job['attempt_id'] is not None):return False
        if getattr(exc,'stage',None)=='cleanup' or isinstance(exc,(HTTPError,engine.PlatformRateLimited,engine.ActionGuardError,api.VkApiLimited,api.VkApiAuthFailure)):return False
        if isinstance(exc,action.ScheduledActionStopped) and exc.stage not in ('browser_open','source_navigation','source_date_read','personal_action','community_submit','community_confirm'):return False
        if isinstance(exc,engine.ActionTransportError):
            if (type(exc) is not engine.ActionTransportError or exc.action!=('like' if job['kind']=='like' else 'repost')
                    or getattr(exc,'stage',None)!='personal_action' or not isinstance(exc.__cause__,action.PlaywrightError)):return False
        if isinstance(exc,engine.StopRun) and not isinstance(exc,(action.ScheduledActionStopped,engine.ActionConfirmationError,engine.ActionTransportError,action.community.ui.CommunityConfirmationError)):return False
        if isinstance(exc,action.community.ui.CommunityConfirmationError) and exc.reason_code!='community_confirmation_timeout':return False
    if job['status']=='attempting' and job['attempt_id'] is None:return False
    if job['attempt_id'] is None:return isinstance(error,engine.ActionTransportError) or technical.is_technical(error,'action')
    if isinstance(error,engine.ActionConfirmationError):return error.reason_code==('like_confirmation_timeout' if job['kind']=='like' else 'repost_confirmation_timeout')
    if isinstance(error,action.community.ui.CommunityConfirmationError):return job['kind']=='community'
    return any(isinstance(e,(OSError,TimeoutError,action.PlaywrightError)) for e in chain)


def actor_uncertainties_safe(state):
    """All unresolved actor fields must have an exact existing authorization."""
    book=RetryLedger(state)
    approved=[]
    for j in state.db.execute("SELECT * FROM scheduler_jobs WHERE status='uncertain'"):
        isolated=state.db.execute('''SELECT 1 FROM scheduler_post_isolations i WHERE job_id=?
          AND account=? AND group_id=? AND post_id=? AND attempt_id=? AND reason=?
          AND authorization='explicit_user_post_isolation' ''',(j['id'],j['account'],j['group_id'],j['post_id'],j['attempt_id'],j['reason'])).fetchone()
        if book.exempts(j) or (isolated and j['kind']=='like' and j['community_id']==0):approved.append(dict(j))
    for account in state.paths:
        with state.account_db(account) as db:
            engine.require_platform_unlocked(db)
            keys=set()
            for j in approved:
                if j['account']!=account:continue
                if j['attempt_id'] is None and book.date_skipped(j):continue
                a=db.execute('SELECT group_id,post_id,action FROM action_attempts WHERE id=?',(j['attempt_id'],)).fetchone()
                if a!=(j['group_id'],j['post_id'],'like' if j['kind']=='like' else 'repost'):return False
                if j['kind']=='community':
                    link=db.execute('''SELECT r.community_id FROM community_run_attempts l JOIN community_runs r ON r.id=l.run_id WHERE l.attempt_id=?''',(j['attempt_id'],)).fetchall()
                    if link!=[(j['community_id'],)]:return False
                keys.add((j['kind'],j['community_id'],j['group_id'],j['post_id']))
            for gid,pid,ls,rs in db.execute("SELECT group_id,post_id,like_status,repost_status FROM posts WHERE like_status IN ('uncertain','error') OR repost_status IN ('uncertain','error')"):
                if ls=='error' or rs=='error':return False
                if ls=='uncertain' and ('like',0,gid,pid) not in keys:return False
                if rs=='uncertain' and ('personal',0,gid,pid) not in keys:return False
            if state.table(db,'community_reposts'):
                for target,gid,pid,status in db.execute("SELECT community_id,group_id,post_id,status FROM community_reposts WHERE status IN ('uncertain','error')"):
                    if status!='uncertain' or ('community',target,gid,pid) not in keys:return False
    return True


def execute_job(worker,job,row,slot,capacity):
    """Return actual reservations and optional batch-stop phase; max two passes."""
    import vk_scheduler as scheduler
    import vk_scheduled_action as actions
    state=worker.state;book=RetryLedger(state);attempted=0
    adaptive=state.config.get('schedule_mode')=='adaptive'
    if book.row(job):book.begin_retry(job,worker.clock())
    else:book.begin(job,worker.clock())
    while True:
        j=book.job(job);r=book.row(job)
        context={'job':job,'account':j['account'],'kind':j['kind'],'community':j['community_id'],'source_group':j['group_id'],'source_post':j['post_id'],'slot':slot}
        def log(event,**data):
            worker.observe_timing_event(event,job,**data)
            worker.pulse_action(event,job=job,stage=data.get('stage'))
            state.log_event(event,worker.clock(),**context,**data)
        def record(aid):
            nonlocal attempted
            book.record_attempt(job,aid);attempted+=1
            worker.observe_timing_event('attempt_reserved',job,attempt_id=aid)
        def before_attempt():
            if adaptive and attempted>=capacity:
                raise RuntimeError('Adaptive batch attempt target reached')
            return book.before_attempt(job,worker.clock(),slot)
        try:
            log('action_started')
            options={}
            if r['passes']==2 and j['attempt_id'] is not None:options={'recovery_attempt_id':j['attempt_id'],
                'allow_retry_click':(not adaptive or attempted<capacity) and
                    state.remaining(worker.clock()).get((j['account'],j['kind'],j['community_id']),0)>0}
            result=actions.execute_action(j['account'],j['kind'],{**row,'community_id':j['community_id']},
                before_attempt=before_attempt,record_attempt=record,
                identity_read_deadline=worker.identity_read_deadline(slot),
                before_identity_read=lambda:book.before_identity_read(job,worker.clock()),
                before_click=lambda:state.click_timeout(job,worker.clock()),config_path=worker.config_path,diagnostic=log,**options)
            outcome=result['status']
            if outcome in ('verified','already'):
                book.success(job,outcome,worker.clock());worker.technical_succeeded('action')
                state.set('last_account',j['account']);log('action_result',outcome=outcome,attempt_id=result.get('attempt_id'))
                return {'attempted':attempted,'phase':None,'skipped':0}
            if outcome=='skipped_vk' and j['attempt_id'] is None and result.get('attempt_id') is None:
                state.finish_vk_skip(job,result.get('skip_evidence'),worker.clock())
                book.interrupt(job,worker.clock());log('action_result',outcome='skipped_vk',reason=result['skip_evidence']['reason'])
                return {'attempted':attempted,'phase':None,'skipped':1}
            if outcome=='uncertain' and r['passes']==2:
                # Confirmation-only recovery did not establish success. Keep the original evidence.
                reason=j['reason']
                book.fail_pass(job,reason,worker.clock())
                if state.blocked() or not actor_uncertainties_safe(state):
                    book.interrupt(job,worker.clock());state.pause(reason)
                    return {'attempted':attempted,'phase':'attention','skipped':0}
                book.hold(job,worker.clock())
                log('action_result',outcome='uncertain',attempt_id=book.job(job)['attempt_id'])
                return {'attempted':attempted,'phase':'attention' if state.get('paused') else None,'skipped':0}
            raise RuntimeError('Unsupported recovery outcome')
        except Exception as exc:
            current=book.job(job);reason=scheduler.safe_failure_reason(exc)
            log('action_failed',reason=reason,outcome='uncertain' if current['attempt_id'] is not None or current['status']=='attempting' else 'failed',attempt_id=current['attempt_id'],stage=getattr(exc,'stage',None),observation=getattr(exc,'observation',None),error=exc)
            if isinstance(exc,engine.ActionConfirmationError):state.set('last_action_confirmation',{'job':job,'at':worker.clock().isoformat(),'reason':reason,'observation':exc.observation})
            p=state.db.execute('SELECT * FROM scheduler_action_passes WHERE job_id=? AND pass=?',(job,r['passes'])).fetchone()
            if date_skip_error(exc) and book.skip_date(job,worker.clock()):
                if p['intent'] and p['attempt_id'] is None:attempted+=1
                if state.blocked():return {'attempted':attempted,'phase':'attention','skipped':0}
                if not actor_uncertainties_safe(state):
                    state.pause('StopRun')
                    return {'attempted':attempted,'phase':'attention','skipped':0}
                log('action_result',outcome='date_skipped',reason='action_stopped_source_date_unavailable',attempt_id=current['attempt_id'])
                return {'attempted':attempted,'phase':None,'skipped':0}
            _,end=policy.work_bounds(worker.clock(),state.config)
            identity_deadline=isinstance(exc,actions.accounts.IdentityPreparationDeadline)
            normal_stop=(identity_deadline or state.get('stop_requested',False) or not state.account_enabled(current['account'])
                or state.action_time_closed(worker.clock())
                or worker.clock().timestamp()>=end.timestamp()-30
                or state.remaining(worker.clock()).get((current['account'],current['kind'],current['community_id']),0)<=0)
            if (identity_deadline or type(exc) is RuntimeError or isinstance(exc,scheduler.ledger.BatchSpacingWait)) and normal_stop and not p['intent'] and p['attempt_id'] is None:
                book.defer_unreserved(job,worker.clock())
                return {'attempted':attempted,'phase':'batch_action_deadline' if identity_deadline else worker.batch_stop_phase(worker.clock(),current['account'],
                    current['kind'],current['community_id'],day=current['day']),'skipped':0}
            eligible=retryable(exc,current)
            # The actor ledger must match the just-completed pass, with no hidden new attempts.
            if eligible:
                p=state.db.execute('SELECT * FROM scheduler_action_passes WHERE job_id=? AND pass=?',(job,r['passes'])).fetchone()
                if type(exc) is actions.community.ui.CommunityPreparationTimeout and (p['intent'] or p['attempt_id'] is not None):eligible=False
                with state.account_db(current['account']) as db:
                    baseline=p['baseline'] if p['baseline'] is not None else current['baseline']
                    fresh=db.execute('SELECT id FROM action_attempts WHERE id>?',(baseline,)).fetchall()
                expected=[] if p['attempt_id'] is None else [(p['attempt_id'],)]
                eligible=eligible and fresh==expected
            if not eligible:
                state.finish_job(job,'uncertain' if current['attempt_id'] is not None or current['status']=='attempting' else 'failed',reason)
                book.interrupt(job,worker.clock());state.pause(reason)
                return {'attempted':attempted,'phase':'attention','skipped':0}
            book.fail_pass(job,reason,worker.clock())
            if r['passes']==1:book.pending(job,worker.clock())
            try:safe=not state.blocked() and actor_uncertainties_safe(state)
            except Exception:safe=False
            if not safe:
                book.interrupt(job,worker.clock());state.pause(reason)
                return {'attempted':attempted,'phase':'attention','skipped':0}
            if r['passes']==2:
                book.hold(job,worker.clock())
                return {'attempted':attempted,'phase':'attention' if state.get('paused') else None,'skipped':0}
            # An adaptive batch may finish its existing attempt's confirmation
            # without another reservation. The callback and allow_retry_click
            # independently prohibit spending beyond the durable target.
            if attempted>=capacity and not (adaptive and current['attempt_id'] is not None):
                return {'attempted':attempted,'phase':None,'skipped':0}
            if state.action_time_closed(worker.clock()):
                return {'attempted':attempted,'phase':'batch_action_deadline','skipped':0}
            if state.get('stop_requested',False) or not policy.in_work_window(worker.clock(),state.config):
                return {'attempted':attempted,'phase':worker.batch_stop_phase(worker.clock(),day=current['day']),'skipped':0}
            book.begin_retry(job,worker.clock())


def date_skip_error(error):
    """No nested guard/protection/cleanup can be hidden by this typed result."""
    import vk_scheduled_action as actions
    if type(error) is not actions.SourceDateUnavailable or error.read_attempts!=4:return False
    if error.stage!='source_date_unavailable' or error.reason_code!='action_stopped_source_date_unavailable':return False
    seen={id(error)};current=error.__cause__ or error.__context__
    while current is not None:
        if id(current) in seen or len(seen)>=8 or not isinstance(current,actions.PlaywrightError):return False
        seen.add(id(current));current=current.__cause__ or current.__context__
    return True
