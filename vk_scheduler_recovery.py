"""Conservative technical recovery. Never retry a send or acknowledge a VK guard."""
import sqlite3
from urllib.error import HTTPError
import vk_batch as engine
import vk_api_read as api
import vk_scheduled_action as actions


def is_technical(error,scope):
    chain=[];seen=set();current=error
    while current is not None and id(current) not in seen and len(chain)<8:
        chain.append(current);seen.add(id(current));current=current.__cause__ or current.__context__
    if current is not None:return False  # A hidden guard or cyclic chain is not proven safe.
    for exc in chain:
        if getattr(exc,'stage',None)=='cleanup':return False
        if isinstance(exc,HTTPError) and not (scope=='read' and isinstance(error,api.VkApiTransientRead)
                and exc.code in (500,502,503,504)):return False
        if isinstance(exc,(engine.PlatformRateLimited,api.VkApiLimited,api.VkApiAuthFailure)):return False
        if isinstance(exc,engine.StopRun) and not isinstance(exc,actions.ScheduledActionStopped):return False
        if isinstance(exc,actions.ScheduledActionStopped) and exc.stage not in ('browser_open','source_navigation','source_date_read'):return False
    if scope=='read':return isinstance(error,api.VkApiTransientRead)
    if scope!='action':return False
    return any(isinstance(exc,(OSError,TimeoutError,actions.PlaywrightError)) or
        (type(exc) in (KeyError,TypeError,AttributeError)) or
        (isinstance(exc,sqlite3.OperationalError) and getattr(exc,'sqlite_errorcode',None) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)) for exc in chain)


def safe_to_defer(state,job=None):
    if state.get('stop_requested',False) or state.blocked():return False
    others=state.db.execute("SELECT id FROM scheduler_jobs WHERE status IN ('dispatching','attempting')").fetchall()
    if any(row[0]!=(job['id'] if job else None) for row in others):return False
    if job:
        actual=state.db.execute('SELECT status,attempt_id,baseline FROM scheduler_jobs WHERE id=?',(job['id'],)).fetchone()
        if (job['status']!='dispatching' or job['attempt_id'] is not None or not actual
                or actual['status'] not in ('dispatching','failed') or actual['attempt_id'] is not None
                or actual['baseline']!=job['baseline']):return False
    import vk_action_retry
    if vk_action_retry.RetryLedger(state).enabled:
        if not vk_action_retry.actor_uncertainties_safe(state):return False
        if job:
            with state.account_db(job['account']) as db:
                if db.execute('SELECT 1 FROM action_attempts WHERE id>? LIMIT 1',(job['baseline'],)).fetchone():return False
                post=db.execute('SELECT like_status,repost_status FROM posts WHERE group_id=? AND post_id=?',(job['group_id'],job['post_id'])).fetchone()
                if job['kind']!='community' and post and post[0 if job['kind']=='like' else 1]!='pending':return False
                if job['kind']=='community' and state.table(db,'community_reposts'):
                    saved=db.execute('SELECT status FROM community_reposts WHERE community_id=? AND group_id=? AND post_id=?',(job['community_id'],job['group_id'],job['post_id'])).fetchone()
                    if saved and saved[0]!='pending':return False
        return True
    for account in state.paths:
        approved=[tuple(r) for r in state.db.execute('''SELECT i.group_id,i.post_id,i.attempt_id FROM scheduler_post_isolations i
          JOIN scheduler_jobs j ON j.id=i.job_id WHERE i.account=? AND j.account=i.account
          AND j.group_id=i.group_id AND j.post_id=i.post_id AND j.attempt_id=i.attempt_id AND j.reason=i.reason
          AND j.status='uncertain' AND j.kind='like' AND j.community_id=0 AND i.authorization='explicit_user_post_isolation' ''',(account,))]
        with state.account_db(account) as db:
            db.row_factory=sqlite3.Row;engine.require_platform_unlocked(db)
            isolated=set()
            for gid,pid,aid in approved:
                a=db.execute('SELECT group_id,post_id,action FROM action_attempts WHERE id=?',(aid,)).fetchone()
                if a is None or tuple(a)!=(gid,pid,'like'):return False
                isolated.add((gid,pid))
            for r in db.execute("SELECT group_id,post_id,like_status,repost_status FROM posts WHERE like_status IN ('uncertain','error') OR repost_status IN ('uncertain','error')"):
                if not ((r['group_id'],r['post_id']) in isolated and r['like_status']=='uncertain' and r['repost_status'] not in ('uncertain','error')):return False
            if state.table(db,'community_reposts') and db.execute("SELECT 1 FROM community_reposts WHERE status IN ('uncertain','error') LIMIT 1").fetchone():return False
            if job and account==job['account']:
                if db.execute('SELECT 1 FROM action_attempts WHERE id>? LIMIT 1',(job['baseline'],)).fetchone():return False
                post=db.execute('SELECT like_status,repost_status FROM posts WHERE group_id=? AND post_id=?',(job['group_id'],job['post_id'])).fetchone()
                if post and post[0 if job['kind']=='like' else 1]!='pending' and job['kind']!='community':return False
                if job['kind']=='community' and state.table(db,'community_reposts'):
                    saved=db.execute('SELECT status FROM community_reposts WHERE community_id=? AND group_id=? AND post_id=?',(job['community_id'],job['group_id'],job['post_id'])).fetchone()
                    if saved and saved[0]!='pending':return False
    return True
