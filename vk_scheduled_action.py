"""Execute one scheduler-selected action through the existing guarded VK UI.

The caller owns calendar quotas and durable job intent. Its before_attempt
callback must check the current window/quota and commit that intent; the
record_attempt callback attaches the account-local durable attempt ID. Both
callbacks finish before any outward click. This module never chooses another
post. A separately authorized recovery may confirm a prior attempt or retry a
strictly proven absent like; repost recovery never sends.
"""
from __future__ import annotations

import time
import sys
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

import vk_accounts as accounts
import vk_account_smoke as smoke
import vk_api_read as api
import vk_batch as engine
import vk_community as community
import vk_groups as groups
import vk_unified as app
import vk_action_availability as availability
import vk_diagnostics as diagnostics
import vk_action_timing as action_timing


class ScheduledActionStopped(engine.StopRun):
    STAGES = {'source', 'destination_precheck', 'community_prepare',
              'community_submit', 'community_confirm', 'personal_action',
              'source_navigation', 'source_guard', 'source_date_read',
              'source_date_unavailable', 'source_date_mismatch', 'source_date_conflict', 'source_reference', 'availability',
              'browser_open', 'cleanup'}

    def __init__(self, stage, message):
        if stage not in self.STAGES:
            raise ValueError('Unknown action stage')
        self.reason_code = 'action_stopped_' + stage
        self.stage=stage
        super().__init__(message)


def prime_action_identity(context,*,deadline=None,before_request=None):
    """Resolve an own-header alias before intent, under the caller's API lease."""
    if accounts.inspect_context(context,attempts=4) is None:
        accounts.prepare_context_identity(context,primary_lock_held=True,
            deadline=deadline,before_request=before_request)


def source_ready(page,gid,pid,stamp,guard,*,timings=None):
    """Navigate once; allow exact metadata to hydrate without retrying a send.

    A visible root is not proof its date has arrived. Missing metadata may be
    polled for up to nine seconds; conflicting/mismatched dates fail at once.
    Every read rechecks identity/platform protection. No fallback date is used.
    """
    if timings is None:timings=getattr(guard,'__dict__',{}).get('stage_timings')
    try:
        with action_timing.measure(timings,'source_navigation'):
            engine.go_to_post(page,gid,pid)
    except engine.PlatformRateLimited:raise
    except (engine.StopRun,PlaywrightError) as exc:
        raise ScheduledActionStopped('source_navigation',str(exc)) from exc
    deadline=time.monotonic()+9
    while True:
        try:guard()
        except engine.PlatformRateLimited:raise
        except (engine.StopRun,PlaywrightError) as exc:
            raise ScheduledActionStopped('source_guard',str(exc)) from exc
        try:info=groups.exact_post_date_info(page,gid,pid)
        except PlaywrightError as exc:
            raise ScheduledActionStopped('source_date_read',str(exc)) from exc
        actual=info.get('publishedAt')
        if info.get('source')=='date-conflict':
            raise ScheduledActionStopped('source_date_conflict','Противоречивые точные даты исходного поста')
        if type(actual) is int:
            if actual!=stamp:
                raise ScheduledActionStopped('source_date_mismatch','Дата точного исходного поста отличается от VK API')
            return
        if time.monotonic()>=deadline:
            raise ScheduledActionStopped('source_date_unavailable','Точная дата исходного поста не появилась за9секунд')
        page.wait_for_timeout(250)


