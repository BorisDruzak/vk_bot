"""OMSU order111: totals before ratios; NULL remains unknown, never reach."""
from __future__ import annotations

import math
from decimal import Decimal
from datetime import date,datetime,time,timedelta,timezone
import vk_analytics_store as store

YEKT=timezone(timedelta(hours=5))


def engagement(likes,comments,reposts,views):
    values=(likes,comments,reposts,views)
    if any(v is not None and (type(v) not in (int,float) or not math.isfinite(v) or v<0) for v in values):
        raise ValueError('Finite nonnegative counters required')
    if any(v is None for v in values) or views==0:return None,None
    er=sum(Decimal(str(v)) for v in (likes,comments,reposts))/Decimal(str(views))*100
    return float(er),float(min(Decimal(100),er/Decimal('3.6')*100))


def stamp(day):
    return int(datetime.combine(date.fromisoformat(day),time(),YEKT).timestamp())


def measurement_timestamp(run,snapshots=(),*,observed_from=None):
    """Conservative saved coverage bound, including empty walls and old cache.

    A scan spanning midnight cannot prove what was posted after its head read.
    Use the earliest saved run/page/post time, never the report clock. Legacy
    empty archives without page evidence remain unknown until a new collection.
    """
    if not run['finished_at'] or (not snapshots and observed_from is None):return None
    values=[run['started_at'],run['finished_at'],*(p['measured_at'] for p in snapshots)]
    if observed_from is not None:values.append(observed_from)
    try:
        dates=[datetime.fromisoformat(value) for value in values]
        if any(d.tzinfo is None or d.utcoffset() is None for d in dates):return None
        return min(d.timestamp() for d in dates)
    except (ValueError,TypeError,OverflowError):return None


def week_windows(start,end,mode='calendar_clip'):
    a,b=store.valid_period(start,end)
    if mode not in ('period','calendar_clip','calendar_full','calendar_complete'):
        raise ValueError('Unknown week boundary mode')
    cursor=a if mode=='period' else a-timedelta(days=a.weekday())
    result=[]
    while cursor<b:
        right=cursor+timedelta(days=7)
        if mode!='calendar_complete' or (cursor>=a and right<=b):
            left_edge=cursor if mode=='calendar_full' else max(a,cursor)
            right_edge=right if mode=='calendar_full' else min(b,right)
            result.append((left_edge.isoformat(),right_edge.isoformat()))
        cursor=right
    return result


def regularity(dates,start,end,mode='calendar_clip'):
    weeks=[dict(start=a,end_exclusive=b,posts=sum(stamp(a)<=d<stamp(b) for d in dates))
           for a,b in week_windows(start,end,mode)]
    failed=[w for w in weeks if w['posts']<3]
    return dict(passed=(not failed) if weeks else None,weeks=weeks,failed_weeks=failed)


def _aggregate(rows,archive_complete):
    known={k:sum(r[k] for r in rows if r[k] is not None) for k in store.COUNTERS}
    coverage={k:sum(r[k] is not None for r in rows) for k in store.COUNTERS}
    complete=archive_complete and all(r['status']=='complete' for r in rows)
    values={k:known[k] if archive_complete and coverage[k]==len(rows) else None for k in store.COUNTERS}
    er,p33=engagement(*(values[k] for k in store.COUNTERS)) if complete else (None,None)
    common=[r for r in rows if r['status']=='complete']
    subset=engagement(*(sum(r[k] for r in common) for k in store.COUNTERS))
    return dict(**values,posts=len(rows),known_sums=known,coverage=coverage,
                observed_available_posts=sum(r['status'] in ('complete','missing_counters') for r in rows),
                unavailable_known_posts=sum(r['status'] not in ('complete','missing_counters') for r in rows),
                reactions=sum(values[k] for k in ('likes','comments','reposts'))
                    if all(values[k] is not None for k in ('likes','comments','reposts')) else None,
                er=er,p33=p33,completeness='complete' if complete else 'partial',
                diagnostic_complete_post_subset_er=subset[0],complete_counter_posts=len(common))


