"""Pure calendar, limits and batch timing for independent VK account jobs.

No I/O, browser or credentials. The caller must persist the randomized daily
schedule and claim slots durably; constructing a schedule does not execute it.
All returned datetimes are UTC aware. Input ``now`` must be timezone aware.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from math import ceil
import random


# Current YEKT is fixed UTC+5. This avoids a hidden tzdata dependency on Windows.
LOCAL_TZ=timezone(timedelta(hours=5),name='Asia/Yekaterinburg')
UTC=timezone.utc
MIN_BATCH_GAP_SECONDS=540  # User-approved23Oct:60batches x4 for three full accounts.
MAX_JITTER_SECONDS=120
LAST_BATCH_RESERVE_SECONDS=600
DEFAULT_CONFIG={
    'timezone':'Asia/Yekaterinburg','start_hour':9,'end_hour':19,
    'batch_size':4,'old_days':7,
    'accounts':{
        '1':{'likes':50,'personal_reposts':10,'community_reposts':20,
             'communities':[242481741,242481697]},
        '2':{'likes':50,'personal_reposts':10,'community_reposts':20,
             'communities':[242479129,242479058]},
    },
}


def _integer(value,minimum,maximum):
    return type(value) is int and minimum<=value<=maximum


def validate_account_policy(account):
    if not isinstance(account,dict) or set(account)!={'likes','personal_reposts','community_reposts','communities'}:
        raise ValueError('Unknown account policy field')
    for key,maximum in (('likes',50),('personal_reposts',10),('community_reposts',20)):
        if not _integer(account[key],0,maximum): raise ValueError('Invalid daily action limit')
    groups=account['communities']
    if (not isinstance(groups,list) or len(groups)>2 or
            any(not _integer(gid,1,2**53-1) for gid in groups) or
            len(set(groups))!=len(groups) or
            (account['community_reposts'] and (len(groups)!=2 or account['community_reposts']%2))):
        raise ValueError('Community quota must split evenly between two explicit unique destinations')


def validate_config(config=None) -> dict:
    """Deep-merge policy overrides, reject ambiguity and return an owned copy.

    Limits may be reduced, never exceed the user-approved per-account50/10/20.
    Community repost quota must divide evenly among the explicit destinations.
    Only these policy keys are accepted; runtime/scanner settings belong outside.
    """
    cfg=deepcopy(DEFAULT_CONFIG)
    if config is None:return cfg
    if not isinstance(config,dict) or set(config)-(set(cfg)|{'planned_batches'}):
        raise ValueError('Unknown or malformed schedule policy')
    for key,value in config.items():
        if key!='accounts':cfg[key]=deepcopy(value)
    if cfg['timezone']!='Asia/Yekaterinburg':
        raise ValueError('Schedule timezone must be Asia/Yekaterinburg')
    if not (_integer(cfg['start_hour'],9,18) and
            _integer(cfg['end_hour'],10,22) and cfg['start_hour']<cfg['end_hour']):
        raise ValueError('Schedule window must stay within09:00–22:00')
    if not _integer(cfg['batch_size'],1,6) or not _integer(cfg['old_days'],0,7):
        raise ValueError('Invalid batch size or reserve post age')
    accounts=config.get('accounts',{})
    if not isinstance(accounts,dict) or any(not isinstance(k,str) or not k.isascii() or
            not k.isdigit() or str(int(k))!=k or not 1<=int(k)<=1000 for k in accounts):
        raise ValueError('Accounts must be explicit positive numbered records')
    for number,override in accounts.items():
        if number not in cfg['accounts']:
            cfg['accounts'][number]={'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[]}
        if not isinstance(override,dict) or set(override)-set(cfg['accounts'][number]):
            raise ValueError('Unknown account policy field')
        cfg['accounts'][number].update(deepcopy(override))
    for account in cfg['accounts'].values():validate_account_policy(account)
    total=sum(sum(a[key] for key in ('likes','personal_reposts','community_reposts'))
              for a in cfg['accounts'].values())
    window=(cfg['end_hour']-cfg['start_hour'])*3600
    count=max(1,ceil(total/cfg['batch_size']))
    if 'planned_batches' in cfg:
        requested=cfg['planned_batches']
        if type(requested) is not int or requested<count or (total>0 and requested>total):
            raise ValueError('Planned batches must cover quotas with no empty targets')
        count=requested
        gaps=count-1
        if gaps*MIN_BATCH_GAP_SECONDS+gaps*(gaps-1)//2>window-LAST_BATCH_RESERVE_SECONDS:
            raise ValueError('Unique batch gaps cannot fit the configured work window')
    if count>window//MIN_BATCH_GAP_SECONDS:
        raise ValueError('Configured batches cannot fit the work window safely')
    return cfg


def _utc(now) -> datetime:
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('An aware current datetime is required')
    return now.astimezone(UTC)


def local_day(now:datetime) -> date:
    """Return the calendar date in YEKT for an aware instant."""
    return _utc(now).astimezone(LOCAL_TZ).date()


def day_bounds(now:datetime) -> tuple[int,int]:
    """Return UTC epoch boundaries [local midnight, next midnight)."""
    start=datetime.combine(local_day(now),time(),LOCAL_TZ)
    return int(start.timestamp()),int((start+timedelta(days=1)).timestamp())


def _work_bounds(day:date,cfg:dict) -> tuple[datetime,datetime]:
    return (datetime.combine(day,time(cfg['start_hour']),LOCAL_TZ).astimezone(UTC),
            datetime.combine(day,time(cfg['end_hour']),LOCAL_TZ).astimezone(UTC))


def work_bounds(now:datetime,config=None) -> tuple[datetime,datetime]:
    """Return UTC instants of this local day's action window."""
    return _work_bounds(local_day(now),validate_config(config))


