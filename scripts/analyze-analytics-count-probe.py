"""Compare public paginated probe identities to a frozen analytics run offline."""
import argparse
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--db',type=Path,required=True);p.add_argument('--run',required=True)
p.add_argument('--official',type=Path,required=True);p.add_argument('--probe',type=Path,action='append',required=True)
p.add_argument('--out',type=Path,required=True)
a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
db=sqlite3.connect(a.db.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
run=db.execute('SELECT * FROM analytics_runs WHERE id=?',(a.run,)).fetchone()
def stamp(day):return int(datetime.fromisoformat(day+'T00:00:00+05:00').timestamp())
start,end=stamp(run['period_start']),stamp(run['period_end'])
official={r['group_id']:r for r in json.loads(a.official.read_text(encoding='utf8')) if r.get('group_id')}
result=[]
for path in a.probe:
    probe=json.loads(path.read_text(encoding='utf8'))
    for gid in probe['groups']:
        row=official[gid];sources={};second={};measurements=[]
        for method in ('owner','all'):
            requests=[r for r in probe['requests'] if r['group_id']==gid and r['filter']==method]
            sources[method]=sorted({x['id'] for r in requests for x in r['items']
                if x['owner_id']==-gid and type(x['date']) is int and start<=x['date']<end})
            second[method]=sorted({x['id'] for r in requests if r['offset']>0 for x in r['items']
                if x['owner_id']==-gid and type(x['date']) is int and start<=x['date']<end})
            measurements.extend(r['measured_at'] for r in requests)
        saved=sorted(r[0] for r in db.execute('SELECT post_id FROM analytics_snapshots WHERE run_id=? AND group_id=?',(a.run,gid)))
        result.append(dict(group_id=gid,name=row.get('name'),official_posts=row.get('posts'),original_saved_posts=len(saved),
            owner_posts=len(sources['owner']),all_posts=len(sources['all']),second_page_target_posts=len(set(second['owner']+second['all'])),
            owner_ids=sources['owner'],all_ids=sources['all'],saved_ids=saved,
            current_ids_missing_from_original=sorted(set(sources['owner']+sources['all'])-set(saved)),
            original_ids_missing_from_current=sorted(set(saved)-set(sources['owner']+sources['all'])),
            measured_from=min(measurements),measured_to=max(measurements),source_file=path.name))
(a.out/'count-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
fields=['group_id','name','official_posts','original_saved_posts','owner_posts','all_posts','second_page_target_posts',
        'current_ids_missing_from_original','original_ids_missing_from_current','measured_from','measured_to']
with (a.out/'count-evidence.csv').open('w',encoding='utf-8-sig',newline='') as stream:
    writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';');writer.writeheader()
    writer.writerows({k:r[k] for k in fields} for r in result)
print(json.dumps([{k:r[k] for k in ('group_id','official_posts','original_saved_posts','owner_posts','all_posts',
    'second_page_target_posts','current_ids_missing_from_original','original_ids_missing_from_current')} for r in result]))
db.close()
