"""Resumable public statistics reader. Each network call uses VM shared guards."""
from __future__ import annotations

import json
import sqlite3
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime
from pathlib import Path

import vk_analytics_store as store
from vk_analytics import stamp


class Deferred(Exception):
    pass


def _error(db,run,gid,code):
    db.execute('INSERT INTO analytics_errors(run_id,group_id,at,code) VALUES(?,?,?,?)',
               (run,gid,store.now(),code))


def _observed_at(payload):
    if '_observed_at' not in payload:return store.now()
    at=payload['_observed_at']
    if not isinstance(at,str):raise ValueError('Invalid observation timestamp')
    parsed=datetime.fromisoformat(at)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Observation timestamp must be timezone aware')
    return at


def collect_run(db,run,fetch,*,page_size=100,max_pages=1000):
    if not 1<=page_size<=100 or not 1<=max_pages<=10000:raise ValueError('Invalid pagination limit')
    record=db.execute('SELECT * FROM analytics_runs WHERE id=?',(run,)).fetchone()
    if record is None:raise ValueError('Unknown analytics run')
    start,end=stamp(record['period_start']),stamp(record['period_end'])
    for g in db.execute('SELECT * FROM analytics_run_groups WHERE run_id=? AND included=1 ORDER BY group_id',(run,)).fetchall():
        if g['status']=='complete':continue
        gid=g['group_id'];offset=0;baseline=g['expected_count'];previous=None;seen=set();canonical_seen=set();reason='page_limit';status='partial';pages=0
        observed_from=g['observed_from']
        # Offset checkpoints are diagnostic only. Resume unfinished communities
        # from head so new insertions cannot silently skip records after a crash.
        try:
            for _ in range(max_pages):
                payload=fetch(gid,offset,page_size)
                at=_observed_at(payload);items=payload.get('items');total=payload.get('count')
                if (not isinstance(items,list) or len(items)>page_size or type(total) is not int
                    or total<0 or total<offset+len(items)):
                    raise ValueError('Invalid page envelope')
                if baseline is not None and baseline!=total:
                    reason='wall_count_changed';break
                baseline=total
                dates=[];accepted=[];witnessed=[];invalid=False;new_ids=set();canonical_new=set();bad_reason='invalid_post'
                for item in items:
                    try:
                        if not isinstance(item,dict):raise ValueError('Invalid item')
                        local_id,canonical_owner,canonical_id=store.post_identity(gid,item)
                        canonical=(canonical_owner,canonical_id)
                        # Validate metadata without saving out-of-period posts.
                        if (type(item.get('date')) is not int or not 0<item['date']<=int(time.time())+3600
                            or type(item.get('is_pinned',0)) not in (int,bool) or item.get('is_pinned',0) not in (0,1)):
                            raise ValueError('Invalid item')
                        if (local_id in new_ids or canonical in canonical_new
                            or ((local_id in seen or canonical in canonical_seen) and not item.get('is_pinned'))):
                            raise ValueError('Repeated item')
                        new_ids.add(local_id);canonical_new.add(canonical)
                        witnessed.append(item)
                        if not item.get('is_pinned'):dates.append(item['date'])
                        if start<=item['date']<end:accepted.append(item)
                    except ValueError:invalid=True
                if any(a<b for a,b in zip(dates,dates[1:])) or (dates and previous is not None and dates[0]>previous):
                    invalid=True;bad_reason='ordering_anomaly'
                # Every committed page, snapshots and cursor move atomically.
                with db:
                    # Metadata outside the period can still contradict the date
                    # of a canonical post inside it; preserve the exact witness.
                    for item in witnessed:store.record_identity_witness(db,run,gid,item,at=at)
                    for item in accepted:store.observe(db,run,gid,item,'wall.get',at=at)
                    pages+=1;offset+=len(items)
                    # Preserve coverage even when the page has no period posts.
                    if observed_from is None or datetime.fromisoformat(at)<datetime.fromisoformat(observed_from):
                        observed_from=at
                    db.execute('UPDATE analytics_run_groups SET pages=?,next_offset=?,status=?,expected_count=?,observed_from=? WHERE run_id=? AND group_id=?',
                               (pages,offset,'partial',baseline,observed_from,run,gid))
                    db.execute('''UPDATE analytics_runs SET status='running',finished_at=NULL,
                      groups_checked=(SELECT COUNT(*) FROM analytics_run_groups WHERE run_id=? AND included=1 AND pages>0),
                      posts_received=(SELECT COUNT(*) FROM analytics_snapshots WHERE run_id=?) WHERE id=?''',(run,run,run))
            …1112 tokens truncated…"VM-only caller; reuses existing ledger/pacing/locks, never browser/actions."""
    def __init__(self,root,*,max_requests=500,before_request=None,wall_filter='owner',use_cache=True):
        import vk_api_read as api
        if type(wall_filter) is not str or wall_filter not in ('owner','all','others') or type(use_cache) is not bool:
            raise ValueError('Invalid wall read policy')
        self.wall_filter,self.use_cache=wall_filter,use_cache
        self.root=Path(root).resolve();self.used=0;self.max_requests=max_requests;self.before_request=before_request
        self._head_observed_at={}
        binding=self.root/'.vk-analytics-runtime.json'
        if not binding.is_file():raise ValueError('Live analytics requires a VM runtime binding')
        runtime=json.loads(binding.read_text(encoding='utf8'))
        if (runtime.get('machine','').lower()!=socket.gethostname().lower()
            or Path(runtime.get('root','')).resolve()!=self.root
            or self.root!=Path(__file__).resolve().parent):
            raise ValueError('Live analytics VM runtime mismatch')
        self.token=api.configured_token()
        if not self.token:raise api.VkApiAuthFailure('Existing API key not configured')
        self.path=self.root/'vk_batch.sqlite3'
        if not self.path.is_file():raise ValueError('Existing shared API ledger required')
        if not 1<=max_requests<=5000:raise ValueError('Invalid request budget')

    def _call(self,gid,method,params,*,fresh_after=None):
        import vk_accounts as accounts
        import vk_api_read as api
        import vk_unified as app
        if type(self.wall_filter) is not str or self.wall_filter not in ('owner','all','others') or type(self.use_cache) is not bool:
            raise ValueError('Invalid wall read policy')
        cache_wall=method=='wall.get' and self.wall_filter=='owner' and self.use_cache
        try:
            with accounts.exclusive_activity(self.root),app.exclusive_run(self.root/'.vk-unified-running.lock'),closing(sqlite3.connect(self.path,timeout=5)) as ledger:
                ledger.row_factory=sqlite3.Row
                # A cached page must obey the scheduled deadline as well.
                if self.before_request:self.before_request()
                # API client records 429/rate lock durably in the existing ledger.
                original=(lambda data:api._default_transport({**data,'filter':self.wall_filter})) if method=='wall.get' else lambda data:self._byid_transport(data,params)
                network_calls=0
                def before_network():
                    if self.before_request:self.before_request()
                    if self.used>=self.max_requests:raise api.VkApiFailure('Collection request budget exhausted')
                def transport(data):
                    nonlocal network_calls
                    network_calls+=1
                    return original(data)
                client=api.VKReadClient(ledger,self.token,pause_seconds=5,per_run=1,per_24h=5000,transport=transport,
                                        shared_path=None if cache_wall else False,fresh_after=fresh_after,
                                        before_request=before_network)
                if self.used>=self.max_requests and (not cache_wall or not client.has_cached_wall(
                        gid,count=params.get('count',100),offset=params.get('offset',0))):
                    raise api.VkApiFailure('Collection request budget exhausted')
                try:return client.wall_get(gid,count=params.get('count',100),offset=params.get('offset',0))
                finally:self.used+=network_calls
        except app.RunAlreadyActive:raise Deferred('worker_or_profile_busy') from None

    def wall_get(self,gid,offset,count):
        if offset==0:self._head_observed_at.pop(gid,None)
        payload=self._call(gid,'wall.get',dict(offset=offset,count=count),fresh_after=self._head_observed_at.get(gid))
        if offset==0 and '_observed_at' in payload:
            self._head_observed_at[gid]=_observed_at(payload)
        return payload

    def catalog_references(self,start,end,group_ids):
        """Read previous public API witnesses from the shared VM catalog, mode=ro."""
        store.valid_period(start,end)
        if any(type(g) is not int or g<=0 for g in group_ids):raise ValueError('Invalid group IDs')
        allowed=set(group_ids)
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute('''SELECT group_id,post_id,published_at,seen_at
                      FROM api_seen_posts WHERE valid=1 AND published_at>=? AND published_at<?
                      ORDER BY group_id,post_id''',(stamp(start),stamp(end))) if r['group_id'] in allowed]

    @staticmethod
    def _byid_transport(data,params):
        import vk_api_read as api
        body=urllib.parse.urlencode(dict(posts=params['posts'],access_token=data['access_token'],v=data['v'])).encode('utf8')
        request=urllib.request.Request('https://api.vk.com/method/wall.getById',data=body,
              headers={'Content-Type':'application/x-www-form-urlencoded'},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=20) as response:
                raw=response.read(8_000_001);status=response.status
        except urllib.error.HTTPError as e:
            if e.code==429:return 429,{}
            raise api.VkApiFailure('HTTP read failure',code=e.code) from None
        except (urllib.error.URLError,TimeoutError,OSError):raise api.VkApiFailure('Read transport failure') from None
        if len(raw)>8_000_000:raise api.VkApiFailure('Read size limit')
        try:payload=json.loads(raw)
        except (ValueError,TypeError):raise api.VkApiFailure('Malformed JSON') from None
        if not isinstance(payload,dict):raise api.VkApiFailure('Malformed response')
        if isinstance(payload.get('response'),list):payload['response']={'items':payload['response']}
        return status,payload

    def wall_get_by_id(self,pairs,*,batch_size=100):
        if not 1<=batch_size<=100 or not pairs or len(pairs)>batch_size:raise ValueError('Batch size must be1..100 and cover request')
        if any(type(g) is not int or g<=0 or type(p) is not int or p<=0 for g,p in pairs):raise ValueError('Invalid post IDs')
        result=self._call(pairs[0][0],'wall.getById',{'posts':','.join(f'-{g}_{p}' for g,p in pairs)})
        allowed=set(pairs);returned=set()
        for item in result['items']:
            if not isinstance(item,dict) or type(item.get('owner_id')) is not int or type(item.get('id')) is not int:
                raise ValueError('Invalid by-ID item')
            pair=(-item['owner_id'],item['id'])
            if pair not in allowed or pair in returned:raise ValueError('Unexpected by-ID identity')
            returned.add(pair)
        return result['items']


