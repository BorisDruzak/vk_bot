"""Read-only view of saved OMSU measurements. Never fetches VK or opens actor DBs."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime,timezone,timedelta,date
from pathlib import Path
import re
import sqlite3

import vk_analytics as calc


def read_analytics(root,*,run_id=None,week_mode='calendar',include_reposts=True):
    if run_id is not None and (not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id)):
        raise ValueError('Invalid measurement')
    if week_mode not in ('calendar','period') or type(include_reposts) is not bool:raise ValueError('Invalid rules')
    path=Path(root).resolve()/'vk_analytics.sqlite3'
    if not path.is_file():raise FileNotFoundError('Analytics not collected')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        runs=[dict(r) for r in db.execute('SELECT * FROM analytics_runs WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 30')]
        if not runs:return dict(runs=[],run=None,groups=[],weekly_summary=[],district=None,week_mode=week_mode,include_reposts=include_reposts)
        run=dict(db.execute('SELECT * FROM analytics_runs WHERE id=? AND finished_at IS NOT NULL',(run_id or runs[0]['id'],)).fetchone() or {})
        if not run:raise ValueError('Unknown measurement')
        registry=[dict(g) for g in db.execute('SELECT * FROM analytics_run_groups WHERE run_id=? ORDER BY group_id',(run['id'],))]
        snapshots=defaultdict(list)
        for p in db.execute('SELECT * FROM analytics_snapshots WHERE run_id=? ORDER BY published_at,post_id',(run['id'],)):
            snapshots[p['group_id']].append(dict(p))
    start,end=run['period_start'],run['period_end']
    measurement_times={g['group_id']:calc.measurement_timestamp(run,snapshots[g['group_id']],
                       observed_from=g.get('observed_from')) for g in registry}
    measured=min((measurement_times[g['group_id']] if measurement_times[g['group_id']] is not None else float('-inf')
                  for g in registry if g['included']),default=float('-inf'))
    if week_mode=='period':
        windows=[];cursor=date.fromisoformat(start)
        while cursor<date.fromisoformat(end):
            right=cursor+timedelta(days=7);windows.append((cursor.isoformat(),right.isoformat()));cursor=right
    else:windows=calc.week_windows(start,end,'calendar_full')
    definitions=[dict(start=a,end_exclusive=b,end=(date.fromisoformat(b)-timedelta(days=1)).isoformat(),
                      closed=calc.stamp(b)<=measured,covered=a>=start and b<=end) for a,b in windows]
    groups=[];all_rows=[]
    for g in registry:
        group_measured=measurement_times[g['group_id']]
        if group_measured is None:group_measured=float('-inf')
        selected=[p for p in snapshots[g['group_id']] if calc.stamp(start)<=p['published_at']<calc.stamp(end)
                  and (include_reposts or not p['is_repost'])]
        available=[p for p in selected if p['status'] in ('complete','missing_counters')]
        stats=calc._aggregate(selected,g['status']=='complete' and calc.stamp(end)<=group_measured)
        observed_er,observed_p33=calc.engagement(*(sum(p[k] for p in selected if p['status']=='complete') for k in ('likes','comments','reposts','views')))
        weeks=[]
        for w in definitions:
            n=sum(calc.stamp(w['start'])<=p['published_at']<calc.stamp(w['end_exclusive']) for p in available)
            if not g['included'] or g['business_card']:status='excluded'
            elif n>=3:status='met'
            elif calc.stamp(w['end_exclusive'])>group_measured:status='open'
            elif not w['covered'] or g['status']!='complete':status='unknown'
            else:status='failed'
            weeks.append(dict(**w,posts=n,status=status))
        if g['included']:all_rows.extend(selected)
        posts=[dict(post_id=p['post_id'],published_at=p['published_at'],measured_at=p['measured_at'],
                    status=p['status'],is_repost=bool(p['is_repost']),url=f"https://vk.com/wall-{g['group_id']}_{p['post_id']}") for p in selected]
        groups.append(dict(group_id=g['group_id'],name=g['name'],included=bool(g['included']),business_card=bool(g['business_card']),
                           archive_status=g['status'],archive_reason=g['reason'],**stats,observed_er=observed_er,
                           observed_p33=observed_p33,weeks=weeks,failed_weeks=[w for w in weeks if w['status']=='failed'],publications=posts))
    included=[g for g in groups if g['included']]
    district=calc._aggregate(all_rows,bool(included) and calc.stamp(end)<=measured and all(g['archive_status']=='complete' for g in included))
    district['observed_er'],district['observed_p33']=calc.engagement(*(sum(p[k] for p in all_rows if p['status']=='complete') for k in ('likes','comments','reposts','views')))
    district['communities']=len(included)
    weekly=[]
    for i,w in enumerate(definitions):
        eligible=[g for g in included if not g['business_card']]
        counts={status:sum(g['weeks'][i]['status']==status for g in eligible) for status in ('met','failed','open','unknown')}
        weekly.append(dict(**w,**counts,eligible=len(eligible),p32=counts['met']/len(eligible)*100 if eligible and w['closed'] and not(counts['open'] or counts['unknown']) else None))
    return dict(run=run,runs=runs,groups=groups,district=district,weekly_summary=weekly,week_mode=week_mode,
                include_reposts=include_reposts,generated_at=datetime.now(timezone.utc).isoformat())
