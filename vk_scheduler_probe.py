"""Real profile/UI inspection with engagement controls physically blocked.

This module opens selectors but never sends likes/reposts, reserves attempts,
or opens account databases for writing. It is safe to inspect outside the
scheduler's action window. A guard interception is a failed probe, not success.
"""
from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

import vk_accounts as accounts
import vk_account_smoke as smoke
import vk_batch as engine
import vk_community as community
import vk_groups as groups
import vk_unified as app


_BLOCKING_SCRIPT = r"""(() => {
  window.__vkProbeBlockedClicks=0;
  const forbidden='[data-testid="post_footer_action_like"],'+
    '[data-vk-batch-footer-action="like"],[data-testid="comment-like"],'+
    '[data-testid="share_tab_submit"],[data-vk-poc-publish]';
  const block=e=>{const node=e.target instanceof Element?e.target:e.target?.parentElement;
    if(node?.closest(forbidden)) {
      e.preventDefault();e.stopImmediatePropagation();window.__vkProbeBlockedClicks++;
    }};
  for(const type of ['pointerdown','pointerup','mousedown','mouseup','click','auxclick','touchstart','touchend'])
    window.addEventListener(type,block,{capture:true,passive:false});
  window.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')block(e)},true);
  window.addEventListener('submit',e=>{
    e.preventDefault();e.stopImmediatePropagation();window.__vkProbeBlockedClicks++;
  },true);
  for(const method of ['submit','requestSubmit'])
    HTMLFormElement.prototype[method]=function(){window.__vkProbeBlockedClicks++};
})();"""


def _readonly_unlocked(account):
    path=Path(engine.DB_PATH if account==1 else smoke.ACCOUNT2_DB if account==2 else accounts.database_for_account(account))
    if not path.is_file():
        raise engine.StopRun('История аккаунта отсутствует; сначала проверка профиля')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            engine.require_platform_unlocked(db)
    except sqlite3.DatabaseError as exc:
        raise engine.StopRun('Не удалось прочитать защитное состояние аккаунта без записи') from exc


def _final_info(page, selector):
    final=page.locator(selector)
    if final.count()!=1 or not final.is_visible():
        raise engine.StopRun('Нет единственного видимого финального элемента')
    if not final.is_enabled() or final.get_attribute('aria-disabled')=='true':
        raise engine.StopRun('Финальный элемент отключён; действия не проверялись')
    return dict(finalbutton_visible=True,finalbutton_enabled=True)


def _personal_chooser(page,gid,pid):
    # Opener and native destination selection only. No publish helper is called.
    engine.post_footer_button(page,gid,pid,'share').click(timeout=12000)
    check=engine.wait_share_destination(page,mark=True)
    if not check.get('ok'):
        raise engine.StopRun('Личный диалог репоста не распознан; отправки не было')
    state=check['state']
    if state['wall'] is True and state['direct_message'] is True:
        raise engine.StopRun('Противоречивое назначение личного диалога')
    if state['wall'] is not True:
        if check.get('method')=='element':
            page.locator('[data-vk-poc-own="true"]').click(timeout=12000)
        elif check.get('method')=='point':
            point=check['point'];page.mouse.click(point['x'],point['y'])
        else:
            raise engine.StopRun('Не подтверждён безопасный выбор личной стены')
        page.wait_for_timeout(350)
    state=page.evaluate(engine.SHARE_DIALOG_JS,{'action':'state'})
    if not state.get('ok') or state['state']['wall'] is not True or state['state']['direct_message'] is not False:
        raise engine.StopRun('VK не подтвердил единственное назначение личной стены')
    publish=page.evaluate(engine.PUBLISH_CONTROLS_JS)
    if not publish.get('ok') or len(publish.get('candidates',[]))!=1 or publish['candidates'][0].get('disabled') is not False:
        raise engine.StopRun('Нет единственной активной кнопки личной публикации')
    return _final_info(page,'[data-vk-poc-publish="0"]')