def in_work_window(now:datetime,config=None) -> bool:
    """Actions may start at09:00;19:00 is excluded (default policy)."""
    current=_utc(now);start,end=work_bounds(current,config)
    return start<=current<end


def daily_total(config=None) -> int:
    """Total allowed attempts per local day across both independent accounts."""
    cfg=validate_config(config)
    return sum(sum(a[key] for key in ('likes','personal_reposts','community_reposts'))
               for a in cfg['accounts'].values())


def batch_count(config=None) -> int:
    """Number of batches; each has at most the configured <=6 attempts."""
    cfg=validate_config(config)
    return cfg['planned_batches'] if 'planned_batches' in cfg else max(1,ceil(daily_total(cfg)/cfg['batch_size']))


def batch_attempt_limit(config,slot) -> int:
    """Saved slot's attempt target; legacy policies retain their full capacity.

    An explicit slot count evenly distributes the daily quota using cumulative
    integer boundaries. Missed or unused targets never enlarge another slot.
    """
    cfg=validate_config(config);count=batch_count(cfg)
    if not _integer(slot,0,count-1):raise ValueError('Invalid batch slot index')
    if 'planned_batches' not in cfg:return cfg['batch_size']
    total=daily_total(cfg)
    return (slot+1)*total//count-slot*total//count


def slot_seconds(config=None) -> float:
    """Nominal spacing; defaults give160 actions /40 batches /900 seconds."""
    cfg=validate_config(config)
    return (cfg['end_hour']-cfg['start_hour'])*3600/batch_count(cfg)