def refresh_known(db,run,previous,reader,*,batch_size=100):
    if not 1<=batch_size<=100:raise ValueError('Invalid batch size')
    previous_row=db.execute('SELECT * FROM analytics_runs WHERE id=?',(previous,)).fetchone()
    current=db.execute('SELECT * FROM analytics_runs WHERE id=?',(run,)).fetchone()
    if previous_row is None or current is None or any(previous_row[k]!=current[k] for k in ('period_start','period_end')):
        raise ValueError('Known-post refresh requires identical periods')
    old=db.execute('SELECT * FROM analytics_snapshots WHERE run_id=? ORDER BY group_id,post_id',(previous,)).fetchall()
    included={r[0] for r in db.execute('SELECT group_id FROM analytics_run_groups WHERE run_id=? AND included=1',(run,))}
    old=[r for r in old if r['group_id'] in included]
    for offset in range(0,len(old),batch_size):
        batch=old[offset:offset+batch_size];pairs=[(r['group_id'],r['post_id']) for r in batch]
        present={(r['group_id'],r['post_id']) for r in db.execute('SELECT group_id,post_id FROM analytics_snapshots WHERE run_id=?',(run,))}
        batch=[r for r in batch if (r['group_id'],r['post_id']) not in present]
        if not batch:continue
        pairs=[(r['group_id'],r['post_id']) for r in batch]
        try:items=reader.wall_get_by_id(pairs,batch_size=batch_size)
        except Exception as exc:
            code=getattr(exc,'code',None)
            with db:
                db.execute('INSERT INTO analytics_capabilities(at,method,batch_size,status,code) VALUES(?,?,?,?,?)',
                   (store.now(),'wall.getById',len(pairs),'unsupported' if code==1051 else 'error',code))
                _error(db,run,None,'byid_'+str(code) if type(code) is int else 'byid_read_failed')
            store.finish(db,run)
            return 'unsupported' if code==1051 else 'partial'
        found={(-p['owner_id'],p['id']):p for p in items};at=store.now()
        with db:
            for r in batch:
                p=found.get((r['group_id'],r['post_id']))
                state=None
                if p is None:state='not_returned'
                elif p.get('is_deleted') in (1,True):state='deleted'
                elif p.get('date')!=r['published_at']:state='invalid_metadata'
                if not state:
                    try:store.observe(db,run,r['group_id'],p,'wall.getById',at=at)
                    except ValueError:state='invalid_metadata'
                if state:store.record_missing(db,run,r,'wall.getById',status=state)
                if state:_error(db,run,r['group_id'],'byid_'+state)
            db.execute('INSERT INTO analytics_capabilities(at,method,batch_size,status) VALUES(?,?,?,?)',
                       (at,'wall.getById',len(pairs),'supported'))
    # Updating only known IDs cannot establish a complete current archive.
    with db:db.execute("UPDATE analytics_run_groups SET status='partial',reason='known_posts_only' WHERE run_id=? AND included=1",(run,))
    store.finish(db,run)
    return 'known_posts_only'
