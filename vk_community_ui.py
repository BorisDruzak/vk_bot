"""Guarded community share UI and bounded, read-only destination evidence.

Preparation never publishes. A final attempt needs a page-bound prepared proof,
fresh identity/destination checks, and the caller's durable reservation callback.
No authenticated DOM or session material is returned or logged.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from playwright.sync_api import Error as PlaywrightError

import vk_batch as engine
import vk_groups as multi
import vk_action_timing as action_timing


_PREPARED = WeakKeyDictionary()
_FINAL = '[data-testid="share_tab_submit"]'
_CLUB = 'label[data-testid="club_share_option"]'
_SELECT = '[data-testid="share_club_select"]'
_RADIO_VALUES = ('club', 'message', 'wall', 'story')


class CommunityConfirmationError(engine.StopRun):
    """A fixed diagnostic code, never authenticated DOM or exception text."""
    def __init__(self, ambiguous=False):
        self.reason_code = ('community_confirmation_ambiguous' if ambiguous else
                            'community_confirmation_timeout')
        super().__init__(self.reason_code)


class CommunityPreparationTimeout(TimeoutError):
    """Initial option did not mount before any destination/send reservation."""
    reason_code='community_prepare_timeout'
    stage='community_prepare'

    def __init__(self):
        super().__init__(self.reason_code)


def _ids(*values):
    if any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in values):
        raise engine.StopRun('Требуются точные положительные числовые ID источника и сообщества')


def _canonical_ids(wall_key, *positive_ids):
    """Canonical wall key is -VK owner_id: group positive, personal negative.

    Preserve the sign in history and destination evidence; abs would conflate
    a personal wall and a community with the same numeric ID. Selected source
    communities and destination IDs still use the stricter _ids validator.
    """
    if type(wall_key) is not int or not 0 < abs(wall_key) <= 2**53 - 1:
        raise engine.StopRun('Нет точного ненулевого владельца оригинала VK')
    _ids(*positive_ids)


def _unique(locator, name, *, visible=True):
    if locator.count() != 1 or (visible and not locator.is_visible()):
        raise engine.StopRun('Нет единственного доступного элемента: ' + name)
    return locator


def _wait(page, predicate, description, *, timeout_ms=9000,timeout_error=None):
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        engine.ensure_ok(page)
        if predicate():
            return
        if time.monotonic() >= deadline:
            if timeout_error is not None:raise timeout_error()
            raise engine.StopRun('Не подтверждено состояние VK: ' + description)
        page.wait_for_timeout(100)


_STRUCTURAL_SOURCE_JS = r"""({groupId,postId}) => {
  const key='-'+groupId+'_'+postId;
  // VK repeats data-post-id on post_text descendants; those are not roots.
  const roots=[...document.querySelectorAll('[data-testid="post"][data-post-id]')]
    .filter(root=>root.getAttribute('data-post-id') === key);
  const byId=document.getElementById('post'+key);
  if(byId && !roots.includes(byId)) roots.push(byId);
  if(roots.length !== 1) return false;
  const root=roots[0], nesting=root.getAttribute('data-post-nesting-lvl');
  if(root.hasAttribute('data-post-id') && root.getAttribute('data-post-id') !== key) return false;
  if(nesting && nesting !== '0') return false;
  if(root.closest('[data-testid="wall_comments_comment_root"]') ||
     root.parentElement?.closest('[data-testid="post"][data-post-id]')) return false;
  const single=document.querySelector('#page-single-post');
  const primary=single?.querySelector(':scope > div > article') ||
                single?.querySelector(':scope > article');
  return single ? !!primary && primary.contains(root) : !!root.closest('article');
}"""


def _source(page, group_id, post_id, *, actionable=True):
    engine.ensure_ok(page)
    url = urlsplit(page.url)
    if (url.scheme != 'https' or url.hostname not in engine.ALLOWED_HOSTS or
            url.path != f'/wall-{group_id}_{post_id}'):
        raise engine.StopRun('Открыт не точный исходный пост')
    if actionable:
        info = engine.post_footer_info(page, group_id, post_id, 'share')
        if not info.get('ok'):
            raise engine.StopRun('Не подтверждён основной исходный пост и его кнопка репоста')
    elif not page.evaluate(_STRUCTURAL_SOURCE_JS, dict(groupId=group_id, postId=post_id)):
        raise engine.StopRun('Не подтверждён точный основной исходный пост за диалогом')


def _dialog(page):
    """Smallest common share container; never use the whole document as dialog."""
    _unique(page.locator(_CLUB), 'выбор сообщества')
    final = _unique(page.locator(_FINAL), 'финальная кнопка')
    container = final.evaluate_handle("""button => {
      for(let el=button.parentElement;el && el !== document.body;el=el.parentElement) {
        if(el.querySelector('label[data-testid="club_share_option"]') &&
           el.querySelector('[data-testid="share_club_select"]') &&
           el.querySelectorAll('input[name="shareOption"]').length >= 4) return el;
      }
      return null;
    }""").as_element()
    if container is None:
        raise engine.StopRun('Не удалось изолировать диалог публикации в сообщество')
    return container, final


def _check_destination(page, proof, dialog, final_handle):
    # An open modal may make the background inert. Its identity must still be
    # exact, but its footer must not be required to be actionable under a modal.
    _source(page, proof['source_group_id'], proof['source_post_id'], actionable=False)
    current_dialog, final = _dialog(page)
    if not dialog.evaluate('(el,other)=>el.isConnected && el === other', current_dialog):
        raise engine.StopRun('Диалог репоста изменился после подготовки')
    if not final_handle.evaluate('(el,other)=>el.isConnected && el === other', final.element_handle()):
        raise engine.StopRun('Финальная кнопка изменилась после подготовки')
    if not final.is_enabled() or final.get_attribute('aria-disabled') == 'true':
        raise engine.StopRun('Финальная кнопка публикации отключена')
    # Exactly these native radio controls were observed in the live probe.
    # Missing, duplicated or new destinations fail closed.
    radios = page.locator('input[name="shareOption"]')
    if radios.count() != len(_RADIO_VALUES):
        raise engine.StopRun('Набор назначений диалога изменился')
    for value in _RADIO_VALUES:
        radio = _unique(page.locator(f'input[name="shareOption"][value="{value}"]'),
                        'назначение ' + value, visible=False)
        if not dialog.evaluate('(el,child)=>el.contains(child)', radio.element_handle()):
            raise engine.StopRun('Назначения не принадлежат одному диалогу')
        if radio.is_checked() is not (value == 'club'):
            raise engine.StopRun('Не подтверждено единственное назначение «В сообществе»')
    select = _unique(page.locator(_SELECT), 'поле сообщества')
    field = _unique(select.locator('input[role="combobox"]'), 'название сообщества')
    if not dialog.evaluate('(el,child)=>el.contains(child)', select.element_handle()):
        raise engine.StopRun('Поле сообщества не принадлежит диалогу')
    if field.input_value() != proof['community_label']:
        raise engine.StopRun('Выбранное сообщество изменилось после подготовки')
    return final


def prepare(page, source_group_id: int, source_post_id: int, community_id: int,
            *, identity_guard=None) -> dict:
    """Open the exact source share chooser and select one explicit community."""
    _PREPARED.pop(page, None)
    _ids(source_group_id, source_post_id, community_id)
    try:
        if identity_guard is not None:
            identity_guard()
        _source(page, source_group_id, source_post_id)
        if page.locator(_FINAL + ':visible').count():
            raise engine.StopRun('Диалог репоста уже открыт; источник окна не подтверждён')
        engine.post_footer_button(page, source_group_id, source_post_id, 'share').click(timeout=12000)
        def chooser_ready():
            count=page.locator(_CLUB).count()
            if count>1:raise engine.StopRun('Неоднозначный выбор сообщества')
            return count==1 and page.locator(_CLUB).is_visible()
        try:
            _wait(page, chooser_ready, 'выбор сообщества',timeout_ms=30000,
                  timeout_error=CommunityPreparationTimeout)
        except CommunityPreparationTimeout:
            # A missing control is retryable only while the exact source and
            # account still hold. Any guard raised here retains its own stop.
            if identity_guard is not None:identity_guard()
            _source(page,source_group_id,source_post_id,actionable=False)
            raise
        _unique(page.locator(_CLUB), 'выбор сообщества').click(timeout=9000)
        _wait(page, lambda: page.locator(_SELECT).count() == 1 and
              page.locator(_SELECT).is_visible(), 'список сообществ')
        select = _unique(page.locator(_SELECT), 'список сообществ')
        _wait(page, lambda: select.evaluate("""el =>
          !el.closest('[aria-busy="true"]') && !el.querySelector('[aria-busy="true"]') &&
          ![...document.querySelectorAll('span,div')].some(node =>
            node.children.length === 0 && node.getBoundingClientRect().width > 0 &&
            getComputedStyle(node).visibility !== 'hidden' &&
            /^Список опций загружается/.test((node.innerText || '').trim()))"""),
              'завершение загрузки списка сообществ')
        field = _unique(select.locator('input[role="combobox"]'), 'поле списка сообществ')
        dropdown = select.locator('svg.vkuiIcon--dropdown_20')

        def options_open():
            # Clicking the club radio can itself open the options. In that
            # state VK replaces dropdown_20 with chevron_up_20, so a second
            # opener click is both unnecessary and liable to close the list.
            return (field.get_attribute('aria-expanded') == 'true' or
                    page.locator('[data-testid^="share_club_option_"]:visible').count() > 0)

        def chooser_ready():
            if options_open():
                return True
            if dropdown.count() > 1:
                raise engine.StopRun('Неоднозначное раскрытие списка сообществ')
            return dropdown.count() == 1 and dropdown.is_visible()

        # Field mounting/loading completion does not imply its icon has mounted.
        _wait(page, chooser_ready, 'готовность раскрытого списка или его точной кнопки')
        if not options_open():
            _unique(dropdown, 'раскрытие списка').click(timeout=9000)
        option = page.locator(f'[data-testid="share_club_option_{community_id}"]')
        _wait(page, lambda: option.count() > 1 or
              (option.count() == 1 and option.is_visible()),
              'запрошенное сообщество', timeout_ms=3000)
        _unique(option, 'запрошенное сообщество')
        label = (option.get_attribute('label') or '').strip()
        if option.get_attribute('value') != str(community_id) or not label:
            raise engine.StopRun('ID или название опции сообщества не подтверждены')
        # VK exposes the selected destination through a text field, so two
        # loaded communities with the same name cannot be distinguished there.
        same_labels = page.locator('[data-testid^="share_club_option_"]').evaluate_all(
            "(options,label)=>options.filter(el=>(el.getAttribute('label') || '').trim() === label).length",
            label)
        if same_labels != 1:
            raise engine.StopRun('Название сообщества неоднозначно в списке назначений')
        option.click(timeout=9000)
        field = _unique(select.locator('input[role="combobox"]'), 'выбранное сообщество')
        _wait(page, lambda: field.input_value() == label, 'выбранное название сообщества')
        proof = dict(source_group_id=source_group_id, source_post_id=source_post_id,
                     community_id=community_id, community_label=label)
        dialog, final = _dialog(page)
        final_handle = final.element_handle()
        _check_destination(page, proof, dialog, final_handle)
        _PREPARED[page] = (dict(proof), dialog, final_handle)
        return proof
    except PlaywrightError as exc:
        raise engine.StopRun('Диалог сообщества не подтвердился; публикации не было') from exc


def submit(page, prepared: dict, *, before_publish, identity_guard=None, before_final_click=None, timings=None) -> None:
    """Recheck proof, persist the attempt, then click exactly once. No retry."""
    saved = _PREPARED.pop(page, None)
    if not isinstance(prepared, dict) or saved is None or prepared != saved[0]:
        raise engine.StopRun('Нет действующей подготовки этого диалога и сообщества')
    if not callable(before_publish):
        raise engine.StopRun('Нет обязательного сохранения попытки перед публикацией')
    if timings is None:
        safety=getattr(before_publish,'__dict__',{}).get('safety')
        timings=vars(safety).get('stage_timings') if safety is not None else None
    proof, dialog, final_handle = saved
    try:
        if identity_guard is not None:
            identity_guard()
        _check_destination(page, proof, dialog, final_handle)
        # Trial is actionability only, and happens before any durable attempt.
        final_handle.click(trial=True, timeout=9000)
        _check_destination(page, proof, dialog, final_handle)
        before_publish()
        # The reservation callback may itself inspect page identity. Recheck
        # state after it; a failure now remains a consumed, uncertain attempt.
        _check_destination(page, proof, dialog, final_handle)
        timeout=engine._final_click_timeout(before_final_click,default_ms=9000)
        with action_timing.measure(timings,'click'):
            final_handle.click(timeout=timeout)
        engine.ensure_ok(page)
    except PlaywrightError as exc:
        raise engine.StopRun('Финальная публикация не подтверждена; автоматический повтор запрещён') from exc


_REPOST_REFERENCE_JS = r"""({groupId,postId}) => {
  const arrays=[];
  if(Array.isArray(window.cur?.apiPrefetchCache)) arrays.push(window.cur.apiPrefetchCache);
  for(const script of document.querySelectorAll('script:not([src])')) {
    const raw=script.textContent||'', marker=/window\.cur\.apiPrefetchCache\s*=\s*/.exec(raw);
    if(!marker)continue;
    const body=raw.slice(marker.index+marker[0].length);
    const end=/;\s*window\.apiPrefetchReadyResolve\s*\(\s*\)/.exec(body);
    if(!end||end.index>8000000)continue;
    try {const parsed=JSON.parse(body.slice(0,end.index).trim());
      if(Array.isArray(parsed))arrays.push(parsed);}catch(_){}
  }
  const valid=n=>Number.isSafeInteger(n)&&n>0&&n<=Number.MAX_SAFE_INTEGER;
  const refs=new Map();let invalid=false;
  for(const entries of arrays)for(const entry of entries) {
    if(!['wall.get','wall.getById'].includes(entry?.method))continue;
    for(const post of entry?.response?.items||[]) {
      if(post.owner_id!==-groupId||post.id!==postId)continue;
      if(!Number.isInteger(post.date)||post.date<1200000000||post.date>=4102444800){invalid=true;continue;}
      let current=post,seen=new Set(),depth=0;
      while(true) {
        if(!Number.isSafeInteger(current.owner_id)||current.owner_id===0||
           !valid(current.id)||depth++>=8){invalid=true;break;}
        const key=current.owner_id+'_'+current.id;
        if(seen.has(key)){invalid=true;break;}seen.add(key);
        if(current.copy_history===undefined||Array.isArray(current.copy_history)&&current.copy_history.length===0) {
          const value={group_id:-current.owner_id,post_id:current.id,published_at:post.date};
          refs.set(JSON.stringify(value),value);break;
        }
        if(!Array.isArray(current.copy_history)||current.copy_history.length!==1){invalid=true;break;}
        current=current.copy_history[0];
        if(!current||typeof current!=='object'){invalid=true;break;}
      }
    }
  }
  return !invalid&&refs.size===1?[...refs.values()][0]:null;
}"""


def repost_reference(page,group_id,post_id):
    """Exact, page-bound original of a VK repost chain; never infer from links."""
    _ids(group_id,post_id);_source(page,group_id,post_id,actionable=False)
    value=page.evaluate(_REPOST_REFERENCE_JS,dict(groupId=group_id,postId=post_id))
    if not isinstance(value,dict):
        raise engine.StopRun('Нет единственной точной цепочки исходного репоста VK')
    _canonical_ids(value.get('group_id'),value.get('post_id'),value.get('published_at'))
    return value


_DESTINATION_POSTS_JS = r"""({communityId, sourceGroupId, sourcePostId}) => {
  const sourceKey=String(-sourceGroupId)+'_'+sourcePostId;
  const roots=[...document.querySelectorAll('[data-testid="post"][data-post-id]')];
  const found=new Set();
  for(const root of roots) {
    if(root.closest('[data-testid="wall_comments_comment_root"]')) continue;
    if(root.getAttribute('data-post-nesting-lvl') !== '0') continue;
    if(root.parentElement?.closest('[data-testid="post"][data-post-id]')) continue;
    const match=/^-(\d+)_(\d+)$/.exec(root.getAttribute('data-post-id') || '');
    if(!match || Number(match[1]) !== communityId || !Number.isSafeInteger(Number(match[2])) ||
       Number(match[2]) <= 0) continue;
    const nested=[...root.querySelectorAll('[data-testid="post"][data-post-id]')];
    if(!nested.some(post => post.getAttribute('data-post-id') === sourceKey &&
       post.getAttribute('data-post-nesting-lvl') === '1' &&
       post.parentElement?.closest('[data-testid="post"][data-post-id]') === root &&
       !post.closest('[data-testid="wall_comments_comment_root"]'))) continue;
    found.add(Number(match[2]));
  }
  return [...found];
}"""


def scan_destination(page, source_group_id: int, source_post_id: int,
                     community_id: int) -> list[dict]:
    """Inspect only rendered target posts. [] never proves exhaustive absence."""
    _canonical_ids(source_group_id, source_post_id, community_id)
    _PREPARED.pop(page, None)
    engine.ensure_ok(page)
    engine.pace_navigation(page)
    try:
        response = page.goto(f'https://vk.com/wall-{community_id}',
                             wait_until='domcontentloaded', timeout=60000)
        if response is not None and response.status == 429:
            raise engine.PlatformRateLimited('VK вернул HTTP 429 при чтении целевой стены')
        engine.ensure_ok(page)
        # Like the existing public wall reader, allow one bounded render pause.
        # There is no scrolling, retry loop or claim that the wall is exhaustive.
        page.wait_for_timeout(1200)
        engine.ensure_ok(page)
        url = urlsplit(page.url)
        if (url.scheme != 'https' or url.hostname not in engine.ALLOWED_HOSTS or
                url.path != f'/wall-{community_id}'):
            raise engine.StopRun('Не подтверждена точная целевая стена сообщества')
        ids = page.evaluate(_DESTINATION_POSTS_JS, dict(communityId=community_id,
                            sourceGroupId=source_group_id, sourcePostId=source_post_id))
        result = []
        for post_id in ids:
            engine.ensure_ok(page)
            date = multi.exact_post_date_info(page, community_id, post_id)
            stamp = date.get('publishedAt')
            if date.get('exact') is not True or type(stamp) is not int:
                stamp = None
            result.append({'post_id': post_id, 'published_at': stamp})
        return result
    except PlaywrightError as exc:
        engine.ensure_ok(page)
        raise engine.StopRun('Не удалось прочитать целевую стену; результат не подтверждён') from exc


def confirm_destination(page, source_group_id: int, source_post_id: int,
                        community_id: int, *, not_before: int, timeout_ms=9000) -> dict:
    """One navigation, then bounded DOM reads. Never submit, reload or scroll.

    The first rendered wall snapshot is not proof of absence: VK can mount the
    repost or its exact date later. Failure still consumes the original attempt.
    """
    _canonical_ids(source_group_id, source_post_id, community_id, not_before)
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 9000:
        raise ValueError('Confirmation timeout must be 1..9000ms')
    found = scan_destination(page, source_group_id, source_post_id, community_id)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        engine.ensure_ok(page)
        url = urlsplit(page.url)
        if (url.scheme != 'https' or url.hostname not in engine.ALLOWED_HOSTS or
                url.path != f'/wall-{community_id}'):
            raise engine.StopRun('Не подтверждена точная целевая стена сообщества')
        now = int(datetime.now(timezone.utc).timestamp())
        fresh = [r for r in found if type(r.get('published_at')) is int and
                 not_before - 5 <= r['published_at'] <= now + 60]
        if len(fresh) > 1:
            raise CommunityConfirmationError(ambiguous=True)
        if len(fresh) == 1:
            return fresh[0]
        if time.monotonic() >= deadline:
            raise CommunityConfirmationError()
        page.wait_for_timeout(100)
        engine.ensure_ok(page)
        ids = page.evaluate(_DESTINATION_POSTS_JS, dict(communityId=community_id,
                            sourceGroupId=source_group_id, sourcePostId=source_post_id))
        found = []
        for post_id in ids:
            date = multi.exact_post_date_info(page, community_id, post_id)
            stamp = date.get('publishedAt')
            found.append({'post_id': post_id, 'published_at': stamp
                          if date.get('exact') is True and type(stamp) is int else None})
