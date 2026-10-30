"""Durable scheduler blocks; releasing one never acknowledges actor history."""
import json
from datetime import datetime,timedelta,timezone
import vk_schedule_policy as policy


class ManualBlockActive(RuntimeError):
    reason_code='manual_block'


def _put(db,key,value):
    db.execute('INSERT INTO scheduler_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key,json.dumps(value)))


def _valid_record(row,reason):
    try:
        stamp=datetime.fromisoformat(row['at'])
        return (isinstance(row,dict) and type(row['version']) is int and row['version']==1 and row['reason']==reason
               and row['kind'] in ('manual','automatic') and isinstance(row['day'],str)
               and stamp.tzinfo is not None and str(policy.local_day(stamp))==row['day'])
    except (KeyError,ValueError,TypeError):return False


def status(state):
    reason=state.get('paused')
    if not reason:return None
    row=state.get('scheduler_block')
    if not _valid_record(row,reason):return {'kind':'manual','reason':reason,'legacy':True,'day':None,'at':None}
    return {k:row[k] for k in ('kind','reason','day','at')}


def _quiet(state,now):
    return not (state.get('stop_requested',False)
        or state.db.execute("SELECT 1 FROM scheduler_jobs WHERE status IN ('dispatching','attempting')").fetchone()
        or state.db.execute("SELECT 1 FROM scheduler_action_recovery WHERE state='running'").fetchone()
        # Old empty orphan batches are historical evidence, not current work.
        # In-flight jobs/running passes above still block across all days.
        or state.db.execute('SELECT 1 FROM scheduler_batches WHERE finished_at IS NULL AND day>=?',
                            (str(policy.local_day(now)),)).fetchone())


def pause(state,reason,*,kind='automatic',now=None):
    if kind not in ('manual','automatic'):raise ValueError('Unknown block kind')
    now=now or state.fresh_time(datetime.now(timezone.utc))
    if now.tzinfo is None:raise ValueError('Aware block time required')
    record={'version':1,'kind':kind,'reason':str(reason),'at':now.isoformat(),'day':str(policy.local_day(now))}
    with state.db:
        if not state.db.in_transaction:state.db.execute('BEGIN IMMEDIATE')
        existing=status(state)
        if kind=='manual' and existing and existing['kind']=='manual':
            return not existing.get('legacy',False) and existing['reason']=='manual_block' and str(reason)=='manual_block'
        if kind=='automatic' and existing and existing['kind']=='manual':
            if str(reason)=='manual_block':return False
            pending=state.get('scheduler_suspended_automatic_block')
            if pending is None:
                _put(state.db,'scheduler_suspended_automatic_block',record)
            elif isinstance(pending,dict) and _valid_record(pending,pending.get('reason')) and pending['kind']=='automatic':
                causes=pending.get('causes') or [{k:pending[k] for k in ('reason','at','day')}]
                merged={**pending,'causes':[*causes,{k:record[k] for k in ('reason','at','day')}]}
                if record['day']>=pending['day']:merged.update(at=record['at'],day=record['day'])
                _put(state.db,'scheduler_suspended_automatic_block',merged)
            return False
        if kind=='manual' and existing and existing['kind']=='automatic':
            _put(state.db,'scheduler_suspended_automatic_block',state.get('scheduler_block'))
        _put(state.db,'paused',str(reason));_put(state.db,'scheduler_block',record)
    return True


def expire(state,now):
    """Expire only against a later saved day and a strictly future saved slot."""
    block=status(state)
    if not block or block['kind']!='automatic' or str(policy.local_day(now))<=block['day']:return False
    with state.db:
        # Reserve the write transaction before comparing the exact block again.
        state.db.execute('BEGIN IMMEDIATE')
        if status(state)!=block or not _quiet(state,now):return False
        day=str(policy.local_day(now))
        plan=state.db.execute('SELECT slots FROM schedule_days WHERE day=?',(day,)).fetchone()
        if not plan:return False
        slots=[datetime.fromisoformat(s) for s in json.loads(plan[0])]
        existing=state.get('resume_not_before',0)
        future=[(i,s) for i,s in enumerate(slots) if s>now+timedelta(seconds=60) and s.timestamp()>=existing
                and not state.db.execute('SELECT 1 FROM scheduler_batches WHERE day=? AND slot=?',(day,i)).fetchone()]
        if not future:return False
        slot,gate=future[0]
        _put(state.db,'last_scheduler_block_release',{'kind':'automatic','reason':block['reason'],'blocked_day':block['day'],
            'at':now.isoformat(),'day':day,'slot':slot,'resume_at':gate.isoformat(),'block_record':state.get('scheduler_block')})
        _put(state.db,'resume_not_before',gate.timestamp());_put(state.db,'paused',None);_put(state.db,'scheduler_block',None)
        _put(state.db,'action_failure_streak',0)
        _put(state.db,'technical_failures',{'read':0,'action':0})
    return True


def clear_manual(state,*,now=None):
    """Operator path only releases the explicit manual switch, never legacy guards."""
    now=now or datetime.now(timezone.utc)
    with state.db:
        state.db.execute('BEGIN IMMEDIATE');block=status(state)
        if not block or block['kind']!='manual' or block.get('legacy') or block['reason']!='manual_block':return False
        if not _quiet(state,now):return False
        day=str(policy.local_day(now));plan=state.db.execute('SELECT slots FROM schedule_days WHERE day=?',(day,)).fetchone()
        if not plan:return False
        slots=[datetime.fromisoformat(s) for s in json.loads(plan[0])]
        future=[s for i,s in enumerate(slots) if s>now+timedelta(seconds=60) and s.timestamp()>=state.get('resume_not_before',0)
                and not state.db.execute('SELECT 1 FROM scheduler_batches WHERE day=? AND slot=?',(day,i)).fetchone()]
        if not future:return False
        pending=state.get('scheduler_suspended_automatic_block')
        if pending is not None and (not isinstance(pending,dict) or not _valid_record(pending,pending.get('reason')) or pending['kind']!='automatic'):return False
        _put(state.db,'last_scheduler_block_release',{'kind':'manual','reason':'manual_block','at':now.isoformat()})
        _put(state.db,'resume_not_before',future[0].timestamp())
        _put(state.db,'paused',pending['reason'] if isinstance(pending,dict) else None)
        _put(state.db,'scheduler_block',pending);_put(state.db,'scheduler_suspended_automatic_block',None)
    return True
