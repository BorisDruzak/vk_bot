#!/usr/bin/env python3
"""One-account, one-post VK UI proof of concept.

No continuous operation, no CAPTCHA bypass, no extraction of credentials or cookies.
Each interaction is manually selected and explicitly confirmed by its owner.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, unquote

from playwright.sync_api import Error as PlaywrightError, sync_playwright

BASE = Path(__file__).resolve().parent
PROFILE = BASE / ".vk-local-profile"
REPORT = BASE / "vk_test_result.json"
ALLOWED_HOSTS = {"vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru"}
POST_RE = re.compile(r"wall(-?\d+)_(\d+)")


def canonical_post_url(raw: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError("Нужна HTTPS-ссылка на пост vk.com, например https://vk.com/wall-123_456")
    source = unquote(parts.path + "?" + parts.query + "#" + parts.fragment)
    found = POST_RE.search(source)
    if not found:
        raise ValueError("В ссылке не найден ID поста вида wall-123_456")
    return f"https://vk.com/wall{found.group(1)}_{found.group(2)}"


def like_already_set(label: str) -> bool:
    """Conservatively interpret a VK button describing removal as an existing like."""
    return bool(re.search(r"убрать\s+реакци|снять\s+лайк|удалить\s+лайк|unlike|remove\s+(like|reaction)", label, re.I))


# All selection is based on the *currently visible DOM*, not VK's internal API.
# Main controls must be inside the specific post ID. This avoids clicking the
# 3 extra feed-post buttons seen during the user's initial inspection.
# Popup steps are found anew after each preceding click (menus change the DOM).
FIND_CONTROLS = r"""
({kind, postId}) => {
  const pattern = {
    like: /лайк|нравит|реакц|like|reaction/i,
    share: /поделить|репост|share|repost/i,
    own: /на\s+(?:своей|моей)\s+(?:странице|стене)|у\s+себя\s+на\s+(?:странице|стене)|на\s+личной\s+странице|(?:моя|своя)\s+страница|на\s+свою\s+стену|my\s+(?:wall|page)/i,
    publish: /опубликовать|разместить|поделиться\s+(?:записью|постом)|сделать\s+репост|опубликовать\s+запись|publish|post\s+to\s+wall/i
  }[kind];
  if (!pattern) return {rootFound:false, candidates:[]};
  document.querySelectorAll('[data-vk-poc-id]').forEach(el => el.removeAttribute('data-vk-poc-id'));

  const isVisible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0' &&
      el.getAttribute('aria-hidden') !== 'true' &&
      el.getAttribute('aria-disabled') !== 'true' && !el.hasAttribute('disabled');
  };
  const root = document.getElementById('post' + postId) ||
    [...document.querySelectorAll('[data-post-id]')].find(el => el.getAttribute('data-post-id') === postId) || null;
  // Fail closed if VK changes the post markup: never click random feed entries.
  if ((kind === 'like' || kind === 'share') && !root) return {rootFound:false, candidates:[]};

  const popupSelector = '[role="dialog"], [role="menu"], [aria-modal="true"], [class*="Modal"], [class*="Popup"], [class*="Popover"], [class*="popover"], [class*="Dropdown"]';
  const buttonSelector = 'button, [role="button"], [role="menuitem"], [role="radio"], a, [tabindex], [aria-label], [title], [data-testid]';
  const nodes = (kind === 'like' || kind === 'share')
    ? [...root.querySelectorAll(buttonSelector)]
    : [...document.querySelectorAll(buttonSelector)];
  const out = [];
  const seen = new Set();

  function addCandidate(el) {
    if (!el || seen.has(el) || !isVisible(el)) return;
    seen.add(el);
    const aria = (el.getAttribute('aria-label') || '').trim();
    const title = (el.getAttribute('title') || '').trim();
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    // Skip giant wrapper elements containing much of the page.
    const shortText = text.length <= 95 ? text : '';
    const matched = [aria, title, shortText].some(x => pattern.test(x));
    if (!matched) return;
    const popup = !!el.closest(popupSelector);
    const label = (aria || title || shortText).slice(0, 100);
    const cls = typeof el.className === 'string' ? el.className : '';
    const score = (pattern.test(aria) ? 100 : 0) + (pattern.test(title) ? 90 : 0) +
      (pattern.test(shortText) ? 60 : 0) + (popup ? 35 : 0) +
      (el.matches('button,[role="button"],[role="menuitem"]') ? 20 : 0);
    out.push({el, label, popup, score, className: cls.slice(0,70), tag:el.tagName.toLowerCase()});
  }
  nodes.forEach(addCandidate);

  // VK can render a clickable menu item as a plain div containing a span.
  // For popup-only steps, find exact visible text and resolve to its nearest
  // reasonable interactive ancestor. The operator still confirms the target.
  if (kind === 'own' || kind === 'publish') {
    for (const leaf of document.querySelectorAll('span, div, p')) {
      if (!isVisible(leaf)) continue;
      const txt = (leaf.innerText || '').replace(/\s+/g, ' ').trim();
      if (txt.length > 95 || !pattern.test(txt)) continue;
      if ([...leaf.children].some(child => pattern.test((child.innerText || '').trim()))) continue;
      const clickable = leaf.closest(buttonSelector) || leaf;
      const countBefore = out.length;
      addCandidate(clickable);
      // If the ancestor includes too much unrelated text, use the label leaf.
      if (out.length === countBefore && clickable !== leaf) addCandidate(leaf);
    }
  }

  out.sort((a,b) => b.score - a.score);
  const limited = out.slice(0,12);
  return {rootFound:!!root, candidates:limited.map((v,i) => {
    v.el.setAttribute('data-vk-poc-id','candidate-'+i);
    return {id:i, label:v.label, popup:v.popup, score:v.score, tag:v.tag, className:v.className};
  })};
}
"""


def controls(page, kind: str, post_id: str) -> dict:
    return page.evaluate(FIND_CONTROLS, {"kind": kind, "postId": post_id})


def choose_and_click(page, kind: str, post_id: str, name: str) -> str:
    """Return one of: clicked, already_liked, cancelled, missing, no_post_scope."""
    found = controls(page, kind, post_id)
    options = found["candidates"]
    if kind in ("like", "share") and not found["rootFound"]:
        print("[STOP] Не удалось точно определить контейнер нужного поста по ID.")
        print("Не нажимаем кнопки других записей. Нужна диагностика актуального DOM VK.")
        return "no_post_scope"
    if not options:
        print(f"[НЕ НАЙДЕНО] Не обнаружены элементы для «{name}».")
        return "missing"

    print(f"\nДоступные элементы: {name}")
    for opt in options:
        detail = " (меню/диалог)" if opt["popup"] else ""
        state = " [ЛАЙК УЖЕ СТОИТ: НЕ НАЖИМАТЬ]" if kind == "like" and like_already_set(opt["label"]) else ""
        print(f"  {opt['id']+1}. {opt['label']!r}{detail}{state}")
    raw = input("Номер нужного элемента (0 — отменить): ").strip()
    if not raw.isdecimal() or not (1 <= int(raw) <= len(options)):
        print("Действие отменено (введён 0 или некорректный номер).")
        return "cancelled"

    opt = options[int(raw)-1]
    if kind == "like" and like_already_set(opt["label"]):
        print("[ALREADY_LIKED] Кнопка предлагает УБРАТЬ лайк: повторный клик снял бы его.")
        print("Поэтому ничего не нажимаем.")
        return "already_liked"

    locator = page.locator(f'[data-vk-poc-id="candidate-{opt["id"]}"]')
    locator.scroll_into_view_if_needed(timeout=8000)
    locator.evaluate("el => {el.style.outline='3px solid #ff8000';el.style.outlineOffset='3px'}")
    print("Проверь ОРАНЖЕВУЮ рамку: выделен именно нужный пункт?")
    answer = input("Для клика введи ДА (Enter или 0 — отмена): ").strip().upper()
    if answer != "ДА":
        print("[CANCELLED] Нажатия не было. Требуется ввести ДА, затем Enter.")
        return "cancelled"
    locator.click(timeout=12000)
    print(f"[CLICKED] Нажатие выполнено: {name}. Это не доказывает, что VK принял действие.")
    return "clicked"


def operator_check(action_name: str) -> str:
    answer = input(f"В VK видно, что {action_name}? (да / нет / не уверен): ").strip().lower()
    return "confirmed_by_operator" if answer == "да" else ("failed" if answer == "нет" else "unverified")




# VK's new share dialog uses a radio choice "На своей стене" and can use
# "Отправить" for the submit button. We do not treat a text click as
# proof that the destination actually changed.
SHARE_DIALOG_JS = r"""
({action}) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0' &&
      el.getAttribute('aria-hidden') !== 'true';
  };
  const all = [...document.querySelectorAll('span, label, div, p, button')].filter(visible);
  function exactLeaf(texts) {
    return all.filter(el => texts.includes(norm(el.innerText)) &&
        ![...el.children].some(child => texts.includes(norm(child.innerText))))
      .sort((a,b) => a.getBoundingClientRect().width * a.getBoundingClientRect().height -
        b.getBoundingClientRect().width * b.getBoundingClientRect().height)[0] || null;
  }
  const own = exactLeaf(['На своей стене', 'На своей странице']);
  const dm = exactLeaf(['В личном сообщении', 'В личные сообщения', 'Личным сообщением']);
  if (!own || !dm) return {ok:false, reason:'Не найдены обе радиокнопки назначения в диалоге VK'};

  function candidateModal() {
    let e = own;
    while (e && e !== document.body) {
      const r = e.getBoundingClientRect();
      if (e.contains(dm) && r.width > 250 && r.height > 150 &&
          r.width < innerWidth * 0.85 && r.height < innerHeight * 0.95) {
        return e;
      }
      e = e.parentElement;
    }
    return null;
  }
  const modal = candidateModal();
  if (!modal) return {ok:false, reason:'Не удалось изолировать окно репоста. Ничего не нажато'};

  function controlFor(leaf) {
    // Prefer a genuine label, role=radio or a row with a native radio.
    let n = leaf;
    while (n && n !== modal) {
      if (n.matches('label, [role="radio"], [role="menuitemradio"]')) return n;
      const inputs = n.querySelectorAll('input[type="radio"], [role="radio"], [role="menuitemradio"]');
      const t = norm(n.innerText);
      if (inputs.length === 1 && t.length < 100 && !t.includes('В личном сообщении') &&
          !t.includes('На своей стене\nВ сообщество')) return n;
      n = n.parentElement;
    }
    return null;
  }
  function selected(leaf) {
    const el = controlFor(leaf);
    if (!el) return null;
    const input = el.matches('input[type="radio"]') ? el : el.querySelector('input[type="radio"]');
    if (input) return !!input.checked;
    const radio = el.matches('[role="radio"],[role="menuitemradio"]') ? el :
      el.querySelector('[role="radio"],[role="menuitemradio"]');
    if (radio && radio.hasAttribute('aria-checked')) return radio.getAttribute('aria-checked') === 'true';
    const state = el.getAttribute('data-state') || el.getAttribute('data-checked');
    if (state === 'checked' || state === 'true') return true;
    if (state === 'unchecked' || state === 'false') return false;
    return null;
  }
  const state = {wall:selected(own), direct_message:selected(dm)};
  if (action === 'state') return {ok:true, state};

  document.querySelectorAll('[data-vk-poc-own]').forEach(el => el.removeAttribute('data-vk-poc-own'));
  const oldMarker = document.getElementById('vk-poc-radio-marker');
  if (oldMarker) oldMarker.remove();
  const control = controlFor(own);
  // If the text is not wrapped in a semantic control, position the click on
  // the visible radio circle to its left, not on an unrelated text wrapper.
  let method = 'element';
  let point = null;
  let target = control;
  if (!target || !visible(target)) {
    const range = document.createRange();
    range.selectNodeContents(own);
    const t = range.getBoundingClientRect();
    const m = modal.getBoundingClientRect();
    const x = t.left - 22;
    const y = t.top + t.height / 2;
    if (!Number.isFinite(x) || !Number.isFinite(y) || x < m.left + 7 ||
        x > m.right - 7 || y < m.top + 7 || y > m.bottom - 7) {
      return {ok:false, reason:'Не удалось безопасно определить положение радиокнопки'};
    }
    method = 'point';
    point = {x, y};
    target = own;
    const marker = document.createElement('div');
    marker.id = 'vk-poc-radio-marker';
    Object.assign(marker.style, {
      position:'fixed',left:(x-13)+'px',top:(y-13)+'px',width:'26px',height:'26px',
      border:'3px solid orange',borderRadius:'50%',pointerEvents:'none',zIndex:'2147483647'
    });
    document.body.appendChild(marker);
  } else {
    target.style.outline = '3px solid orange';
    target.style.outlineOffset = '3px';
  }
  target.setAttribute('data-vk-poc-own', 'true');
  return {ok:true, method, point, state};
}
"""

PUBLISH_CONTROLS_JS = r"""
() => {
  // VK (20 Oct 2026): the real final control is
  // <button data-testid="share_tab_submit">Поделиться</button>.
  // Earlier versions mistakenly searched ONLY for a long text label and
  // scoped their search to the radio group, which may exclude the footer.
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 5 && r.height > 5 && s.display !== 'none' &&
      s.visibility !== 'hidden' && s.opacity !== '0' &&
      el.getAttribute('aria-hidden') !== 'true';
  };
  document.querySelectorAll('[data-vk-poc-publish]').forEach(el =>
    el.removeAttribute('data-vk-poc-publish'));

  function textLeaf(text) {
    return [...document.querySelectorAll('span,div,label,p')].filter(visible).find(el =>
      norm(el.innerText) === text &&
      ![...el.children].some(child => norm(child.innerText) === text)) || null;
  }
  const own = textLeaf('На своей стене') || textLeaf('На своей странице');
  const dm = textLeaf('В личном сообщении') || textLeaf('В личные сообщения') || textLeaf('Личным сообщением');
  if (!own || !dm) return {ok:false, reason:'Радиокнопки назначения репоста не найдены', candidates:[]};

  // The footer is commonly a SIBLING of the radio-group, so walk UP from
  // the candidate button to find the SHARED, reasonably sized modal root.
  // Reject document.body/overlay-wide wrappers to avoid unrelated buttons.
  function belongsToSameDialog(button) {
    let node = button.parentElement;
    while (node && node !== document.body && node !== document.documentElement) {
      if (node.contains(own) && node.contains(dm)) {
        const r = node.getBoundingClientRect();
        if (r.width > 250 && r.height > 150 &&
            r.width < innerWidth * 0.85 && r.height < innerHeight * 0.95 &&
            visible(node)) return true;
      }
      node = node.parentElement;
    }
    return false;
  }

  const specific = [...document.querySelectorAll('button[data-testid="share_tab_submit"]')]
    .filter(visible);
  // For older VK layouts: exact text matching ONLY inside the same dialog;
  // the generic «Поделиться» is allowed ONLY with the explicit test id.
  const legacy = [...document.querySelectorAll('button,[role="button"],input[type="submit"]')]
    .filter(visible)
    .filter(el => !el.matches('button[data-testid="share_tab_submit"]'));
  const broadPattern = /^(?:Отправить|Опубликовать(?: запись)?|Разместить|Поделиться (?:постом|записью))$/i;
  const taggedPattern = /^(?:Отправить|Опубликовать(?: запись)?|Разместить|Поделиться(?: (?:постом|записью))?)$/i;
  const buttons = specific.length ? specific : legacy;
  const matches = [];
  for (const el of buttons) {
    if (!belongsToSameDialog(el)) continue;
    const variants = [norm(el.innerText), norm(el.getAttribute('aria-label')), norm(el.value)];
    const label = variants.find(x => (specific.length ? taggedPattern : broadPattern).test(x));
    if (!label) continue;
    const css = getComputedStyle(el);
    const disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true' ||
      el.getAttribute('aria-busy') === 'true' || el.getAttribute('data-disabled') === 'true' ||
      css.pointerEvents === 'none');
    matches.push({el, label, disabled});
  }
  if (matches.length !== 1) {
    return {ok:false, reason: matches.length ?
      'Обнаружено несколько финальных кнопок — безопасный выбор невозможен' :
      (specific.length ? 'Кнопка share_tab_submit есть, но не подтверждена принадлежность окну репоста' :
       'Финальная кнопка в окне репоста не обнаружена'), candidates:[]};
  }
  matches[0].el.setAttribute('data-vk-poc-publish','0');
  return {ok:true, candidates:[{id:0, label:matches[0].label, disabled:matches[0].disabled}]};
}
"""


def choose_wall_destination(page) -> str:
    """Select the radio explicitly, verify state, else require visible owner check."""
    found = page.evaluate(SHARE_DIALOG_JS, {"action": "prepare"})
    if not found["ok"]:
        print("[STOP]", found["reason"])
        return "destination_not_found"
    state = found["state"]
    if state["wall"] is True and state["direct_message"] is not True:
        print("[SELECTED] Радиокнопка «На своей стене» уже включена.")
    else:
        print("\nВыбираем радиокнопку «На своей стене» (НЕ «В личном сообщении»).")
        print("До клика: wall=", state["wall"], ", message=", state["direct_message"])
        print("Оранжевым отмечена целевая строка или радиокнопка.")
        if input("Подтверждаешь правильный элемент? Введи ДА: ").strip().upper() != "ДА":
            return "cancelled"
        if found["method"] == "element":
            page.locator('[data-vk-poc-own="true"]').click(timeout=12000)
        else:
            page.mouse.click(found["point"]["x"], found["point"]["y"])
        print("[CLICKED] Выполнен клик по радиокнопке назначения.")
        page.wait_for_timeout(500)
    current = page.evaluate(SHARE_DIALOG_JS, {"action": "state"})
    if not current["ok"]:
        print("[STOP] Окно репоста исчезло или изменилось; публикацию не выполняем.")
        return "destination_unverified"
    state = current["state"]
    print("Проверка назначения: wall=", state["wall"], ", message=", state["direct_message"])
    if state["direct_message"] is True or state["wall"] is False:
        print("[STOP] VK не подтвердил переключение на личную стену. Нажатие публикации запрещено.")
        return "destination_not_selected"
    if state["wall"] is True:
        print("[VERIFIED] По состоянию DOM выбрана личная стена.")
        return "verified"
    # Many VK layouts have non-semantic, custom-styled radio controls.
    print("[UNKNOWN] Разметка VK не раскрывает checked / aria-checked.")
    print("Посмотри на окно VK: синий кружок должен быть у «На своей стене»,")
    print("а у «В личном сообщении» кружок должен быть пустым.")
    answer = input("Видишь именно такое состояние? (да/нет): ").strip().lower()
    return "confirmed_visually" if answer == "да" else "destination_unverified"


def choose_publish(page) -> str:
    found = page.evaluate(PUBLISH_CONTROLS_JS)
    if not found["ok"]:
        print("[STOP]", found["reason"])
        return "publish_not_found"
    options = found["candidates"]
    if not options:
        print("[STOP] Не найдена кнопка публикации внутри окна «Поделиться».")
        return "publish_not_found"
    print("\nКнопка завершения репоста:")
    for option in options:
        print(f"  {option['id']+1}. {option['label']!r}" + (" [НЕАКТИВНА]" if option["disabled"] else ""))
    raw = input("Номер кнопки (0 — отменить): ").strip()
    if not raw.isdecimal() or not (1 <= int(raw) <= len(options)):
        return "cancelled"
    selected = options[int(raw)-1]
    if selected["disabled"]:
        print("[STOP] Кнопка VK неактивна. Не пытаемся обходить ограничение.")
        return "publish_disabled"
    locator = page.locator(f'[data-vk-poc-publish="{selected["id"]}"]')
    locator.scroll_into_view_if_needed(timeout=8000)
    locator.evaluate("el => {el.style.outline='3px solid orange';el.style.outlineOffset='3px'}")
    print("Проверь: выделена именно кнопка завершения репоста на ТВОЕЙ СТЕНЕ.")
    if input("Для публикации введи ДА (остальное — отмена): ").strip().upper() != "ДА":
        return "cancelled"
    # Re-check destination just before the irreversible publish action.
    current = page.evaluate(SHARE_DIALOG_JS, {"action": "state"})
    if not current["ok"] or current["state"]["direct_message"] is True or \
            current["state"]["wall"] is False:
        print("[STOP] Назначение изменилось перед публикацией; действий не выполняем.")
        return "destination_changed"
    locator.click(timeout=12000)
    print(f"[CLICKED] {selected['label']}. Подтверди итог непосредственно в VK.")
    return "clicked"


def repost_flow(page, post_id: str) -> str:
    print("\nЭтап репоста 1/3 — открыть «Поделиться» у НУЖНОГО поста.")
    first = choose_and_click(page, "share", post_id, "Поделиться")
    if first != "clicked":
        return first
    page.wait_for_timeout(500)

    print("\nЭтап репоста 2/3 — выбрать «На своей стене».")
    state = choose_wall_destination(page)
    if state not in ("verified", "confirmed_visually"):
        print("[INCOMPLETE] Без проверки получателя репост не отправляется.")
        return state

    print("\nЭтап репоста 3/3 — завершить публикацию.")
    print('Ищем финальную кнопку VK по data-testid="share_tab_submit" (надпись может быть «Поделиться»).')
    already = input("Репост УЖЕ появился на твоей стене, без третьего клика? (да/нет): ").strip().lower()
    if already == "да":
        return operator_check("репост появился на твоей странице")
    result = choose_publish(page)
    if result != "clicked":
        print("[INCOMPLETE] Финальная публикация не выполнена.")
        return result
    return operator_check("репост появился на твоей странице")


def save_report(url: str, results: dict[str, str]) -> None:
    data = {
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "post_url": url,
        "results": results,
        "note": "Результаты по видимому интерфейсу и словам оператора; API VK не проверялось",
    }
    REPORT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="VK POC v5: один аккаунт, одна публикация")
    parser.add_argument("post_url", help="Например https://vk.com/wall-123_456")
    parser.add_argument("--action", choices=("inspect", "like", "repost", "both"), default="inspect")
    args = parser.parse_args()
    try:
        url = canonical_post_url(args.post_url)
    except ValueError as error:
        parser.error(str(error))
    post_id = urlsplit(url).path.removeprefix("/wall")
    PROFILE.mkdir(parents=True, exist_ok=True)
    print(f"VK POC v5 | публикация: {url}")
    print(f"Локальный браузерный профиль: {PROFILE}")
    print("Не отправляй профиль браузера, cookie, пароли или токены.")
    results: dict[str, str] = {}

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE), headless=False, locale="ru-RU",
                viewport={"width": 1320, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                print("\nАвторизуйся вручную, если VK попросит войти.")
                input("Когда нужный пост открыт, нажми Enter в PowerShell... ")
                if "wall" not in page.url or "id.vk.com" in page.url:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(750)

                if args.action == "inspect":
                    for kind in ("like", "share"):
                        found = controls(page, kind, post_id)
                        print(f"\n{kind}: post_scope={found['rootFound']}, кандидатов={len(found['candidates'])}")
                        for item in found["candidates"]:
                            print(f"   - {item['label']}")
                    print("[INSPECT_OK] Действия не выполнялись.")
                else:
                    if args.action in ("like", "both"):
                        print("\nЭтап лайка: кнопка «Убрать реакцию» означает, что лайк УЖЕ стоит.")
                        status = choose_and_click(page, "like", post_id, "Лайк")
                        results["like"] = operator_check("лайк стоит у нужного поста") if status == "clicked" else status
                    if args.action in ("repost", "both"):
                        results["repost"] = repost_flow(page, post_id)
                    save_report(url, results)
                    print(f"\nОтчёт: {REPORT} (только ссылка и статусы).")
                input("\nEnter — закрыть браузер теста... ")
            finally:
                context.close()
    except PlaywrightError as error:
        if results:
            save_report(url, results)
        print("[ERROR] Playwright/VK:", str(error)[:750])
        print("Повторные попытки не выполняются автоматически.")
        return 2
    except (KeyboardInterrupt, EOFError):
        if results:
            save_report(url, results)
        print("\nТест остановлен пользователем.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
