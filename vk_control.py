"""Fixed JSON control boundary. Never runs arbitrary shell or engagement requests."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing,contextmanager,ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vk_accounts as accounts
import vk_batch as engine
import vk_schedule_policy as policy


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)


def login_path(account):
    accounts.profile_for_account(account)
    return accounts.ROOT/f'.vk-login-{account}.json'


def _login_status_raw(account):
    path=login_path(account)
    if not path.exists():return {'state':'idle'}
    row=json.loads(path.read_text(encoding='utf-8'))
    if row.get('state') in ('opening','waiting_browser','awaiting_login','ready','wrong_account'):
        at=datetime.fromisoformat(row['at'])
        if (datetime.now(timezone.utc)-at).total_seconds()>90:
            return {**row,'state':'stale','reason':'login_process_not_reporting'}
    return row


def login_status(account):
    import vk_authentication as authentication
    row=_login_status_raw(account)
    proof=authentication.status(account)
    if row.get('flow')=='add_any':
        if row.get('state')=='complete' and type(row.get('canonical_account')) is int:
            proof=authentication.status(row['canonical_account'])
        else:
            proof={'state':'unverified','expected_vk_id':None,'observed_vk_id':None,
                   'fresh':False,'can_finish':False,'checked_at':None}
    # A legacy worker's detected=True never proved WHICH account was open.
    if row.get('state') in ('ready','complete') and row.get('verification_version')!=2:
        row={**row,'state':'unverified','reason':'legacy_identity_not_compared'}
    return {**row,'authentication':proof}


def desktop_session_id():
    """Determine our own Windows desktop session, never infer from other processes."""
    if os.name!='nt':return 0
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    call=kernel.ProcessIdToSessionId
    call.argtypes=(wintypes.DWORD,ctypes.POINTER(wintypes.DWORD))
    call.restype=wintypes.BOOL
    session=wintypes.DWORD()
    if not call(os.getpid(),ctypes.byref(session)):
        raise accounts.AccountError('desktop_session_unavailable')
    return session.value


def launch_login_task(account,session,*,check_only=False):
    """Use the web desktop directly; only an SSH/service caller needs a task."""
    if os.name!='nt':raise accounts.AccountError('login_requires_windows_desktop')
    def quote(value):return "'"+str(value).replace("'","''")+"'"
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():raise accounts.AccountError('pythonw_missing')
    if desktop_session_id()>0:
        # The panel already runs in InteractiveToken session. Re-registering an
        # old SSH-created task would fail its ACL under the filtered user token.
        subprocess.Popen([str(pythonw),'-X','utf8',str(Path(__file__).resolve()),
                          '--login-worker',str(account),'--session',session,*(['--check-only'] if check_only else [])],
                         cwd=accounts.ROOT,stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return
    args=f'-X utf8 "{Path(__file__).resolve()}" --login-worker {account} --session {session}'+(' --check-only' if check_only else '')
    code=(f"$ErrorActionPreference='Stop'; $user=[Security.Principal.WindowsIdentity]::GetCurrent().Name; "
          f"$action=New-ScheduledTaskAction -Execute {quote(pythonw)} -Argument {quote(args)} -WorkingDirectory {quote(accounts.ROOT)}; "
          "$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited; "
          "$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -MultipleInstances IgnoreNew; "
          f"Register-ScheduledTask -TaskName 'VKUnifiedLogin{account}' -Action $action -Principal $principal -Settings $settings -Force | Out-Null; "
          f"Start-ScheduledTask -TaskName 'VKUnifiedLogin{account}'")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',
                           base64.b64encode(code.encode('utf-16-le')).decode()],
                          capture_output=True,timeout=35)
    if result.returncode:raise accounts.AccountError('interactive_login_task_failed')


def start_login(account,*,check_only=False):
    if accounts.account_enabled(account) and not check_only:raise accounts.AccountError('account_must_be_disabled_for_login')
    scheduler=accounts.ROOT/'vk_scheduler.sqlite3'
    if scheduler.exists():
        with closing(readonly(scheduler)) as db:
            if db.execute("SELECT 1 FROM scheduler_jobs WHERE account=? AND status IN ('dispatching','attempting') LIMIT 1",(account,)).fetchone():
                raise accounts.AccountError('account_operation_finishing')
    with accounts.management_lock():
        current=_login_status_raw(account)
        if current['state'] in ('opening','waiting_browser','awaiting_login','ready','wrong_account'):return login_status(account)
        import vk_authentication as authentication
        authentication.invalidate(account)
        session=uuid.uuid4().hex
        row={'state':'opening','session':session,'at':datetime.now(timezone.utc).isoformat(),
             'desktop':'192.168.100.46','account':account}
        import vk_connections
        if not check_only and vk_connections.enabled():row['flow']='add_any'
        accounts.atomic_json(login_path(account),row)
        try:launch_login_task(account,session,check_only=check_only)
        except Exception:
            accounts.atomic_json(login_path(account),{**row,'state':'error','reason':'interactive_login_task_failed'})
            raise
        return row


def check_login(account):
    current=_login_status_raw(account)
    if current['state'] in ('opening','waiting_browser','awaiting_login','ready','wrong_account'):
        return login_status(account)
    return start_login(account,check_only=True)


def finish_login(account):
    current=_login_status_raw(account)
    if current['state']!='ready':return current
    import vk_authentication as authentication
    if current.get('verification_version')!=2 or not authentication.may_finish(account):
        raise accounts.AccountError('account_authentication_required')
    # Separate command file avoids overwriting the worker's heartbeat atomically.
    accounts.atomic_json(login_path(account).with_suffix('.finish.json'),{'session':current['session']})
    return {'state':'finishing','account':account}


@contextmanager
def login_browser_lease(save):
    import vk_activity
    import vk_unified as app
    until=time.monotonic()+1800
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(vk_activity.shared_activity(accounts.ROOT))
                break
            except app.RunAlreadyActive:
                if time.monotonic()>=until:raise accounts.AccountError('browser_wait_timeout')
                save('waiting_browser',reason='worker_batch_busy')
                time.sleep(2)
        yield


def login_worker(account,session,*,check_only=False):
    from playwright.sync_api import sync_playwright
    import vk_authentication as authentication
    import vk_connections as connections
    automatic=not check_only and connections.enabled()
    launched_profile=accounts.profile_for_account(account)
    path=login_path(account)
    def save(state,**extra):
        current=json.loads(path.read_text(encoding='utf-8'))
        if current.get('session')!=session:raise accounts.AccountError('login_session_replaced')
        accounts.atomic_json(path,{'account':account,'session':session,'state':state,
                                 'verification_version':2,'at':datetime.now(timezone.utc).isoformat(),
                                 **({'flow':'add_any'} if automatic else {}),**extra})
    try:
        if accounts.account_enabled(account) and not check_only:raise accounts.AccountError('account_enabled')
        with login_browser_lease(save),accounts.exclusive_profile(account),sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(str(launched_profile),
                headless=False,locale='ru-RU',viewport={'width':1320,'height':900})
            try:
                page=context.new_page()
                loaded=False
                try:
                    response=page.goto('https://vk.com/feed',wait_until='domcontentloaded',timeout=35000)
                    loaded=response is not None and type(response.status) is int and 200<=response.status<400
                except Exception:pass # Identity is still required; no actions are possible here.
                save('opening' if check_only else 'awaiting_login',browser_opened=True,page_loaded=loaded)
                if check_only:
                    identity=accounts.inspect_context(context,attempts=15)
                    proof=authentication.observe(account,identity)
                    save('checked',detected=identity is not None,authentication=proof)
                    return 0 if proof['state']=='matched' else 2
                until=time.monotonic()+1200
                while time.monotonic()<until:
                    if accounts.account_enabled(account):raise accounts.AccountError('account_enabled')
                    if not context.pages:save('closed');return 1
                    identity=accounts.inspect_context(context,attempts=1)
                    if automatic:
                        if identity is not None:
                            target=connections.connect(account,identity,profile=launched_profile)
                            save('complete',canonical_account=target,browser_opened=True,
                                 authentication=authentication.status(target));return 0
                        save('awaiting_login',browser_opened=True,page_loaded=loaded)
                        for tab in list(context.pages):
                            try:tab.evaluate("title => { document.title = title; }",'VK — '+accounts.account_record(account)['name'])
                            except Exception:pass
                        context.pages[-1].wait_for_timeout(2000)
                        continue
                    proof=authentication.observe(account,identity)
                    state=('ready' if proof['can_finish'] else 'wrong_account' if proof['state']=='wrong_account' else 'awaiting_login')
                    save(state,detected=identity is not None,authentication=proof)
                    for tab in list(context.pages):
                        try:
                            tab.evaluate("prefix => { if (!document.title.startsWith(prefix)) document.title = prefix + document.title; }",
                                         f'VK Unified — профиль №{account} | ')
                        except Exception:pass # Window label never establishes identity.
                    finish=path.with_suffix('.finish.json')
                    if proof['can_finish'] and finish.exists() and json.loads(finish.read_text(encoding='utf-8')).get('session')==session:
                        identity=accounts.inspect_context(context,attempts=2)
                        if identity is None:continue
                        proof=authentication.observe(account,identity)
                        if not proof['can_finish']:continue
                        accounts.register_detected_id(accounts.BINDINGS_PATH,account,identity)
                        proof=authentication.observe(account,identity)
                        save('complete',bound=True,authentication=proof);finish.unlink(missing_ok=True);return 0
                    context.pages[-1].wait_for_timeout(2000)
                save('expired');return 1
            finally:context.close()
    except Exception as exc:
        import traceback
        frames=[{'file':Path(frame.filename).name,'line':frame.lineno}
                for frame in traceback.extract_tb(exc.__traceback__)
                if Path(frame.filename).resolve().parent==accounts.ROOT]
        save('error',reason=type(exc).__name__,frames=frames,
             os_error=getattr(exc,'winerror',None) or getattr(exc,'errno',None))
        return 2


def snapshot():
    from vk_action_retry import RetryLedger
    import vk_scheduler_state as ledger
    import vk_scheduler
    import vk_statistics
    import vk_connections
    now=datetime.now(timezone.utc)
    paths={n:accounts.database_for_account(n) for n in accounts.account_numbers()}
    base,_=vk_scheduler.load_settings(accounts.ROOT/'vk_automation.json')
    result={'now':now.isoformat(),'execution_host':'192.168.100.46','accounts':[],
            'scheduler':{},'jobs':[],'posts':[],'groups':[]}
    if vk_connections.enabled():result['account_flow']='add_any'
    import vk_statistics_job
    result['statistics_refresh']=vk_statistics_job.status()
    source=paths[1]
    if source.exists():
        result['posts']=vk_statistics.posts(source)
        result['groups']=vk_statistics.groups(source)
    bindings=accounts.load_bindings(accounts.BINDINGS_PATH)['accounts']
    usage={};remaining={};effective=base
    scheduler=accounts.ROOT/'vk_scheduler.sqlite3'
    if scheduler.exists():
        with closing(readonly(scheduler)) as db:
            db.row_factory=sqlite3.Row
            state=ledger.State.__new__(ledger.State)
            state.db=db;state.config=base;state.paths={k:v for k,v in paths.items() if v.exists()}
            retry=RetryLedger(state)
            state.policy_provider=lambda:accounts.effective_policy(base)
            state.enabled_provider=accounts.account_enabled
            state.refresh_policy(now);effective=state.config
            usage=state.usage(now);remaining=state.remaining(now)
            saved=db.execute('SELECT slots FROM schedule_days WHERE day=?',(str(policy.local_day(now)),)).fetchone()
            latest=db.execute('SELECT MAX(day) FROM schedule_days').fetchone()[0]
            day=(max(policy.local_day(now),datetime.fromisoformat(latest).date())+timedelta(days=1) if latest else policy.local_day(now))
            result['scheduler']={'heartbeat':state.get('heartbeat'),'paused':state.get('paused'),
                'active_uncertain':state.active_uncertain_count(),
                'isolated_posts':db.execute('SELECT COUNT(*) FROM scheduler_post_isolations').fetchone()[0],
                'slots':json.loads(saved[0]) if saved else [],
                'batches':[dict(r) for r in db.execute('SELECT * FROM scheduler_batches WHERE day=? ORDER BY slot',(str(policy.local_day(now)),))],
                'policy_effective_day':str(day),'coverage':state.get('scan_coverage'),
                'last_diagnostic_error':state.get('last_diagnostic_error')}
            result['scheduler']['action_retry']=retry.summary()
            result['jobs']=[dict(r) for r in db.execute('SELECT * FROM scheduler_jobs ORDER BY id DESC LIMIT 200')]
            for job in result['jobs']:job['recovery']=retry.job_snapshot(job['id'])
    for number in paths:
        row=accounts.account_record(number);bound=bindings.get(str(number),{})
        if result.get('account_flow')=='add_any' and row.get('archived'):continue
        used={kind:sum(n for (a,k,_),n in usage.items() if a==number and k==kind)
              for kind in ('like','personal','community')}
        left={kind:sum(n for (a,k,_),n in remaining.items() if a==number and k==kind)
              for kind in used}
        result['accounts'].append({**row,'bound':type(bound.get('vk_id')) is int,
            'vk_id':bound.get('vk_id'),'profile_exists':accounts.profile_for_account(number).is_dir(),
            'usage':used,'remaining':left,'effective_policy':effective['accounts'].get(str(number)),
            'login':login_status(number),'authentication':__import__('vk_authentication').status(number)})
    pending=accounts.effective_policy(base)
    for key,cfg in (('work_window',effective),('pending_work_window',pending)):
        result['scheduler'][key]={name:cfg[name] for name in ('start_hour','end_hour')}
    return result


def dispatch(request):
    if not isinstance(request,dict):raise ValueError('invalid_request')
    operation=request.get('operation')
    if operation=='snapshot':return snapshot()
    if operation=='refresh_statistics':
        import vk_statistics_job
        return vk_statistics_job.start(request.get('group_id'))
    if operation=='add_account':
        row=accounts.add_account(request.get('name',''))
        return row
    if operation=='posts':
        import vk_statistics
        return {'posts':vk_statistics.posts(accounts.database_for_account(1),
                        group_id=request.get('group_id'),limit=request.get('limit',100))}
    account=request.get('account')
    if operation=='configure_account':
        values={k:v for k,v in request.items() if k not in ('operation','account')}
        result=accounts.configure_account(account,**values)
        return result
    if operation=='login_start':return start_login(account)
    if operation=='login_check':return check_login(account)
    if operation=='login_status':return login_status(account)
    if operation=='login_finish':return finish_login(account)
    raise ValueError('invalid_request')


def rpc(source=sys.stdin,destination=sys.stdout):
    try:
        raw=source.read(65537)
        if len(raw)>65536:raise ValueError('request_too_large')
        result=dispatch(json.loads(raw))
        code=0
    except (ValueError,KeyError,TypeError):result={'error':'invalid_request'};code=2
    except accounts.AccountError as exc:
        allowed={'account_must_be_disabled_for_login','account_operation_finishing','interactive_login_task_failed','account_authentication_required',
                 'community_destinations_required','account_schedule_capacity'}
        result={'error':str(exc) if str(exc) in allowed else 'account_guard'};code=2
    except Exception:result={'error':'control_unavailable'};code=2
    # ASCII wire format is stable through Windows OpenSSH/PowerShell code pages.
    json.dump(result,destination,ensure_ascii=True);destination.write('\n');return code


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rpc',action='store_true')
    parser.add_argument('--login-worker',type=int)
    parser.add_argument('--session')
    parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    if args.rpc:raise SystemExit(rpc())
    if args.login_worker and args.session:raise SystemExit(login_worker(args.login_worker,args.session,check_only=args.check_only))
    parser.error('Choose --rpc or --login-worker / --session')