class _SingleBudget:
    """A single durable reservation, recorded in existing report ledgers."""
    def __init__(self, db, kind, gid, pid, community_id, run_id, guard,
                 before_attempt, record_attempt, before_click=None,diagnostic=None,
                 recovery_attempt_id=None,recovery_guard=None,stage_timings=None):
        self.db,self.kind,self.gid,self.pid=db,kind,gid,pid
        self.community_id,self.run_id=community_id,run_id
        self.guard,self.before_attempt,self.record_attempt=guard,before_attempt,record_attempt
        self.confirmation_guard=guard
        self.require_vk_denial_evidence=True
        self.action='like' if kind=='like' else 'repost'
        self.attempt_id=None
        self.diagnostic=diagnostic
        self.recovery_attempt_id=recovery_attempt_id
        self.recovery_guard=recovery_guard
        self.stage_timings=stage_timings
        if before_click is None:self.final_click_timeout=None
        else:
            def timeout():
                diagnostics.emit(self.diagnostic,'final_click_check',stage='final_click',attempt_id=self.attempt_id)
                return before_click()  # Last operation: logging must not stale the final deadline.
            self.final_click_timeout=timeout

    def available_actions(self, actions):
        engine.require_platform_unlocked(self.db)
        return {self.action} & set(actions) if self.attempt_id is None else set()

    def reserve(self, post_id, action):
        diagnostics.emit(self.diagnostic,'action_stage',stage='attempt_reservation')
        if self.attempt_id is not None or post_id!=self.pid or action!=self.action:
            raise engine.StopRun('Планировщик допускает только одну выбранную попытку')
        self.guard()
        engine.require_platform_unlocked(self.db)
        if self.recovery_attempt_id is not None:
            if self.kind!='like' or not callable(self.recovery_guard):
                raise engine.StopRun('Возобновление отправки репоста запрещено')
            self.recovery_guard()
        if self.kind=='community':
            community.require_reference_available(self.db,self.community_id,self.gid,self.pid)
        # Parent persists its uncertain job intent before the account ledger.
        self.before_attempt()
        with self.db:
            if self.recovery_attempt_id is not None:
                _validate_recovery_attempt(self.db,self.kind,self.gid,self.pid,self.community_id,self.recovery_attempt_id)
            if self.kind=='community':
                self.db.execute("INSERT OR IGNORE INTO community_reposts VALUES(?,?,?,'pending',NULL,?)",
                    (self.community_id,self.gid,self.pid,engine.moment()))
                saved=self.db.execute("""UPDATE community_reposts SET status='uncertain',updated_at=?
                    WHERE community_id=? AND group_id=? AND post_id=? AND status='pending'""",
                    (engine.moment(),self.community_id,self.gid,self.pid))
                if saved.rowcount!=1:
                    raise engine.StopRun('Репост в сообщество уже выполнен или uncertain')
                link_table='community_run_attempts'
            else:
                expected='uncertain' if self.recovery_attempt_id is not None else 'pending'
                saved=self.db.execute(f"""UPDATE posts SET {action}_status='uncertain',detail=?,updated_at=?
                    WHERE group_id=? AND post_id=? AND {action}_status=?""",
                    ('Планировщик сохраняет попытку перед кликом',engine.moment(),self.gid,self.pid,expected))
                if saved.rowcount!=1:
                    raise engine.StopRun('Личное действие уже выполнено или не pending')
                link_table='command_action_attempts'
            cursor=self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                (self.gid,self.pid,action,engine.moment()))
            attempt_id=cursor.lastrowid
            self.db.execute(f'INSERT INTO {link_table}(run_id,attempt_id) VALUES(?,?)',(self.run_id,attempt_id))
        self.attempt_id=attempt_id
        # If this callback fails, the local state is still uncertain and the
        # cooldown still runs. No browser click has happened at this point.
        self.record_attempt(attempt_id)
        diagnostics.emit(self.diagnostic,'attempt_reserved',attempt_id=attempt_id,stage='attempt_reservation')


def _warning(exc):
    return any(word in str(exc).lower() for word in
        ('captcha','капча','ограничени','робот','подозрительная активность','vk запросил вход'))


