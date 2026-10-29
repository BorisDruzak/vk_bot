"""VM-native, bounded OMSU snapshots. No browser or worker control operations."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import socket
import ssl
import sys
import uuid
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path

import vk_analytics as calc
import vk_analytics_store as store
from vk_analytics_cli import export_report
from vk_analytics_collect import LiveReader,Deferred,collect_run

ROOT=Path(__file__).resolve().parent
MAX_READS=250


def utcnow():return datetime.now(timezone.utc)


def select_slot(at):
    if at.tzinfo is None:raise ValueError('Aware measurement time required')
    local=at.astimezone(calc.YEKT)
    if local.hour not in (8,20) or local.minute>=15:return None
    return dict(key=local.strftime('%Y%m%d-')+f'{local.hour:02d}00',
                cutoff=local.replace(minute=40,second=0,microsecond=0),day=local.date())


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    try:
        with temporary.open('w',encoding='utf8') as stream:
            stream.write(store.dumps(value));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def read_selection(manifest_path,registry_path):
    manifest=json.loads(Path(manifest_path).read_text(encoding='utf-8-sig'))
    raw=Path(registry_path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['registry_sha256']:raise ValueError('Frozen registry mismatch')
    groups=json.loads(raw.decode('utf-8-sig'))
    if (not isinstance(groups,list) or not groups or len(groups)!=manifest['communities']
        or len({g['id'] for g in groups})!=len(groups)
        or any(type(g['id']) is not int or g['id']<=0 for g in groups)):
        raise ValueError('Invalid frozen registry')
    return manifest,groups


def export_posts(db,run,out):
    rows=db.execute('''SELECT s.*,p.url FROM analytics_snapshots s JOIN analytics_publications p
       ON p.group_id=s.group_id AND p.post_id=s.post_id WHERE s.run_id=? ORDER BY s.group_id,s.post_id''',(run,))
    with (out/'posts.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream,delimiter=';');writer.writerow([x[0] for x in rows.description]);writer.writerows(rows)


def _run_locked(root,manifest_path,registry_path,slot,clock,reader_factory):
    manifest,groups=read_selection(manifest_path,registry_path)
    out=root/'analytics_local'/'observation-from-2026-10-14'/'scheduled'/slot['key']
    out.mkdir(parents=True,exist_ok=True);checkpoint=out/'checkpoint.json'
    with closing(store.open_db(root/'vk_analytics.sqlite3')) as db:
        if checkpoint.exists():
            state=json.loads(checkpoint.read_text(encoding='utf8'))
            if state['phase']=='finished':return state['result']
            row=db.execute('SELECT * FROM analytics_runs WHERE id=?',(state['run_id'],)).fetchone()
            if row is None or row['period_start']!=manifest['period_start'] or row['period_end']!=state['period_end']:
                raise ValueError('Invalid schedule checkpoint')
        else:
            end=(slot['day']+timedelta(days=1)).isoformat()
            identity=uuid.uuid5(uuid.NAMESPACE_URL,f"vk-omsu-schedule:{root.resolve()}:{slot['key']}:{manifest['period_start']}:{manifest['registry_sha256']}").hex
            rid=store.new_run(db,manifest['period_start'],end,groups,run_id=identity)
            state=dict(run_id=rid,phase='collecting',period_end=end,attempted_reads=0,
                       deadline=min(slot['cutoff'],clock()+timedelta(minutes=25)).isoformat())
            # Before any API access; an interruption resumes the same run.
            atomic_json(checkpoint,state)
        rid=state['run_id']
        # Honor the durable collector outcome even if the subsequent JSON
        # checkpoint write was interrupted. Never retry a recorded VK error.
        reasons=[r[0] for r in db.execute('SELECT reason FROM analytics_run_groups WHERE run_id=?',(rid,)) if r[0]]
        errors=[r[0] for r in db.execute('SELECT code FROM analytics_errors WHERE run_id=?',(rid,))]
        read_error=any(r.startswith(('read_','api_code_')) for r in reasons+errors)
        finished=db.execute('SELECT finished_at FROM analytics_runs WHERE id=?',(rid,)).fetchone()[0]
        if state['phase']=='collecting' and (read_error or finished):
            state.update(phase='collected',status='read_error' if read_error else state.get('last_stop','deferred_worker' if 'worker_or_profile_busy' in reasons else 'saved'))
            atomic_json(checkpoint,state)
        if state['phase']=='collecting':
            stop=None
            def deadline_guard():
                nonlocal stop
                if clock()>=datetime.fromisoformat(state['deadline']):
                    state['last_stop']='deferred_deadline';atomic_json(checkpoint,state)
                    stop='deferred_deadline';raise Deferred('deadline')
            reader=reader_factory(root,max_requests=max(1,MAX_READS-state['attempted_reads']),before_request=deadline_guard)
            store.capture_catalog_references(db,rid,reader.catalog_references(manifest['period_start'],state['period_end'],[g['id'] for g in groups]))
            def fetch(gid,offset,count):
                nonlocal stop
                deadline_guard()
                if state['attempted_reads']>=MAX_READS:
                    state['last_stop']='deferred_budget';atomic_json(checkpoint,state)
                    stop='deferred_budget';raise Deferred('budget')
                # Reserve against the slot budget before a possible crash.
                state['attempted_reads']+=1;atomic_json(checkpoint,state)
                return reader.wall_get(gid,offset,count)
            # Match queue/statistics page identity for shared observations.
            collect_run(db,rid,fetch,page_size=100)
            reasons=[r[0] for r in db.execute('SELECT reason FROM analytics_run_groups WHERE run_id=?',(rid,)) if r[0]]
            if any(r.startswith(('read_','api_code_')) for r in reasons):status='read_error'
            elif stop:status=stop
            elif 'worker_or_profile_busy' in reasons:status='deferred_worker'
            else:status='saved'
            state.update(phase='collected',status=status);atomic_json(checkpoint,state)
        previous=db.execute('SELECT id,payload FROM analytics_reports WHERE run_id=? ORDER BY fixed_at DESC LIMIT 1',(rid,)).fetchone()
        if previous:
            report=json.loads(previous['payload']);comparison=report['comparison'];report_id=previous['id']
        else:
            report=calc.report(db,rid,weeks='period');comparison=calc.compare(report,[])
            report_id=store.freeze(db,{**report,'comparison':comparison})
        export_report(report,comparison,out);export_posts(db,rid,out)
        result=dict(status=state['status'],run_id=rid,report_id=report_id,output=str(out),
                    completeness=report['district']['completeness'],posts=report['district']['posts'],
                    attempted_reads=state['attempted_reads'])
        state.update(phase='finished',result=result);atomic_json(checkpoint,state)
        return result


def run_job(root,manifest_path,registry_path,*,clock=utcnow,reader_factory=LiveReader):
    root=Path(root);slot=select_slot(clock())
    if slot is None:return dict(status='skipped_outside_slot')
    from vk_scheduler import singleton
    from vk_unified import RunAlreadyActive
    try:
        with singleton(root/'.vk-analytics.guard'):
            return _run_locked(root,manifest_path,registry_path,slot,clock,reader_factory)
    except RunAlreadyActive:return dict(status='skipped_analytics_busy')


def preflight(root,manifest_path,registry_path):
    """Key/binding/TLS access under the unattended task; zero VK API calls."""
    manifest,groups=read_selection(manifest_path,registry_path)
    reader=LiveReader(root,max_requests=1)
    with closing(store.open_db(root/'vk_analytics.sqlite3')) as db:
        db.execute('SELECT COUNT(*) FROM analytics_runs').fetchone()
    with socket.create_connection(('api.vk.com',443),timeout=10) as conn:
        with ssl.create_default_context().wrap_socket(conn,server_hostname='api.vk.com') as tls:
            network=bool(tls.version())
    return dict(status='preflight_ok',groups=len(groups),key_available=bool(reader.token),
                tls=network,api_requests=reader.used,period_start=manifest['period_start'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight',action='store_true')
    args=parser.parse_args(argv)
    manifest=ROOT/'analytics_local/observation-from-2026-10-14.json'
    registry=ROOT/'analytics_local/observation-from-2026-10-14/registry.json'
    events=ROOT/'analytics_local/observation-from-2026-10-14/schedule-events.jsonl'
    code=0
    try:result=preflight(ROOT,manifest,registry) if args.preflight else run_job(ROOT,manifest,registry)
    except Exception as exc:
        result=dict(status='error',error_type=type(exc).__name__);code=2
    if result['status']=='read_error':code=2
    record=dict(at=store.now(),**result)
    events.parent.mkdir(parents=True,exist_ok=True)
    with events.open('a',encoding='utf8') as stream:stream.write(store.dumps(record)+'\n')
    atomic_json(events.with_name('preflight.json' if args.preflight else 'latest-scheduled.json'),record)
    print(store.dumps(record));return code


if __name__=='__main__':sys.exit(main())
