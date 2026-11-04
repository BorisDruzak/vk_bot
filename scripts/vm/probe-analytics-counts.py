"""Bounded public API metadata audit. No actions or browser/profile access."""
from __future__ import annotations
import argparse,json,sys,time
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import vk_analytics as calc
import vk_analytics_store as store
from vk_analytics_collect import LiveReader,Deferred
from vk_scheduler import singleton

MAX_READS=60


def safe(item,gid):
    if not isinstance(item,dict):return {'invalid_item':True}
    fields={k:item.get(k) if type(item.get(k)) in (int,bool) else None for k in ('id','owner_id','date','is_pinned','is_deleted','is_hidden','marked_as_ads')}
    date=item.get('date')
    fields['date']=date if type(date) is int and 0<date<=int(time.time())+3600 else None
    fields['post_type']=item.get('post_type') if item.get('post_type') in ('post','copy','reply','post_ads','photo','video','clip') else None
    fields['from_wall_owner']=item.get('from_id')==-gid
    fields['has_copy_history']=bool(item.get('copy_history'))
    for name in ('inner_type','type'):
        value=item.get(name)
        fields[name]=value if type(value) is int and -(2**63)<value<2**63 else None
    owners=item.get('coowners')
    shapes={list:'list',dict:'object',tuple:'tuple',str:'string',int:'number',bool:'boolean',type(None):'null'}
    fields['coowners_shape']=shapes.get(type(owners),'other')
    fields['coowners_count']=len(owners) if type(owners) in (list,dict,tuple) else None
    # Unknown coowner schemas are diagnostic shape/count only. Never emit
    # arbitrary nested content, strings, hashes or attachments.
    if type(owners) is list and all(type(value) is int and 0<abs(value)<2**63 for value in owners):
        fields['coowners']=owners[:20]
        fields['coowners_truncated']=len(owners)>20
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
    readers=[]
    def used():return sum(reader.used for reader in readers)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    def save():
        result['used']=used();args.out.write_text(store.dumps(result),encoding='utf8')
    def budget_exhausted():
        if used()<MAX_READS:return False
        result['budget_exhausted']=True;save()
        print(store.dumps(dict(state='budget_exhausted',requests=used())),flush=True)
        return True
    with singleton(ROOT/'.vk-analytics.guard'):
        for gid in gids:
            for variant in filters:
                if budget_exhausted():return
                reader=LiveReader(ROOT,max_requests=MAX_READS-used(),wall_filter=variant,use_cache=False)
                readers.append(reader)
                for page in range(args.pages):
                    if budget_exhausted():return
                    offset=args.offset_start+page*args.count
                    request=dict(group_id=gid,filter=variant,offset=offset,count=args.count,measured_at=store.now())
                    try:
                        payload=reader.wall_get(gid,offset,args.count)
                    except Deferred:
                        result['deferred']=True;save();print(store.dumps(dict(state='worker_busy',requests=used())),flush=True);return
                    except Exception as exc:
                        code=getattr(exc,'code',None)
                        request.update(error_type=type(exc).__name__,error_code=code if type(code) is int else None)
                        result['requests'].append(request);save();print(store.dumps(request),flush=True);return
                    items=payload['items']
                    total=payload.get('count')
                    if type(total) is not int or not 0<=total<2**63:
                        request.update(total=None,error_type='invalid_envelope')
                        result['requests'].append(request);save();print(store.dumps(request),flush=True);return
                    safe_items=[safe(p,gid) for p in items]
                    request.update(total=total,items=safe_items)
                    result['requests'].append(request);save()
                    dates=[p['date'] for p in safe_items if p.get('date') is not None]
                    print(store.dumps(dict(group_id=gid,filter=variant,offset=offset,total=total,
                       returned=len(items),oldest=min(dates) if dates else None,
                       period_posts=sum(calc.stamp(args.period_start)<=date<calc.stamp(args.period_end) for date in dates))),flush=True)
                    if not items or offset+len(items)>=total:break
        result.update(complete=True,finished_at=store.now());save()


if __name__=='__main__':main()