def _validate_recovery_attempt(db,kind,gid,pid,destination,attempt_id):
    """Bind recovery to this actor's latest immutable attempt and original run."""
    if type(attempt_id) is not int or attempt_id<=0:
        raise ValueError('Нужен точный ID предыдущей попытки')
    action='like' if kind=='like' else 'repost'
    prior=db.execute('SELECT * FROM action_attempts WHERE id=?',(attempt_id,)).fetchone()
    if prior is None or (prior['group_id'],prior['post_id'],prior['action'])!=(gid,pid,action):
        raise engine.StopRun('Предыдущая попытка принадлежит другому источнику или действию')
    if db.execute('SELECT 1 FROM action_attempts WHERE group_id=? AND post_id=? AND action=? AND id>? LIMIT 1',
                  (gid,pid,action,attempt_id)).fetchone():
        raise engine.StopRun('После предыдущей попытки есть другая попытка; повтор запрещён')
    if kind=='community':
        link=db.execute('''SELECT r.community_id FROM community_run_attempts a
          JOIN community_runs r ON r.id=a.run_id JOIN community_run_posts p ON p.run_id=r.id
          WHERE a.attempt_id=? AND p.group_id=? AND p.post_id=?''',(attempt_id,gid,pid)).fetchall()
        valid=len(link)==1 and link[0]['community_id']==destination
        saved=community.get_state(db,destination,gid,pid)['status']
        # A missing historic canonical reference must never be reconstructed.
        if not db.execute('SELECT 1 FROM community_source_refs WHERE group_id=? AND post_id=?',(gid,pid)).fetchone():
            raise engine.StopRun('У предыдущей попытки нет сохранённого точного оригинала')
    else:
        field='requested_like' if kind=='like' else 'requested_repost'
        link=db.execute(f'''SELECT r.{field} FROM command_action_attempts a
          JOIN command_action_runs r ON r.id=a.run_id JOIN command_action_posts p ON p.run_id=r.id
          WHERE a.attempt_id=? AND p.group_id=? AND p.post_id=?''',(attempt_id,gid,pid)).fetchall()
        valid=len(link)==1 and link[0][field]>0
        saved=db.execute(f'SELECT {action}_status FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()
        saved=saved[0] if saved else None
    if not valid or saved!='uncertain':
        raise engine.StopRun('Предыдущая попытка не связана с точным uncertain действием и назначением')
    try:
        at=datetime.fromisoformat(prior['attempted_at'])
        if at.tzinfo is None:raise ValueError('Missing timezone')
        stamp=int(at.timestamp())
        if stamp<=0 or stamp>int(datetime.now(timezone.utc).timestamp()):raise ValueError('Invalid attempt time')
    except (TypeError,ValueError,OverflowError) as exc:
        raise engine.StopRun('Время предыдущей попытки не подтверждено') from exc
    return stamp


def _recovery_personal_state(page,gid,pid,kind,guard):
    """Current exact actor-page JSON plus the main-post like footer."""
    guard()
    state=engine.refresh_state(page,gid,pid,attempts=1)
    guard()
    if state.get('source')=='conflict':
        raise engine.StopRun('Противоречивое состояние точного исходного поста')
    if state.get('source') not in ('live','inline'):return 'uncertain'
    if kind=='personal':return 'verified' if state.get('reposted') is True else 'uncertain'
    footer=engine.post_footer_info(page,gid,pid,'like');guard()
    liked=state.get('liked');main=footer.get('liked') if footer.get('ok') is True else None
    if type(liked) is bool and type(main) is bool and liked!=main:
        raise engine.ActionGuardError('pre_action_like_state_conflict',{'liked':liked,'main_post_liked':main})
    if liked is True and main is True:return 'verified'
    if liked is False and main is False and state.get('canLike') is True:return 'retry'
    return 'uncertain'


def execute_action(account,kind,row,*,before_attempt,record_attempt,config_path=None,before_click=None,diagnostic=None,
                   recovery_attempt_id=None,allow_retry_click=True,
                   identity_read_deadline=None,before_identity_read=None):
    """Return status/attempt_id for exactly one like, personal or club repost.

    `row` needs exact API-certified group_id/post_id/published_at, and a positive
    community_id for kind='community'. Old reserve candidates may cover the
    seven preceding local calendar days, so the defensive API age bound is8
    rolling days. The scheduler applies the stricter calendar classification.
    before_click, when provided, rechecks the live deadline after all slow UI
    preparation and returns a1..9000ms timeout for the outward click itself.
    recovery_attempt_id must identify the latest exact uncertain actor attempt.
    Recovery returns attempt_id=None unless one new like is durably reserved;
    the caller keeps the old attempt in its append-only recovery history.
    allow_retry_click=False makes recovery read-only, including negative likes.
    No binding, cookie, credential, or authenticated DOM is returned.
    """
    if type(account) is not int or account not in accounts.account_numbers() or kind not in ('like','personal','community'):
        raise ValueError('Неверный аккаунт или вид одиночного действия')
    if not callable(before_attempt) or not callable(record_attempt):
        raise ValueError('Нужны callbacks проверки и записи попытки')
    if before_click is not None and not callable(before_click):
        raise ValueError('before_click должен возвращать допустимый timeout финального клика')
    if before_identity_read is not None and not callable(before_identity_read):
        raise ValueError('before_identity_read должен быть callback без reservation')
    if identity_read_deadline is not None and (not isinstance(identity_read_deadline,datetime)
            or identity_read_deadline.tzinfo is None or identity_read_deadline.utcoffset() is None):
        raise ValueError('Identity read deadline должен содержать timezone')
    if type(allow_retry_click) is not bool:raise ValueError('allow_retry_click должен быть bool')
    if recovery_attempt_id is not None and (type(recovery_attempt_id) is not int or recovery_attempt_id<=0):
        raise ValueError('Нужен точный ID предыдущей попытки')
    values=[row.get(key) for key in ('group_id','post_id','published_at')]
    if any(type(value) is not int or value<=0 for value in values):
        raise ValueError('Нужны точные числовые ID и timestamp VK API')
    gid,pid,stamp=values
    destination=row.get('community_id') if kind=='community' else None
    if kind=='community' and (type(destination) is not int or not 0<destination<=2**53-1):
        raise ValueError('Нужен точный положительный ID сообщества')
    cfg=groups.load_config(Path(config_path) if config_path is not None else groups.CONFIG_PATH)
    action='like' if kind=='like' else 'repost'
    source=next((g for g in cfg['groups'] if g['id']==gid and g.get('enabled',True)),None)
    if source is None or not cfg['actions'][action] or not source.get(action+'_enabled',True):
        raise engine.StopRun('Источник или выбранное действие отключены в конфигурации')
    try:
        exact=smoke.catalog_post(gid,pid,catalog_db=Path(engine.DB_PATH),days=8)
    except smoke.SmokeError as exc:
        raise engine.StopRun('Нет действующего точного источника VK API для выбранного поста') from exc
    if exact!=stamp or stamp>int(datetime.now(timezone.utc).timestamp()):
        raise engine.StopRun('Дата выбранного источника не совпадает с текущим VK API')
    identity=community.pinned_identity(account)
    profile=accounts.profile_for_account(account)
    if not profile.is_dir():
        raise engine.StopRun('Сохранённый профиль отсутствует; сначала --check-account')
    db_path=engine.DB_PATH if account==1 else smoke.ACCOUNT2_DB if account==2 else accounts.database_for_account(account)
    result={'status':'pending','attempt_id':None}
    timings=action_timing.StageTimings()
    with ExitStack() as stack:
        stack.enter_context(app.exclusive_run())
        if account!=1 or not accounts.uses_primary_profile_lock(account):stack.enter_context(accounts.exclusive_profile(account))
        db=stack.enter_context(closing(engine.db_open(db_path)))
        if account!=1:smoke.init_account2(db,identity)
        community.init_state(db,identity);app.init_command_actions(db)
        engine.require_platform_unlocked(db)
        engine.add_posts(db,gid,[pid])
        groups.init_dates(db);groups.save_date(db,gid,pid,stamp,api.API_SOURCE,True)
        saved=(community.get_state(db,destination,gid,pid)['status'] if kind=='community' else
               db.execute(f'SELECT {action}_status FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()[0])
        recovery_stamp=None
        if recovery_attempt_id is not None:
            recovery_stamp=_validate_recovery_attempt(db,kind,gid,pid,destination,recovery_attempt_id)
        if saved in ('verified','already'):
            result['status']='already'
            if kind=='community':result['target_post_id']=community.get_state(db,destination,gid,pid)['target_post_id']
            return result
        if saved!='pending' and recovery_attempt_id is None:
            raise engine.StopRun('Выбранное действие не pending; автоматический повтор запрещён')
        if kind=='community':
            run_id=community.begin_run(db,destination,0,1,1)
            db.execute('INSERT INTO community_run_posts VALUES(?,?,?)',(run_id,gid,pid))
            run_table='community_runs'
        else:
            run_id=app.begin_command_action_run(db,int(kind=='like'),int(kind=='personal'),1)
            db.execute('INSERT INTO command_action_posts VALUES(?,?,?)',(run_id,gid,pid))
            run_table='command_action_runs'
        db.commit()
        budget=None;cooled=False;stage='source';started=time.perf_counter()
        def report(event,**data):
            try:
                data.setdefault('stage',stage)
                data.setdefault('elapsed_ms',max(0,int((time.perf_counter()-started)*1000)))
                if budget is not None:data.setdefault('attempt_id',budget.attempt_id)
                diagnostics.emit(diagnostic,event,**data)
            except Exception:pass
        def cooldown():
            nonlocal cooled
            if budget is not None and budget.attempt_id is not None and not cooled:
                print('[ACTION PAUSE] sleep 20 секунд после попытки',flush=True)
                report('cooldown_started',stage='cooldown')
                with timings.measure('cooldown'):
                    time.sleep(20)
                cooled=True
                report('cooldown_finished',stage='cooldown')
        try:
            stage='browser_open'
            report('action_stage')
            with sync_playwright() as pw:
                with timings.measure('browser_open'):
                    context=pw.chromium.launch_persistent_context(user_data_dir=str(profile),headless=False,
                        locale='ru-RU',viewport={'width':1320,'height':900})
                page=None
                try:
                    page=context.new_page()
                    engine.watch_rate_limit_responses(page)
                    diagnostics.watch_network(page,report)
                    engine.configure_navigation_pacing(page,max(20,cfg['read_pause_seconds']),db=db)
                    identity_prepared=False
                    def guard():
                        nonlocal identity_prepared
                        try:
                            with timings.measure('source_guard'):
                                engine.ensure_ok(page)
                                if not identity_prepared:
                                    try:
                                        prime_action_identity(context,deadline=identity_read_deadline,
                                            before_request=before_identity_read)
                                    except accounts.IdentityPreparationDeadline:raise
                                    except (accounts.AccountError,api.VkApiFailure):
                                        raise ScheduledActionStopped('source_guard','Не удалось подтвердить точный аккаунт') from None
                                    identity_prepared=True
                                community.live_identity(context,identity)
                        except PlaywrightError as exc:
                            # Identity/protection probes are guards, even when
                            # called from inside process_one's confirmation path.
                            raise ScheduledActionStopped('source_guard','Не удалось проверить точный аккаунт и защиты VK') from exc
                    guard.stage_timings=timings
                    def check_source():
                        nonlocal stage
                        stage='source_navigation'
                        report('action_stage')
                        engine.require_platform_unlocked(db)
                        source_ready(page,gid,pid,stamp,guard)
                        stage='source_date_read'
                        report('action_stage')
                    budget=_SingleBudget(db,kind,gid,pid,destination,run_id,guard,before_attempt,record_attempt,before_click,report,
                                         stage_timings=timings)
                    check_source()
                    recovering=recovery_attempt_id is not None
                    if recovering and kind=='community':
                        stage='source_reference'
                        reference=community.ui.repost_reference(page,gid,pid)
                        if reference['published_at']!=stamp:
                            raise engine.StopRun('Дата цепочки исходного репоста отличается от VK API')
                        # remember_reference fails on any owner/sign or original change.
                        community.remember_reference(db,gid,pid,reference['group_id'],reference['post_id'])
                        stage='community_confirm'
                        try:
                            with timings.measure('confirmation'):
                                found=community.ui.confirm_destination(page,reference['group_id'],reference['post_id'],destination,
                                                                       not_before=recovery_stamp)
                        except community.ui.CommunityConfirmationError as exc:
                            guard()
                            if exc.reason_code!='community_confirmation_timeout':raise
                            found=None
                        guard()
                        result['status']='uncertain'
                        if found is not None and type(found.get('published_at')) is int and found['published_at']>=recovery_stamp:
                            community.write_state(db,destination,gid,pid,'verified',target_post_id=found['post_id'])
                            result.update(status='verified',target_post_id=found['post_id'])
                    elif recovering:
                        stage='personal_action'
                        with timings.measure('confirmation'):
                            observed=_recovery_personal_state(page,gid,pid,kind,guard)
                        if observed=='verified':
                            engine.status_write(db,gid,pid,action,'verified','Свежая точная проверка предыдущей попытки')
                            result['status']='verified'
                        elif observed=='retry' and allow_retry_click:
                            def recovery_guard():
                                if _recovery_personal_state(page,gid,pid,kind,guard)!='retry':
                                    raise engine.StopRun('Отрицательное состояние лайка изменилось перед повтором')
                            budget=_SingleBudget(db,kind,gid,pid,destination,run_id,guard,before_attempt,record_attempt,before_click,report,
                                                 recovery_attempt_id=recovery_attempt_id,recovery_guard=recovery_guard,
                                                 stage_timings=timings)
                            post=dict(db.execute('SELECT * FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone())
                            post['like_status']='pending'  # Virtual only; durable state stays uncertain.
                            engine.process_one(page,db,gid,post,0,safety=budget,actions={action},already_open=True)
                            result['status']=db.execute('SELECT like_status FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()[0]
                            if result['status'] not in ('verified','already'):
                                raise engine.StopRun('Повтор лайка не подтверждён')
                        else:result['status']='uncertain'
                    elif kind=='community':
                        stage='source_reference'
                        report('action_stage')
                        reference=community.ui.repost_reference(page,gid,pid)
                        if reference['published_at']!=stamp:
                            raise engine.StopRun('Дата цепочки исходного репоста отличается от VK API')
                        cg,cp=reference['group_id'],reference['post_id']
                        community.remember_reference(db,gid,pid,cg,cp)
                        previous=community.reference_state(db,destination,gid,pid)
                        if previous['status']=='uncertain':
                            raise engine.StopRun('Оригинал имеет uncertain в этом сообществе; повтор запрещён')
                        stage='destination_precheck'
                        report('action_stage')
                        if previous['status']=='pending':
                            community.require_reference_available(db,destination,gid,pid)
                        known=([{'post_id':previous['target_post_id']}] if previous['status'] in ('verified','already')
                               else community.ui.scan_destination(page,cg,cp,destination));guard()
                        if known:
                            target=known[0]['post_id'];community.write_state(db,destination,gid,pid,'already',target_post_id=target)
                            result.update(status='already',target_post_id=target)
                        else:
                            stage='source'
                            check_source()
                            stage='source_reference'
                            if community.ui.repost_reference(page,gid,pid)!=reference:
                                raise engine.StopRun('Исходная цепочка репоста изменилась перед отправкой')
                            stage='availability'
                            report('action_stage')
                            denial=availability.confirmed_denial(page,gid,pid,stamp,kind,guard)
                            if denial:
                                result.update(status='skipped_vk',skip_evidence=denial)
                                return result
                            stage='community_prepare'
                            report('action_stage')
                            with timings.measure('community_prepare'):
                                proof=community.ui.prepare(page,gid,pid,destination,identity_guard=guard)
                            booked=[]
                            def reserve():
                                budget.reserve(pid,'repost')
                                booked.append(int(datetime.now(timezone.utc).timestamp()))
                            reserve.safety=budget
                            try:
                                stage='community_submit'
                                report('action_stage')
                                click_options={'before_final_click':budget.final_click_timeout} if before_click is not None else {}
                                community.ui.submit(page,proof,before_publish=reserve,identity_guard=guard,**click_options)
                            finally:cooldown()
                            if len(booked)!=1:
                                raise engine.StopRun('Финальная отправка не имеет единственной сохранённой попытки')
                            stage='community_confirm'
                            report('action_stage')
                            with timings.measure('confirmation'):
                                found=community.ui.confirm_destination(page,cg,cp,destination,not_before=booked[0])
                            guard()
                            target=found['post_id'];community.write_state(db,destination,gid,pid,'verified',target_post_id=target)
                            result.update(status='verified',target_post_id=target)
                    else:
                        stage='availability'
                        report('action_stage')
                        denial=availability.confirmed_denial(page,gid,pid,stamp,kind,guard)
                        if denial:
                            result.update(status='skipped_vk',skip_evidence=denial)
                            return result
                        stage='personal_action'
                        report('action_stage')
                        post=db.execute('SELECT * FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()
                        engine.process_one(page,db,gid,post,0,safety=budget,actions={action},already_open=True)
                        saved=db.execute(f'SELECT {action}_status FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()[0]
                        if saved not in ('verified','already','unavailable'):
                            raise engine.StopRun('Одиночное действие не подтверждено; повтор запрещён')
                        result['status']=saved
                    result['attempt_id']=budget.attempt_id
                finally:
                    action_error=sys.exc_info()[0] is not None
                    if sys.exc_info()[0] is None:stage='cleanup'
                    report('action_stage',stage='cleanup')
                    try:
                        try:
                            cooldown()
                        finally:
                            page_error=None
                            try:
                                if page is not None and not page.is_closed():page.close()
                            except PlaywrightError as exc:page_error=exc
                            finally:context.close()
                            if page_error is not None:
                                if not action_error and cooled and result['status']=='verified':
                                    engine.require_platform_unlocked(db)
                                    report('cleanup_warning',stage='cleanup',outcome='verified',error=page_error)
                                else:raise page_error
                    except Exception:
                        stage='cleanup'
                        raise
        except engine.PlatformRateLimited as exc:
            exc.stage=stage
            engine.lock_on_platform_warning(db,0,str(exc));raise
        except engine.StopRun as exc:
            if _warning(exc):engine.lock_on_platform_warning(db,0,str(exc))
            if isinstance(exc,(community.ui.CommunityConfirmationError,ScheduledActionStopped,engine.ActionConfirmationError,engine.ActionTransportError,engine.ActionGuardError)):
                if not hasattr(exc,'stage'):exc.stage=stage
                raise
            raise ScheduledActionStopped(stage,str(exc)) from exc
        except PlaywrightError as exc:
            raise ScheduledActionStopped(stage,'Интерфейс VK не подтвердил одиночное действие; автоматического повтора нет') from exc
        except Exception as exc:
            try:exc.stage=stage
            except Exception:pass
            raise
        finally:
            try:
                outcome='complete' if result['status']=='verified' else 'partial'
                db.execute(f'UPDATE {run_table} SET finished_at=?,result=? WHERE id=?',(engine.moment(),outcome,run_id));db.commit()
            finally:
                timings.flush(diagnostic)
    return result