def p31_penalty(ratio):
    if ratio is None:return None
    if not 0<=ratio<=1:raise ValueError('P3.1 compliance ratio must be 0..1')
    # Twelve explicit bands avoid floating point boundary subtraction artifacts.
    for threshold,penalty in ((.99,0),(.96,-2),(.93,-4),(.90,-6),(.87,-8),(.84,-10),
                              (.81,-12),(.78,-14),(.75,-16),(.72,-18),(.69,-20)):
        if ratio>=threshold:return penalty
    return -22


def report(db,run,*,weeks='calendar_clip',include_reposts=True,p31_ratio=None):
    row=db.execute('SELECT * FROM analytics_runs WHERE id=?',(run,)).fetchone()
    if row is None:raise ValueError('Unknown analytics run')
    start,end=row['period_start'],row['period_end'];groups=[];all_rows=[];archive_coverage=[]
    registry=[dict(r) for r in db.execute('SELECT * FROM analytics_run_groups WHERE run_id=? ORDER BY group_id',(run,))]
    for g in registry:
        if not g['included']:continue
        snapshots=[dict(s) for s in db.execute('SELECT * FROM analytics_snapshots WHERE run_id=? AND group_id=? ORDER BY published_at,post_id',(run,g['group_id']))]
        measured=measurement_timestamp(row,snapshots,observed_from=g.get('observed_from'))
        period_elapsed=measured is not None and stamp(end)<=measured
        archive_coverage.append(g['status']=='complete' and period_elapsed)
        usable=[s for s in snapshots if stamp(start)<=s['published_at']<stamp(end) and (include_reposts or not s['is_repost'])]
        stats=_aggregate(usable,g['status']=='complete' and period_elapsed);all_rows.extend(usable)
        reg=regularity([s['published_at'] for s in snapshots if include_reposts or not s['is_repost']],start,end,weeks)
        windows=week_windows(start,end,weeks)
        if g['status']!='complete' or not period_elapsed or (weeks=='calendar_full' and any(a<start or b>end for a,b in windows)):
            reg['passed']=None;reg['reason']='archive_or_boundary_incomplete'
        if g['business_card']:
            reg['passed']=None;reg['reason']='business_card_excluded'
        groups.append(dict(group_id=g['group_id'],name=g['name'],business_card=bool(g['business_card']),
                           archive_status=g['status'],archive_reason=g['reason'],**stats,regularity=reg,
                           measured_from=min((s['measured_at'] for s in usable),default=None),
                           measured_to=max((s['measured_at'] for s in usable),default=None)))
    district=_aggregate(all_rows,bool(groups) and all(archive_coverage))
    eligible=[g for g in groups if not g['business_card']]
    passed=sum(g['regularity']['passed'] is True for g in eligible)
    p32=min(100.0,passed/len(eligible)*100) if eligible and all(g['regularity']['passed'] is not None for g in eligible) else None
    known_er=[g['er'] for g in groups if g['er'] is not None]
    mean=sum(known_er)/len(known_er) if known_er else None
    district.update(p32=p32,p32_passed=passed,p32_eligible=len(eligible),communities=len(groups),
                    diagnostic_mean_community_er=mean,
                    diagnostic_mean_community_known=len(known_er),
                    diagnostic_mean_community_total=len(groups),
                    diagnostic_mean_community_completeness='complete' if len(known_er)==len(groups) else 'partial',
                    diagnostic_mean_community_p33=min(100.0,mean/3.6*100) if mean is not None else None,
                    p31_penalty=p31_penalty(p31_ratio),p3=None)
    if district['p31_penalty'] is not None and p32 is not None and district['p33'] is not None:
        district['p3']=district['p31_penalty']+.5*p32+.5*district['p33']
    return dict(run_id=run,period_start=start,period_end=end,report_month=start[:7],
                generated_at=store.now(),measurement_started=row['started_at'],measurement_finished=row['finished_at'],
                rules_version=store.RULES_VERSION,variant=f'sum;weeks={weeks};reposts={include_reposts}',
                registry=registry,district=district,groups=groups,
                caveats=['Границы недель, репосты, страницы-визитки и состав реестра требуют сверки с ЦУР.',
                         'Счётчики накоплены к моменту API-чтения у постов, опубликованных в периоде; это не приращение за период.',
                         'Нет исторических снимков на прежнюю дату — нельзя восстановить прежние счётчики.',
                         'Число постов — известный минимум; исчезнувшие записи из прежнего API-каталога дают partial и NULL.',
                         'Неполный архив или NULL не дают окончательного ER. V — views.count, не охват.',
                         'calendar_full требует данных вне периода: без них регулярность UNKNOWN.'])


