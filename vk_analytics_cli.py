"""Read-only OMSU analytics; run live collection on the primary VK VM only."""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import time
from contextlib import closing
from pathlib import Path

import vk_analytics as calc
import vk_analytics_store as store
import vk_analytics_import as importer

ROOT=Path(__file__).resolve().parent


def export_report(report,comparison,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    payload={**report,'comparison':comparison}
    (out/'report.json').write_text(store.dumps(payload),encoding='utf8')
    fields=['group_id','name','posts','observed_available_posts','unavailable_known_posts',*store.COUNTERS,'reactions','er','p33','completeness',
            'archive_status','measured_from','measured_to','regularity_passed','failed_weeks','business_card']
    with (out/'communities.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';');writer.writeheader()
        for g in report['groups']:
            row={k:g.get(k) for k in fields}
            row.update(regularity_passed=g['regularity']['passed'],failed_weeks=store.dumps(g['regularity']['failed_weeks']))
            writer.writerow(row)
    if comparison['rows']:
        with (out/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(comparison['rows'][0]),delimiter=';')
            writer.writeheader();writer.writerows(comparison['rows'])
    words={'complete':'полные','partial':'неполные','likes':'Лайки','comments':'Комментарии','reposts':'Репосты',
           'views':'Просмотры','posts':'Публикации','er':'ER, %','p33':'П3.3, %','p32':'П3.2, %','p3':'П3, %'}
    def esc(v):
        if v is None:return 'UNKNOWN'
        if type(v) is bool:return 'да' if v else 'нет'
        return html.escape(words.get(v,v) if isinstance(v,str) else str(round(v,6) if isinstance(v,float) else v))
    def table(rows,columns):
        return '<table><thead><tr>'+''.join('<th>'+esc(label)+'</th>' for _,label in columns)+'</tr></thead><tbody>'+''.join(
            '<tr>'+''.join('<td>'+esc(row.get(key))+'</td>' for key,_ in columns)+'</tr>' for row in rows)+'</tbody></table>'
    d=report['district']
    text='''<!doctype html><html lang="ru"><meta charset="utf-8"><link rel="icon" href="data:,"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Сосновский округ — вовлечённость госпабликов</title><style>
    body{font:16px/1.5 system-ui;margin:24px;color:#162737;background:#f5f7fa}main{max-width:1500px;margin:auto}
    h1{font-size:26px}h2{font-size:21px;margin-top:28px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;background:white}
    th,td{text-align:left;border-bottom:1px solid #d3dce6;padding:9px;vertical-align:top}th{background:#e5edf5}
    td:first-child{max-width:420px}.note{background:#fff4db;padding:14px}small{color:#41556b}</style><main>'''
    text+='<h1>Госпаблики Сосновского муниципального округа</h1>'
    text+='<p>Период ['+esc(report['period_start'])+'; '+esc(report['period_end'])+'), YEKT. Замер UTC: '+esc(report['measurement_started'])+' — '+esc(report['measurement_finished'])+'</p>'
    text+='<p class="note">Полнота: '+esc(d['completeness'])+'. UNKNOWN означает недостаток данных, а не ноль. '
    text+='Точное время замера ЦУР неизвестно. Правила недель и репостов требуют сверки с ЦУР.</p>'
    text+='<p>Публикации: известный минимум '+esc(d['posts'])+'; доступны сейчас '+esc(d['observed_available_posts'])+\
        '; известны по истории, но недоступны '+esc(d['unavailable_known_posts'])+'. Отсутствующие счётчики не заменены нулями.</p>'
    text+='<h2>Округ: отношение сумм</h2><div class="scroll">'+table([d],list(zip(
        ('communities','posts','likes','comments','reposts','views','reactions','er','p33','p32','p3'),
        ('Сообщества','Посты','Лайки L','Комментарии K','Репосты R','Просмотры V','Реакции','ER, %','П3.3, %','П3.2, %','П3, %'))))+'</div>'
    week_label=report['variant'].split('weeks=')[1].split(';')[0]
    week_names={'period':'по7дней от начала периода','calendar_clip':'календарные, края обрезаны',
                'calendar_complete':'полные календарные','calendar_full':'все пересекающиеся календарные'}
    text+='<p>Диагностическое среднее ER сообществ: '+esc(d['diagnostic_mean_community_er'])+'% (определён ER у '+esc(d['diagnostic_mean_community_known'])+'/'+esc(d['diagnostic_mean_community_total'])+'). Недели: '+esc(week_names[week_label])+'. Репосты '+('включены' if 'reposts=True' in report['variant'] else 'исключены')+'.</p>'
    text+='<h2>Сообщества</h2><div class="scroll">'+table(report['groups'],[(k,v) for k,v in (
      ('name','Сообщество'),('posts','Посты'),('likes','L'),('comments','K'),('reposts','R'),('views','V'),('er','ER, %'),('p33','П3.3, %'),('archive_status','Архив'),('completeness','Счётчики'))])+'</div>'
    text+='<h2>Регулярность по неделям</h2><div class="scroll">'
    text+=table([dict(name=g['name'],passed=g['regularity']['passed'],excluded=g['business_card'],
               weeks='; '.join(f"{w['start']}–{w['end_exclusive']} (искл.): {w['posts']}" for w in g['regularity']['weeks']),
               failed='; '.join(w['start'] for w in g['regularity']['failed_weeks'])) for g in report['groups']],
               [('name','Сообщество'),('excluded','Визитка'),('passed','Выполнено'),('weeks','Недели и публикации'),('failed','Недели <3')])+'</div>'
    if comparison.get('official_recalculation'):
        o=comparison['official_recalculation']
        text+='<h2>Проверка формулы на числах Excel</h2><p>Сумма публикаций: '+esc(o['posts'])+'. Отношение сумм: ER '+esc(o['er_ratio_of_totals'])+'%, П3.3 '+esc(o['p33_ratio_of_totals'])+'%. Среднее ER страниц: '+esc(o['diagnostic_mean_community_er'])+'%.</p>'
    display_rows=[{**r,'group_id':r['group_id'] if r['group_id'] else 'Округ'} for r in comparison['rows']]
    text+='<h2>Сверка с ЦУР</h2><details><summary>Показать сравнение показателей по сообществам и округу</summary><div class="scroll">'+table(display_rows,[(k,v) for k,v in (
        ('group_id','VK ID / округ'),('metric','Показатель'),('official','ЦУР'),('ours','API'),('absolute_difference','Абсолютное расхождение'),
        ('relative_difference_percent','Относительное, %'),('official_as_of_date','Дата ЦУР'),('ours_measured_to','Замер API UTC'),('completeness','Полнота'))])+'</div></details>'
    text+='<h2>Ограничения и диагностика</h2><ul>'+''.join('<li>'+esc(s)+'</li>' for s in report['caveats']+comparison['limitations']+comparison['possible_causes']+comparison['missing_evidence'])+'</ul>'
    text+='<small>Правила: '+esc(store.RULES_VERSION)+'; run '+esc(report['run_id'])+'</small></main></html>'
    (out/'report.html').write_text(text,encoding='utf8')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=ROOT/'vk_analytics.sqlite3')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('import-official');p.add_argument('file',type=Path)
    p.add_argument('--start');p.add_argument('--end');p.add_argument('--as-of')
    p.add_argument('--municipality',default='Сосновский МО');p.add_argument('--registry-out',type=Path)
    p=sub.add_parser('manual');p.add_argument('--json',required=True,help='Canonical official fields, percent units; district group_id=null')
    p=sub.add_parser('collect');p.add_argument('--registry',type=Path);p.add_argument('--start');p.add_argument('--end')
    p.add_argument('--resume');p.add_argument('--live',action='store_true',required=True)
    p.add_argument('--max-requests',type=int,default=500);p.add_argument('--page-size',type=int,default=100)
    p.add_argument('--refresh-from');p.add_argument('--batch-size',type=int,default=100)
    p.add_argument('--wait-busy-seconds',type=int,default=0,help='Bounded worker wait; never retries failed API reads')
    p=sub.add_parser('probe-by-id');p.add_argument('--run',required=True);p.add_argument('--batch-size',type=int,default=2)
    p=sub.add_parser('report');p.add_argument('--run',required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--weeks',choices=('period','calendar_clip','calendar_full','calendar_complete'),default='calendar_clip')
    p.add_argument('--exclude-reposts',action='store_true');p.add_argument('--p31-ratio',type=float)
    p.add_argument('--official-import');p.add_argument('--freeze',action='store_true')
    sub.add_parser('status')
    args=parser.parse_args(argv)
    with closing(store.open_db(args.db)) as db:
        if args.command=='import-official':
            rows=importer.read_official(args.file,municipality=args.municipality,start=args.start,end=args.end,as_of=args.as_of)
            iid=store.import_official(db,rows,source=args.file.name)
            if args.registry_out:
                args.registry_out.parent.mkdir(parents=True,exist_ok=True)
                args.registry_out.write_text(store.dumps(importer.registry_from_official(rows)),encoding='utf8')
            print(store.dumps(dict(import_id=iid,measurements=len(rows),communities=sum(bool(r.get('group_id')) for r in rows))))
        elif args.command=='manual':
            row=json.loads(args.json)
            if not isinstance(row,dict):raise ValueError('Measurement JSON must be an object')
            print(store.dumps(dict(import_id=store.import_official(db,[row],source='manual'))))
        elif args.command in ('collect','probe-by-id'):
            from vk_analytics_collect import LiveReader,collect_run,refresh_known
            from vk_scheduler import singleton
            with singleton(ROOT/'.vk-analytics.guard'):
                reader=LiveReader(ROOT,max_requests=getattr(args,'max_requests',1))
                if args.command=='probe-by-id':
                    if not 1<=args.batch_size<=100:raise ValueError('Batch must be1..100')
                    rows=db.execute('SELECT group_id,post_id FROM analytics_snapshots WHERE run_id=? AND status IN (?,?) LIMIT ?',
                                    (args.run,'complete','missing_counters',args.batch_size)).fetchall()
                    if not rows:raise ValueError('Collect known post IDs before probing')
                    code=None
                    try:
                        result=reader.wall_get_by_id([(r[0],r[1]) for r in rows],batch_size=args.batch_size)
                        status='supported';received=len(result)
                    except Exception as exc:
                        code=getattr(exc,'code',None);received=0
                        status='unsupported' if code==1051 else 'error'
                    db.execute('INSERT INTO analytics_capabilities(at,method,batch_size,status,code) VALUES(?,?,?,?,?)',
                               (store.now(),'wall.getById',len(rows),status,code))
                    print(store.dumps(dict(method='wall.getById',requested=len(rows),received=received,status=status,code=code)))
                else:
                    if args.resume:
                        rid=args.resume
                        if args.registry or args.start or args.end:raise ValueError('Resume uses frozen period and registry; omit new selection')
                    else:
                        if not args.registry or not args.start or not args.end:raise ValueError('New run requires registry/start/end')
                        groups=json.loads(args.registry.read_text(encoding='utf-8-sig'))
                        rid=store.new_run(db,args.start,args.end,groups)
                    run_period=db.execute('SELECT period_start,period_end FROM analytics_runs WHERE id=?',(rid,)).fetchone()
                    if run_period is None:raise ValueError('Unknown analytics run')
                    ids=[r[0] for r in db.execute('SELECT group_id FROM analytics_run_groups WHERE run_id=? AND included=1',(rid,))]
                    store.capture_catalog_references(db,rid,reader.catalog_references(run_period[0],run_period[1],ids))
                    print(store.dumps(dict(run_id=rid)),flush=True)
                    if args.refresh_from:
                        result=refresh_known(db,rid,args.refresh_from,reader,batch_size=args.batch_size)
                        if result=='unsupported':
                            print(store.dumps(dict(fallback='wall.get',reason='wall.getById_1051')),flush=True)
                            collect_run(db,rid,reader.wall_get,page_size=args.page_size)
                    else:
                        if not 0<=args.wait_busy_seconds<=900:raise ValueError('Busy wait must be0..900 seconds')
                        deadline=time.monotonic()+args.wait_busy_seconds
                        while True:
                            collect_run(db,rid,reader.wall_get,page_size=args.page_size)
                            deferred=db.execute("SELECT 1 FROM analytics_run_groups WHERE run_id=? AND status='deferred' LIMIT 1",(rid,)).fetchone()
                            if not deferred or time.monotonic()>=deadline:break
                            with db:db.execute("UPDATE analytics_runs SET status='waiting_worker',finished_at=NULL WHERE id=?",(rid,))
                            time.sleep(min(5,max(0,deadline-time.monotonic())))
                    print(store.dumps(dict(run_id=rid,requests=reader.used)),flush=True)
        elif args.command=='report':
            report=calc.report(db,args.run,weeks=args.weeks,include_reposts=not args.exclude_reposts,p31_ratio=args.p31_ratio)
            official=[json.loads(r[0]) for r in db.execute('SELECT payload FROM analytics_official WHERE import_id=?',(args.official_import,))] if args.official_import else []
            comparison=calc.compare(report,official)
            export_report(report,comparison,args.out)
            frozen=store.freeze(db,{**report,'comparison':comparison}) if args.freeze else None
            print(store.dumps(dict(report_id=frozen,district=report['district'],out=str(args.out.resolve()))))
        else:
            print(store.dumps([dict(r) for r in db.execute('SELECT * FROM analytics_runs ORDER BY started_at DESC LIMIT 10')]))
        db.commit()
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        # No raw exceptions/URLs/tokens in live output.
        print(store.dumps(dict(status='error',error_type=type(exc).__name__,code=getattr(exc,'code',None))),file=sys.stderr)
        sys.exit(2)
