"""Bounded public API metadata audit. No actions or browser/profile access."""
from __future__ import annotations
import argparse,json,sys,time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import vk_api_read as api
import vk_analytics as calc
import vk_analytics_store as store
from vk_analytics_collect import LiveReader,Deferred
from vk_scheduler import singleton


def safe(item,gid):
    if not isinstance(item,dict):return {'invalid_item':True}
    fields={k:item.get(k) if type(item.get(k)) in (int,bool) else None for k in ('id','owner_id','date','is_pinned','is_deleted','is_hidden','marked_as_ads')}
    fields['post_type']=item.get('post_type') if item.get('post_type') in ('post','copy','reply','post_ads','photo','video','clip') else None
    fields['from_wall_owner']=item.get('from_id')==-gid
    fields['has_copy_history']=bool(item.get('copy_history'))
    fields.update({k:store.counter(item,k) for k in store.COUNTERS})
    return fields


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--groups',required=True)
    parser.add_argument('--filters',default='owner,all')
    parser.add_argument('--pages',type=int,default=3)
    parser.add_argument('--count',type=int,default=100)
    parser.add_argument('--offset-start',type=int,default=0)
    parser.add_argument('--period-start',default='2026-10-08')
    parser.add_argument('--period-end',default='2026-10-15')
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    gids=[int(x) for x in args.groups.split(',')]
    allowed={g['id'] for g in json.loads((ROOT/'sosnovsky_registry.json').read_text(encoding='utf8'))}
    filters=args.filters.split(',')
    if not set(gids)<=allowed or not 1<=args.pages<=10 or not 1<=args.count<=100 or not 0<=args.offset_start<=10000 or not set(filters)<=set(('owner','all','others')):
        raise ValueError('Invalid bounded audit selection')
    result=dict(started_at=store.now(),groups=gids,requests=[],complete=False)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    def save():args.out.write_text(store.dumps(result),encoding='utf8')
    transport=api._default_transport
    with singleton(ROOT/'.vk-analytics.guard'):
        reader=LiveReader(ROOT,max_requests=60)
        for gid in gids:
            for variant in filters:
                for page in range(args.pages):
                    offset=args.offset_start+page*args.count
                    request=dict(group_id=gid,filter=variant,offset=offset,count=args.count,measured_at=store.now())
                    # Only the public documented filter differs; same reservation,
                    # warning handler, VM binding, rate/budget and OS leases.
                    def filtered(params):return transport({**params,'filter':variant})
                    try:
                        with patch.object(api,'_default_transport',side_effect=filtered):
                            payload=reader.wall_get(gid,offset,args.count)
                    except Deferred:
                        result['deferred']=True;save();print(store.dumps(dict(state='worker_busy',requests=reader.used)),flush=True);return
                    except Exception as exc:
                        request.update(error_type=type(exc).__name__,error_code=getattr(exc,'code',None))
                        result['requests'].append(request);save();print(store.dumps(request),flush=True);return
                    items=payload['items']
                    request.update(total=payload.get('count'),items=[safe(p,gid) for p in items])
                    result['requests'].append(request);save()
                    dates=[p.get('date') for p in items if isinstance(p,dict) and type(p.get('date')) is int]
                    print(store.dumps(dict(group_id=gid,filter=variant,offset=offset,total=payload.get('count'),
                       returned=len(items),oldest=min(dates) if dates else None,
                       period_posts=sum(isinstance(p,dict) and type(p.get('date')) is int and calc.stamp(args.period_start)<=p['date']<calc.stamp(args.period_end) for p in items))),flush=True)
                    if not items or (type(payload.get('count')) is int and offset+len(items)>=payload['count']):break
        result.update(complete=True,finished_at=store.now(),used=reader.used);save()


if __name__=='__main__':main()