def compare(report,official):
    comparisons=[];limits=[]
    for o in official:
        gid=o.get('group_id');ours=next((g for g in report['groups'] if g['group_id']==gid),None) if gid else report['district']
        mismatch=any(o.get(k)!=report[k] for k in ('period_start','period_end'))
        if mismatch:limits.append('Период официального измерения неизвестен или отличается; числовая сверка отключена.')
        if o.get('p33') is not None and not all(o.get(k) is not None for k in store.COUNTERS):
            limits.append('Одного П3.3 недостаточно для восстановления исходных счётчиков, постов или способа агрегации.')
        for key in (*store.COUNTERS,'posts','er','p33','p32','p3'):
            if o.get(key) is None:continue
            own=ours.get(key) if ours and not mismatch else None
            delta=abs(own-o[key]) if own is not None else None
            comparisons.append(dict(group_id=gid,metric=key,official=o[key],ours=own,
                absolute_difference=delta,relative_difference_percent=delta/abs(o[key])*100 if delta is not None and o[key] else None,
                method='ratio_of_totals' if key in ('er','p33') else 'count_or_methodology',
                official_measured_at=o.get('measured_at'),official_as_of_date=o.get('as_of_date'),
                ours_measured_from=ours.get('measured_from',report['measurement_started']) if ours else None,
                ours_measured_to=ours.get('measured_to',report['measurement_finished']) if ours else None,
                completeness=ours.get('completeness') if ours else 'not_in_registry',period_matches=not mismatch))
        if o.get('period_status')!='confirmed':
            limits.append('Границы периода ЦУР пока не подтверждены; числовая сверка условна при выбранных датах.')
    community_rows=[r for r in official if r.get('group_id')]
    our_ids={r['group_id'] for r in report['groups']};official_ids={r['group_id'] for r in community_rows}
    unique=len(community_rows)==len(official_ids)
    sums={k:sum(r[k] for r in community_rows) if community_rows and all(r.get(k) is not None for r in community_rows) and unique else None for k in store.COUNTERS}
    official_er,official_p33=engagement(*(sums[k] for k in store.COUNTERS))
    mean_er=sum(r['er'] for r in community_rows)/len(community_rows) if community_rows and all(r.get('er') is not None for r in community_rows) and unique else None
    uncapped=sum(r.get('p33') is not None and r['p33']>100 for r in community_rows)
    if uncapped:limits.append(f'У {uncapped} страниц в исходном Excel П3.3 выше100%. Исходные значения сохранены, наша методика ограничивает100%.')
    return dict(rows=comparisons,limitations=sorted(set(limits)),
        registry_comparison=dict(official=len(official_ids),ours=len(our_ids),
            missing_in_ours=sorted(official_ids-our_ids),extra_in_ours=sorted(our_ids-official_ids),duplicate_official_ids=not unique),
        official_recalculation=dict(**sums,er_ratio_of_totals=official_er,p33_ratio_of_totals=official_p33,
            diagnostic_mean_community_er=mean_er,
            posts=sum(r['posts'] for r in community_rows) if community_rows and all(r.get('posts') is not None for r in community_rows) and unique else None),
        possible_causes=[
        'Разные моменты фиксации статистики','Разный состав госпабликов','Неполный архив публикаций',
        'Разные правила включения репостов','Разные границы отчётного периода',
        'Суммарная или средняя вовлечённость','Удалённые или недоступные публикации'],
        missing_evidence=['Точное время замера ЦУР и часовой пояс','Список ID постов официальной выборки',
                          'Подтверждение периода из имени файла и правил недель/репостов/визиток'])