def probe(account,kind,row,*,config_path=None):
    """Inspect one exact API source and destination, without changing history.

    Returns only public source/destination IDs and generic boolean UI evidence.
    Already-liked posts are allowed: their like button is read without clicking.
    No profile identity value, community name, DOM or session data is returned.
    """
    if type(account) is not int or account not in accounts.account_numbers() or kind not in ('like','personal','community'):
        raise ValueError('Неверный аккаунт или вид проверки')
    values=[row.get(key) for key in ('group_id','post_id','published_at')]
    if any(type(value) is not int or value<=0 for value in values):
        raise ValueError('Нужны точные числовые ID и дата источника')
    gid,pid,stamp=values;destination=row.get('community_id')
    if kind=='community' and (type(destination) is not int or not 0<destination<=2**53-1):
        raise ValueError('Нужен положительный точный ID сообщества')
    cfg=groups.load_config(Path(config_path) if config_path is not None else groups.CONFIG_PATH)
    if not any(g['id']==gid and g.get('enabled',True) for g in cfg['groups']):
        raise engine.StopRun('Исходная группа не включена в конфигурацию')
    try:
        exact=smoke.catalog_post(gid,pid,catalog_db=Path(engine.DB_PATH),days=8)
    except smoke.SmokeError as exc:
        raise engine.StopRun('Нет действующего точного API-источника для проверки') from exc
    if exact!=stamp or stamp>int(datetime.now(timezone.utc).timestamp()):
        raise engine.StopRun('Дата выбранного источника не совпадает с VK API')
    identity=community.pinned_identity(account)
    profile=accounts.profile_for_account(account)
    if not profile.is_dir():raise engine.StopRun('Сохранённый профиль отсутствует')
    result=dict(account=account,kind=kind,source_confirmed=False,date_confirmed=False,
        account_confirmed=False,destination='source' if kind=='like' else kind,
        like_state=None,finalbutton_visible=False,finalbutton_enabled=False,blocked_clicks=0)
    if kind=='community':result['community_id']=destination
    with ExitStack() as stack:
        stack.enter_context(accounts.exclusive_activity(Path(engine.DB_PATH).resolve().parent))
        stack.enter_context(app.exclusive_run())
        if account!=1 or not accounts.uses_primary_profile_lock(account):stack.enter_context(accounts.exclusive_profile(account))
        _readonly_unlocked(account)
        try:
            with sync_playwright() as pw:
                context=pw.chromium.launch_persistent_context(user_data_dir=str(profile),headless=False,
                    locale='ru-RU',viewport={'width':1320,'height':900})
                page=None
                try:
                    context.add_init_script(_BLOCKING_SCRIPT)
                    page=context.new_page()
                    engine.watch_rate_limit_responses(page)
                    engine.configure_navigation_pacing(page,max(20,cfg['read_pause_seconds']))
                    def guard():
                        engine.ensure_ok(page);community.live_identity(context,identity)
                    engine.go_to_post(page,gid,pid);guard()
                    date=groups.exact_post_date_info(page,gid,pid).get('publishedAt')
                    if type(date) is not int or date!=stamp:
                        raise engine.StopRun('Точная дата исходного поста отличается от VK API')
                    footer=engine.wait_post_footer_info(page,gid,pid,'like',mark=(kind=='like'))
                    result.update(source_confirmed=True,date_confirmed=True,account_confirmed=True,like_state=footer['liked'])
                    if kind=='like':
                        result.update(_final_info(page,'[data-vk-batch-footer-action="like"]'))
                    elif kind=='personal':
                        result.update(_personal_chooser(page,gid,pid));guard()
                    else:
                        prepared=community.ui.prepare(page,gid,pid,destination,identity_guard=guard)
                        if prepared.get('community_id')!=destination:
                            raise engine.StopRun('Подготовлено другое сообщество')
                        result.update(_final_info(page,'[data-testid="share_tab_submit"]'));guard()
                    return result
                finally:
                    try:
                        if page is not None:
                            community.ui._PREPARED.pop(page,None)
                            try:
                                count=page.evaluate('()=>window.__vkProbeBlockedClicks')
                            except PlaywrightError as exc:
                                raise engine.StopRun('Не удалось подтвердить счётчик защитной проверки') from exc
                            if type(count) is not int or count<0:
                                raise engine.StopRun('Счётчик защитной проверки отсутствует')
                            result['blocked_clicks']=count
                            if count:
                                raise engine.StopRun('Проба пыталась нажать запрещённый элемент; действие заблокировано')
                    finally:
                        try:
                            if page is not None and not page.is_closed():page.close()
                        finally:context.close()
        except PlaywrightError as exc:
            raise engine.StopRun('Интерфейс VK не подтвердил read-only проверку') from exc