def _balanced_schedule(start,end,count,spacing,generator):
    """Bounded dense-plan fallback: unique consecutive gaps in balanced pairs.

    Pairing extremes keeps cumulative jitter small; shuffling pairs and their
    orientation preserves randomness without a retry loop or shorter pauses.
    """
    gaps=count-1
    if not gaps:return [start]
    span=min(int((end-start).total_seconds())-LAST_BATCH_RESERVE_SECONDS,round(gaps*spacing))
    base=(span-gaps*(gaps-1)//2)//gaps
    if base<MIN_BATCH_GAP_SECONDS:raise ValueError('Unique batch gaps cannot fit the work window')
    values=list(range(base,base+gaps));pairs=[]
    while len(values)>1:
        pair=[values.pop(0),values.pop()]
        if generator.randint(0,1):pair.reverse()
        pairs.append(pair)
    if values:pairs.append(values)
    for index in range(len(pairs)-1,0,-1):
        other=generator.randint(0,index)
        pairs[index],pairs[other]=pairs[other],pairs[index]
    slots=[start]
    for pair in pairs:
        for gap in pair:slots.append(slots[-1]+timedelta(seconds=gap))
    return slots


def build_schedule(day_or_now,config=None,*,rng=None) -> list[datetime]:
    """Build daily randomized UTC batch starts; caller MUST persist the result.

    Default40 nominal starts get jitter within±120 seconds. First
    start is never before09:00; last leaves10 minutes before19:00. Starts remain
    unique and at least9 minutes apart, with no repeated gap seconds. At most
    eight random draws per slot precede a finite deterministic offset search.
    Dense plans can use a bounded balanced-gap fallback; no unbounded retry.
    Pass random.Random(seed) in tests;
    actual callers get SystemRandom and must not rebuild on restart.
    """
    cfg=validate_config(config)
    if isinstance(day_or_now,datetime):day=local_day(day_or_now)
    elif type(day_or_now) is date:day=day_or_now
    else:raise ValueError('Schedule needs local date or aware datetime')
    start,end=_work_bounds(day,cfg)
    spacing=slot_seconds(cfg)
    count=batch_count(cfg)
    gaps=count-1
    minimum_span=gaps*MIN_BATCH_GAP_SECONDS+gaps*(gaps-1)//2
    if minimum_span>(end-start).total_seconds()-LAST_BATCH_RESERVE_SECONDS:
        raise ValueError('Unique whole-second batch gaps cannot fit the configured work window')
    jitter=MAX_JITTER_SECONDS
    generator=rng if rng is not None else random.SystemRandom()
    slots=[];used_gaps=set()
    for index in range(count):
        nominal=start+timedelta(seconds=round(index*spacing))
        def permitted(offset):
            candidate=nominal+timedelta(seconds=offset)
            candidate=max(start,min(candidate,end-timedelta(seconds=LAST_BATCH_RESERVE_SECONDS)))
            if slots:
                gap=int((candidate-slots[-1]).total_seconds())
                if gap<MIN_BATCH_GAP_SECONDS or gap in used_gaps:return None
            return candidate
        selected=None
        for _ in range(8):
            selected=permitted(generator.randint(-jitter,jitter))
            if selected is not None:break
        if selected is None:
            for offset in range(-jitter,jitter+1):
                selected=permitted(offset)
                if selected is not None:break
        if selected is None:
            slots=_balanced_schedule(start,end,count,spacing,generator)
            break
        if slots:used_gaps.add(int((selected-slots[-1]).total_seconds()))
        slots.append(selected)
    _validate_schedule(slots,cfg)
    if any((b-a).total_seconds()<MIN_BATCH_GAP_SECONDS for a,b in zip(slots,slots[1:])):
        raise ValueError('Randomized batches cannot fit safe spacing')
    return slots


def _validate_schedule(schedule,cfg):
    if not isinstance(schedule,(list,tuple)) or not schedule:
        raise ValueError('Persisted schedule must contain aware UTC starts')
    slots=[_utc(slot) for slot in schedule]
    day=local_day(slots[0]);start,end=_work_bounds(day,cfg)
    gap_seconds=[int((b-a).total_seconds()) for a,b in zip(slots,slots[1:])]
    if (slots!=sorted(set(slots)) or len(slots)>batch_count(cfg) or
            any(local_day(slot)!=day or not start<=slot<=end-timedelta(seconds=LAST_BATCH_RESERVE_SECONDS)
                for slot in slots) or
            any(gap<MIN_BATCH_GAP_SECONDS for gap in gap_seconds) or
            len(set(gap_seconds))!=len(gap_seconds)):
        raise ValueError('Persisted batch times must be ordered, spaced and leave finish reserve')
    return slots


def batch_slot(now:datetime,config=None,*,schedule=None) -> int | None:
    """Return only the current/latest index, never a list of missed indices.

    Without a persisted schedule this is the nominal slot. With one it is the
    latestdue index; use due_slot for the expiry guard before claiming a job.
    """
    current=_utc(now);cfg=validate_config(config)
    if not in_work_window(current,cfg):return None
    if schedule is not None:
        slots=_validate_schedule(schedule,cfg)
        if local_day(slots[0])!=local_day(current):return None
        index=bisect_right(slots,current)-1
        return index if index>=0 else None
    start,_=work_bounds(current,cfg)
    return min(batch_count(cfg)-1,int((current-start).total_seconds()//slot_seconds(cfg)))


def due_slot(now:datetime,schedule,*,grace_seconds=120,config=None) -> int | None:
    """Return at most one due start, expiring lateness beyond grace; no catchup.

    A separate durable claim is still mandatory. An old day's schedule returns
    None. Malformed times raise ValueError even if current time is off-window.
    """
    if not _integer(grace_seconds,0,MAX_JITTER_SECONDS):
        raise ValueError('Batch grace must be between0 and120 seconds')
    current=_utc(now);cfg=validate_config(config)
    slots=_validate_schedule(schedule,cfg)
    index=batch_slot(current,cfg,schedule=slots)
    if index is None or (current-slots[index]).total_seconds()>grace_seconds:return None
    return index


def next_wake(now:datetime,config=None,*,schedule=None) -> datetime:
    """Next future start, or next day's opening once this schedule is exhausted.

    Returning tomorrow09:00 intentionally lets the caller create that new day's
    persisted randomized schedule then. This function never returns missed work.
    """
    current=_utc(now);cfg=validate_config(config)
    if schedule is not None:
        slots=_validate_schedule(schedule,cfg)
        if local_day(slots[0])==local_day(current):
            future=[slot for slot in slots if slot>current]
            if future:return future[0]
    else:
        start,end=work_bounds(current,cfg)
        if current<start:return start
        index=batch_slot(current,cfg)
        if index is not None and index+1<batch_count(cfg):
            return start+timedelta(seconds=(index+1)*slot_seconds(cfg))
    return _work_bounds(local_day(current)+timedelta(days=1),cfg)[0]


def classify_post(published_at,now:datetime,config=None) -> str | None:
    """'new' means published today; 'old' is the previous <=7 local days.

    Future, malformed and older timestamps are not candidates. A currentday
    post remains 'new' independently of when the scanner first discovered it.
    """
    current=_utc(now);cfg=validate_config(config)
    if type(published_at) is not int or published_at<0 or published_at>current.timestamp():return None
    start,_=day_bounds(current)
    if published_at>=start:return 'new'
    if published_at>=start-cfg['old_days']*86400:return 'old'
    return None


def priority_key(published_at,now:datetime,config=None,*,
                 like_count=None,repost_count=None,comment_count=0) -> tuple[int,int,int,int]:
    """Sort day band, known low engagement, then newest; excluded records last.

    Counters must be nonnegative exact integers. Missing or invalid data
    stays unknown and follows known counts within its day band; it is never
    interpreted as zero. Newtoday always precedes reserve regardless of counts.
    Selection must still exclude classify_post=None; sorting is not eligibility.
    """
    band=classify_post(published_at,now,config)
    # The default retains compatibility for old two-counter callers. Live rows
    # always pass comment_count explicitly, including None for unknown data.
    known=all(type(value) is int and value>=0 for value in (like_count,repost_count,comment_count))
    total=like_count+repost_count+comment_count if known else 0
    return ({'new':0,'old':1}.get(band,2),0 if known else 1,total,
            -published_at if type(published_at) is int else 0)
