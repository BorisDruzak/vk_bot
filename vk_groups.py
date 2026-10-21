#!/usr/bin/env python3
"""VK Batch multi-group dispatcher for ONE owner-authorized local browser profile.

The script reuses the tested single-post engine from vk_batch.py.
It does not impersonate users, evade detection, solve challenges, or rotate IPs.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PWTimeout, sync_playwright

import vk_batch as engine

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / 'vk_groups.json'
SOURCE_POST_RE = re.compile(r'^-(\d+)_(\d+)$')

# Only public post identity and visible date text are returned from the page.
DISCOVER_DATED_JS = r"""
({groupId}) => {
  const prefix = '-' + String(groupId) + '_';
  const items = [];
  for (const root of document.querySelectorAll('[data-testid="post"][data-post-id]')) {
    if (root.getAttribute('data-post-nesting-lvl') === '1') continue;
    const key = root.getAttribute('data-post-id') || '';
    if (!key.startsWith(prefix)) continue;
    const postId = Number(key.slice(prefix.length));
    if (!Number.isSafeInteger(postId) || postId <= 0) continue;
    const dateElement = [...root.querySelectorAll('[data-testid="post_date_block_preview"]')]
      .find(node => node.closest('[data-testid="post"]') === root);
    const timeEl = dateElement?.querySelector('time[datetime]');
    items.push({postId, dateText:(dateElement?.textContent || '').trim().slice(0,90),
                exactTime:(timeEl?.getAttribute('datetime') || '').slice(0,50)});
  }
  // Do not search the whole page for generic links: only explicit source post roots.
  return items;
}
"""

# Read publication timestamp only from a VERIFIED source for the specified
# owner/post ID. The visible "1 ч назад" is NEVER an exact timestamp. JSON
# prefetch is preferred; a scoped DOM <time datetime> / explicit UNIX date
# attribute, or this page's published_time meta tag can also be unambiguous.
# Do not return raw HTML, script source, cookies, or any session data.
EXACT_DATE_JS = r"""
({groupId, postId}) => {
  const owner = -Number(groupId), id = Number(postId);
  const expectedPath = '/wall-' + Number(groupId) + '_' + id;
  const inBounds = n => Number.isSafeInteger(n) && n > 1200000000 && n < 4102444800;
  const unix = raw => {
    if (!/^\d{10}(?:\d{3})?$/.test(String(raw || '').trim())) return null;
    const n = Number(raw);
    const value = n >= 1000000000000 ? Math.floor(n / 1000) : n;
    return inBounds(value) ? value : null;
  };
  const iso = raw => {
    const str = String(raw || '').trim();
    // An offset is required. Do not silently interpret dates in an arbitrary
    // machine-local timezone as authoritative.
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})$/i.test(str)) return null;
    const millis = Date.parse(str);
    const n = Math.floor(millis / 1000);
    return inBounds(n) ? n : null;
  };
  const result = (dates, source) => dates.size === 1
      ? {publishedAt:[...dates][0], source, exact:true}
      : {publishedAt:null, source:dates.size > 1 ? 'date-conflict' : 'date-unknown', exact:false};
  const candidateArrays = [];
  if (Array.isArray(window.cur?.apiPrefetchCache))
    candidateArrays.push(window.cur.apiPrefetchCache);
  for (const script of document.querySelectorAll('script:not([src])')) {
    const raw = script.textContent || '';
    const marker = /window\.cur\.apiPrefetchCache\s*=\s*/.exec(raw);
    if (!marker) continue;
    const body = raw.slice(marker.index + marker[0].length);
    const end = /;\s*window\.apiPrefetchReadyResolve\s*\(\s*\)/.exec(body);
    if (!end || end.index > 8000000) continue;
    const json = body.slice(0, end.index).trim();
    if (!json.startsWith('[')) continue;
    try {
      const parsed = JSON.parse(json);
      if (Array.isArray(parsed)) candidateArrays.push(parsed);
    } catch (_) { /* NEVER execute scripts */ }
  }
  const jsonDates = new Set();
  for (const entries of candidateArrays) {
    for (const entry of entries) {
      if (!['wall.getById', 'wall.get'].includes(entry?.method)) continue;
      for (const post of (entry?.response?.items || [])) {
        if (Number(post?.owner_id) !== owner || Number(post?.id) !== id) continue;
        const value = Number(post?.date);
        if (inBounds(value)) jsonDates.add(value);
      }
    }
  }
  if (jsonDates.size) return result(jsonDates, 'vk-post-json');

  // Never read timestamps in embedded reposts, recommendations, comments.
  const root = document.querySelector('[data-testid="post"][data-post-id="-' + groupId + '_' + id + '"]');
  if (root) {
    const anchors = [...root.querySelectorAll('[data-testid="post_date_block_preview"]')]
      .filter(el => el.closest('[data-testid="post"][data-post-id]') === root)
      .filter(el => {try {return new URL(el.getAttribute('href') || '', 'https://vk.com/').pathname === expectedPath;}
                     catch (_) {return false;}});
    if (anchors.length === 1) {
      const anchor = anchors[0];
      const time = anchor.matches('time[datetime]') ? anchor : anchor.querySelector('time[datetime]');
      const isoDates = new Set();
      const isoValue = iso(time?.getAttribute('datetime') || anchor.getAttribute('datetime'));
      if (isoValue !== null) isoDates.add(isoValue);
      if (isoDates.size) return result(isoDates, 'vk-dom-iso');
      const unixDates = new Set();
      for (const key of ['data-timestamp', 'data-unixtime', 'data-published-at']) {
        const value = unix(anchor.getAttribute(key) || time?.getAttribute(key));
        if (value !== null) unixDates.add(value);
      }
      if (unixDates.size) return result(unixDates, 'vk-dom-unix');
    }
  }

  // A page-scoped meta timestamp is safe only when the current route is
  // exactly the requested post, not a group feed or unrelated page.
  if (location.pathname === expectedPath) {
    const og = document.querySelector('meta[property="og:url"]')?.getAttribute('content');
    const ogMatches = !og || (() => {try {return new URL(og,location.href).pathname === expectedPath;}
                                   catch (_) {return false;}})();
    if (ogMatches) {
      const dates = new Set();
      for (const key of ['article:published_time', 'article:published', 'pubdate']) {
        const value = iso(document.querySelector('meta[property="' + key + '"]')?.getAttribute('content'));
        if (value !== null) dates.add(value);
      }
      if (dates.size) return result(dates, 'vk-meta-iso');
    }
  }
  return {publishedAt:null, source:'date-unknown', exact:false};
}
"""

# Capture only short, public date metadata for this exact permalink. The
# tooltip can have a human-readable local time without year/timezone, so it
# remains a PRIORITY HINT, not an exact time of publication.
DATE_ANCHOR_JS = r"""
({groupId, postId}) => {
  const expected = '/wall-' + Number(groupId) + '_' + Number(postId);
  const root = document.querySelector('[data-testid="post"][data-post-id="-' + Number(groupId) + '_' + Number(postId) + '"]');
  if (!root) return {found:false, text:'', title:'', aria:'', datetime:''};
  const anchors = [...root.querySelectorAll('[data-testid="post_date_block_preview"]')]
    .filter(el => el.closest('[data-testid="post"][data-post-id]') === root)
    .filter(el => {try {return new URL(el.getAttribute('href') || '', 'https://vk.com/').pathname === expected;}
                   catch (_) {return false;}});
  if (anchors.length !== 1) return {found:false, text:'', title:'', aria:'', datetime:''};
  const el = anchors[0];
  return {found:true, text:(el.textContent || '').trim().slice(0,90),
          title:(el.getAttribute('title') || '').trim().slice(0,120),
          aria:(el.getAttribute('aria-label') || '').trim().slice(0,120),
          datetime:(el.getAttribute('datetime') || el.querySelector('time[datetime]')?.getAttribute('datetime') || '').slice(0,75)};
}
"""

VISIBLE_DATE_TOOLTIP_JS = r"""
() => {
  const selectors = '[role="tooltip"],[data-testid="tooltip"],.vkuiTooltipBase__content,[class*="TooltipBase__content"],[data-testid*="date-tooltip"]';
  const result = [];
  for (const el of new Set(document.querySelectorAll(selectors))) {
    const box = el.getBoundingClientRect(), style = getComputedStyle(el);
    if (box.width <= 2 || box.height <= 2 || style.visibility === 'hidden' || style.display === 'none') continue;
    const value = (el.textContent || '').replace(/\s+/g,' ').trim().slice(0,140);
    if (value && /\d{1,2}:\d{2}|(?:янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек|\d{2}\.\d{2})/i.test(value)) result.push(value);
  }
  return result.slice(0, 8);
}
"""

MONTHS = {
    'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'ма': 5, 'июн': 6,
    'июл': 7, 'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12,
}


def approximate_post_date(date_text: str, exact_time: str = '', *, now: datetime | None = None) -> int | None:
    """Feed-only priority hint. Never use it as execution authorization."""
    now = now or datetime.now(timezone.utc)
    if exact_time:
        try:
            parsed = datetime.fromisoformat(exact_time.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return int(parsed.timestamp())
        except ValueError:
            pass
    txt = re.sub(r'\s+', ' ', (date_text or '').lower().replace('\xa0', ' ')).strip()
    if not txt:
        return None
    if re.search(r'только что|секунд|сек\b', txt):
        return int(now.timestamp())
    if txt.startswith('сегодня'):
        return int(now.timestamp())
    if txt.startswith('вчера'):
        return int((now - timedelta(days=1)).timestamp())
    m = re.search(r'(\d+)\s*(?:мин(?:ут)?(?:ы|у)?\b|м\b)', txt)
    if m:
        return int((now - timedelta(minutes=int(m.group(1)))).timestamp())
    m = re.search(r'(\d+)\s*(?:час(?:а|ов)?\b|ч\b)', txt)
    if m:
        return int((now - timedelta(hours=int(m.group(1)))).timestamp())
    m = re.search(r'(\d+)\s*(?:д(?:ня|ней)?\b|сут(?:ки|ок)?\b)', txt)
    if m:
        return int((now - timedelta(days=int(m.group(1)))).timestamp())
    m = re.search(r'(\d+)\s*(?:нед(?:ел(?:и|ю|ь))?\b)', txt)
    if m:
        return int((now - timedelta(weeks=int(m.group(1)))).timestamp())
    # Absolute VK dates without a year: approximate year only, exact post JSON
    # will still be required before execution.
    m = re.search(r'\b(\d{1,2})\s+([а-яё]+)(?:\s+(20\d{2}))?\b', txt)
    if m:
        day = int(m.group(1))
        key = m.group(2)[:3] if not (m.group(2).startswith('май') or m.group(2).startswith('мая')) else 'ма'
        month = MONTHS.get(key)
        if month:
            try:
                result = datetime(int(m.group(3) or now.year), month, day, tzinfo=timezone.utc)
                if m.group(3) is None and result > now + timedelta(days=1):
                    result = result.replace(year=result.year - 1)
                return int(result.timestamp())
            except ValueError:
                return None
    return None


def _display_date_timestamp(text: str, *, now: datetime | None = None) -> int | None:
    """Best-effort absolute date shown in a VK tooltip, in the browser's OS-local time.

    Tooltip formats don't carry a timezone and often omit the year. Even with
    a full date this is not an authoritative epoch. NEVER mark it exact.
    """
    txt = re.sub(r'\s+', ' ', (text or '').lower().replace('\xa0', ' ')).strip()
    if not txt or len(txt) > 140:
        return None
    clock = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)', txt)
    if not clock:
        return None
    hour, minute, second = (int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0))
    local_now = now or datetime.now().astimezone()
    # Convert with the host's local timezone, which Chromium uses unless a
    # browser timezone was explicitly overridden (we do not override it).
    if txt.startswith('сегодня') or txt.startswith('вчера'):
        dt = local_now.date() - timedelta(days=1 if txt.startswith('вчера') else 0)
        return int(datetime(dt.year, dt.month, dt.day, hour, minute, second).timestamp())
    numerical = re.search(r'(?<!\d)([0-3]?\d)[./-]([01]?\d)[./-](20\d{2})(?!\d)', txt)
    if numerical:
        day, month, year = (int(numerical.group(1)), int(numerical.group(2)), int(numerical.group(3)))
    else:
        named = re.search(r'(?<!\d)([0-3]?\d)\s+([а-яё]{3,12})\.?\s*(?:(20\d{2})(?:\s*(?:г\.?|года))?)?', txt)
        if not named:
            return None
        day = int(named.group(1))
        key = named.group(2)[:3] if not named.group(2).startswith(('май','мая')) else 'ма'
        month = MONTHS.get(key)
        if not month:
            return None
        year = int(named.group(3) or local_now.year)
    try:
        naive = datetime(year, month, day, hour, minute, second)
        if not numerical and named.group(3) is None and naive.date() > local_now.date() + timedelta(days=1):
            naive = naive.replace(year=year - 1)
        result = int(naive.timestamp())
        if 1200000000 <= result <= local_now.timestamp() + 3600:
            return result
    except (ValueError, OverflowError, OSError):
        return None
    return None


def read_post_date_hint(page, gid: int, pid: int, *, hover: bool = True) -> dict:
    """Read only date link metadata/hover tooltip of the EXACT main post.

    Date text can be approximate. No HTML, cookies, tokens, hidden data, clicks,
    or profile state are collected or changed.
    """
    hint = page.evaluate(DATE_ANCHOR_JS, {'groupId':gid, 'postId':pid})
    if not isinstance(hint, dict) or not hint.get('found'):
        return {'found':False, 'text':'', 'title':'', 'aria':'', 'datetime':'', 'tooltip':''}
    hint['tooltip'] = ''
    if hover:
        # Exact href + main-post root prevents hovering a repost or comment.
        selector = (f'[data-testid="post"][data-post-id="-{gid}_{pid}"] '
                    f'[data-testid="post_date_block_preview"][href="/wall-{gid}_{pid}"]')
        try:
            loc = page.locator(selector)
            if loc.count() == 1:
                before = set(page.evaluate(VISIBLE_DATE_TOOLTIP_JS))
                loc.hover(timeout=3000)
                page.wait_for_timeout(350)
                after = page.evaluate(VISIBLE_DATE_TOOLTIP_JS)
                fresh = [x for x in after if x not in before]
                valid = [x for x in fresh if _display_date_timestamp(x) is not None]
                hint['tooltip'] = (valid or fresh or [''])[0][:140]
        except (PWTimeout, PlaywrightError):
            # Hover is an optional, read-only hint; inability to hover must not
            # be interpreted as an unavailable page or a known timestamp.
            pass
    return hint


def approximate_from_date_hint(hint: dict) -> tuple[int | None, str]:
    for source in ('tooltip', 'title', 'aria'):
        epoch = _display_date_timestamp(hint.get(source, ''))
        if epoch is not None:
            return epoch, 'tooltip-local-approx'
    text = hint.get('text', '')
    if text:
        epoch = approximate_post_date(text)
        if epoch is not None:
            return epoch, 'post-relative-approx'
    return None, 'date-unknown'


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    # v6.13 configurations contain all 57 groups but predate this setting.
    # Migration is in memory only: never overwrite customized group JSON.
    data.setdefault('scan_groups_per_run', 3)
    # Conservative read-only defaults; the existing 57-group JSON remains valid.
    data.setdefault('scan_groups_cap', 1)
    data.setdefault('scan_scroll_cap', 2)
    data.setdefault('read_pause_seconds', 15.0)
    data.setdefault('audit_posts_per_run', 3)
    data.setdefault('scan_min_interval_hours', 12.0)
    if type(data.get('lookback_days')) is not int or not (1 <= data['lookback_days'] <= 365):
        raise ValueError('lookback_days должен быть целым числом 1..365')
    for key, low, high in [('scan_scrolls_per_group', 1, 500), ('stale_scrolls', 2, 50),
                           ('scan_groups_per_run', 1, 57)]:
        if type(data.get(key)) is not int or not low <= data[key] <= high:
            raise ValueError(f'{key} должен быть целым числом {low}..{high}')
    for key in ('scan_pause_seconds', 'action_pause_seconds'):
        if type(data.get(key)) not in (int, float) or not 0 <= data[key] <= 60:
            raise ValueError(f'{key}: допустимы значения 0..60 секунд')
    if type(data['scan_groups_cap']) is not int or not 1 <= data['scan_groups_cap'] <= 3:
        raise ValueError('scan_groups_cap: допустимы значения 1..3')
    if type(data['scan_scroll_cap']) is not int or not 1 <= data['scan_scroll_cap'] <= 10:
        raise ValueError('scan_scroll_cap: допустимы значения 1..10')
    if type(data['read_pause_seconds']) not in (int, float) or not 5 <= data['read_pause_seconds'] <= 300:
        raise ValueError('read_pause_seconds: допустимы значения 5..300 секунд')
    if type(data['audit_posts_per_run']) is not int or not 1 <= data['audit_posts_per_run'] <= 50:
        raise ValueError('audit_posts_per_run: допустимы значения 1..50')
    if type(data['scan_min_interval_hours']) not in (int, float) or not 1 <= data['scan_min_interval_hours'] <= 168:
        raise ValueError('scan_min_interval_hours: допустимы значения 1..168 часов')
    for key in ('recent_only',):
        if type(data.get(key)) is not bool:
            raise ValueError(f'{key} должен быть true или false')
    if not isinstance(data.get('actions'), dict) or any(type(data['actions'].get(a)) is not bool for a in ('like','repost')):
        raise ValueError('actions.like и actions.repost должны быть true/false')
    limits = data.get('limits')
    keys = ('posts_per_run','actions_per_run','actions_per_24h',
            'likes_per_run','likes_per_24h','reposts_per_run','reposts_per_24h')
    if not isinstance(limits, dict) or any(type(limits.get(k)) is not int or limits[k] <= 0 for k in keys):
        raise ValueError('Все limits должны быть положительными целыми числами')
    groups = data.get('groups')
    if not isinstance(groups, list) or not groups:
        raise ValueError('Список groups пуст')
    seen = set()
    for group in groups:
        if not isinstance(group, dict) or type(group.get('id')) is not int or group['id'] <= 0:
            raise ValueError('Неверный id сообщества')
        if group['id'] in seen:
            raise ValueError(f'Дублируется id сообщества: {group["id"]}')
        seen.add(group['id'])
        if not isinstance(group.get('name'), str) or not group['name'].strip():
            raise ValueError(f'Нет названия группы {group["id"]}')
        for flag in ('enabled','like_enabled','repost_enabled'):
            if type(group.get(flag)) is not bool:
                raise ValueError(f'{flag} должен быть true/false, группа {group["id"]}')
        engine.validate_group_url(group.get('url', ''))
        if re.fullmatch(r'https://vk\.com/club(\d+)', group['url']) is None or int(group['url'].split('club')[1]) != group['id']:
            raise ValueError(f'ID и URL сообщества не совпадают: {group["id"]}')
    return data


def selected_groups(cfg: dict, only: str | None, max_groups: int) -> list[dict]:
    enabled = [g for g in cfg['groups'] if g['enabled']]
    if only:
        try:
            ids = {int(x.strip()) for x in only.split(',') if x.strip()}
        except ValueError as exc:
            raise ValueError('--only принимает числовые ID через запятую') from exc
        if not ids or not ids.issubset({g['id'] for g in cfg['groups']}):
            raise ValueError('В --only указан ID, которого нет в JSON')
        enabled = [g for g in enabled if g['id'] in ids]
    if max_groups:
        enabled = enabled[:max_groups]
    return enabled


def init_dates(db):
    db.execute('''CREATE TABLE IF NOT EXISTS post_publication (
        group_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
        published_at INTEGER, source TEXT NOT NULL, exact INTEGER NOT NULL DEFAULT 0,
        checked_at TEXT NOT NULL, exact_attempt_at TEXT,
        PRIMARY KEY(group_id, post_id))''')
    # v6.11/6.12 databases lack this column. Preserve existing rows, statuses,
    # browser auth and exact timestamps with one idempotent additive migration.
    columns = {row['name'] for row in db.execute('PRAGMA table_info(post_publication)')}
    if 'exact_attempt_at' not in columns:
        db.execute('ALTER TABLE post_publication ADD COLUMN exact_attempt_at TEXT')
    db.commit()


def init_scan_progress(db) -> None:
    # One progress cursor across runs. Scanning is split into modest batches;
    # this never retries a VK rate limit or increases frequency to bypass it.
    db.execute('''CREATE TABLE IF NOT EXISTS group_scan_progress (
        group_id INTEGER PRIMARY KEY,
        last_scanned_at TEXT NOT NULL,
        discovered INTEGER NOT NULL
    )''')
    db.commit()


def choose_scan_groups(db, groups: list[dict], limit: int, *,
                       min_interval_hours: float = 0.0) -> list[dict]:
    # Unscanned groups first, then the least recently scanned. A group whose
    # scan completed recently is skipped, avoiding redundant network reads.
    stamps = {r['group_id']:r['last_scanned_at'] for r in
              db.execute('SELECT group_id,last_scanned_at FROM group_scan_progress')}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_interval_hours)
    due = []
    for group in groups:
        value = stamps.get(group['id'])
        if value is not None and min_interval_hours:
            try:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo is not None and stamp > cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # Corrupt progress timestamp: scan once and repair it.
        due.append(group)
    return sorted(due, key=lambda g:(stamps.get(g['id']) is not None,
                                     stamps.get(g['id']) or ''))[:limit]


def mark_group_scanned(db, group_id: int, discovered: int) -> None:
    db.execute('''INSERT INTO group_scan_progress(group_id,last_scanned_at,discovered)
        VALUES(?,?,?) ON CONFLICT(group_id) DO UPDATE SET
        last_scanned_at=excluded.last_scanned_at,discovered=excluded.discovered''',
        (group_id,engine.moment(),discovered))
    db.commit()


def save_date(db, gid: int, pid: int, published_at: int | None, source: str, exact: bool,
              *, date_checked: bool = False):
    if published_at is not None and not (1200000000 <= published_at < 4102444800):
        raise ValueError('Unexpected publication timestamp')
    at = engine.moment()
    verified_at = at if date_checked else None
    if published_at is None:
        # An unavailable authoritative date is not evidence that a prior
        # relative-time hint was invalid. Keep the hint for prioritization.
        # exact_attempt_at stops the same post from monopolizing every audit.
        db.execute('''INSERT INTO post_publication
            (group_id,post_id,published_at,source,exact,checked_at,exact_attempt_at)
            VALUES(?,?,NULL,?,0,?,?)
            ON CONFLICT(group_id,post_id) DO UPDATE SET
              published_at=post_publication.published_at,
              source=CASE WHEN post_publication.published_at IS NULL
                          THEN excluded.source ELSE post_publication.source END,
              checked_at=excluded.checked_at,
              exact_attempt_at=COALESCE(excluded.exact_attempt_at,post_publication.exact_attempt_at)''',
            (gid,pid,source,at,verified_at))
        db.commit()
        return
    # A relative/tooltip hint must never overwrite a verified exact date.
    db.execute('''INSERT INTO post_publication
        (group_id,post_id,published_at,source,exact,checked_at,exact_attempt_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(group_id,post_id) DO UPDATE SET
          published_at=CASE WHEN post_publication.exact=1 AND excluded.exact=0
                         THEN post_publication.published_at ELSE excluded.published_at END,
          source=CASE WHEN post_publication.exact=1 AND excluded.exact=0
                    THEN post_publication.source ELSE excluded.source END,
          exact=MAX(post_publication.exact, excluded.exact),
          checked_at=excluded.checked_at,
          exact_attempt_at=COALESCE(excluded.exact_attempt_at,post_publication.exact_attempt_at)''',
        (gid,pid,published_at,source,int(exact),at,verified_at))
    db.commit()


def scan_group(page, db, group: dict, cfg: dict) -> tuple[int,int]:
    gid = group['id']
    url = engine.validate_group_url(group['url'])
    print(f'[SCAN] {gid} | {group["name"][:65]}')
    engine.pace_navigation(page)
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
    except (PWTimeout, PlaywrightError) as exc:
        # A rejected request is NOT an ordinary network skip.
        engine.ensure_ok(page)
        print(f'   [SKIP] Не удалось открыть сообщество ({type(exc).__name__})')
        return None
    page.wait_for_timeout(1200)
    engine.ensure_ok(page)
    discovered: set[int] = set()
    new_total = 0
    empty = 0
    page.mouse.move(720,620)
    # A respectful floor on UI polling: not a VK-approved safe quota or an
    # attempt to circumvent a detected rate limit. Detection always stops.
    scan_pause=max(3.0,cfg['scan_pause_seconds'])
    if cfg['scan_pause_seconds'] < scan_pause:
        print(f'   [SAFETY] Пауза между прокрутками: {scan_pause:.0f} с '
              '(старое значение конфигурации ограничено снизу)')
    max_scrolls = min(cfg['scan_scrolls_per_group'], cfg['scan_scroll_cap'])
    for turn in range(max_scrolls + 1):
        engine.ensure_ok(page)
        content = page.evaluate(DISCOVER_DATED_JS, {'groupId':gid})
        # Fallback detects IDs when VK has not yet rendered a footer date.
        root_ids = page.evaluate(engine.DISCOVER_JS, {'groupId':gid})['ids']
        ids = sorted(set(root_ids) | {int(item['postId']) for item in content})
        previous = len(discovered)
        discovered.update(ids)
        new_total += engine.add_posts(db,gid,ids)
        now = datetime.now(timezone.utc)
        for item in content:
            date_text = item.get('dateText','')
            iso_text = item.get('exactTime','')
            timestamp = approximate_post_date(date_text, iso_text, now=now)
            if timestamp is not None:
                # ISO timestamps with an explicit timezone, scoped to the
                # exact date link, are authoritative even in a group feed.
                aware_iso = False
                if iso_text:
                    try:
                        dt = datetime.fromisoformat(iso_text.replace('Z','+00:00'))
                        aware_iso = dt.tzinfo is not None
                    except ValueError:
                        pass
                save_date(db,gid,int(item['postId']),timestamp,
                          'feed-dom-iso' if aware_iso else 'feed-approx',aware_iso)
        empty = empty+1 if len(discovered) == previous else 0
        if turn >= max_scrolls or empty >= cfg['stale_scrolls']:
            break
        page.mouse.wheel(0,1800)
        page.wait_for_timeout(int(scan_pause*1000))
    print(f'   [FOUND] За проход={len(discovered)}, новые в SQLite={new_total}, прокруток={turn}')
    return len(discovered),new_total


def exact_post_date_info(page, gid: int, pid: int) -> dict:
    data = page.evaluate(EXACT_DATE_JS, {'groupId':gid,'postId':pid})
    if not isinstance(data, dict) or type(data.get('publishedAt')) not in (int,type(None)):
        return {'publishedAt':None, 'source':'date-unknown', 'exact':False}
    return data


def exact_post_date(page, gid: int, pid: int) -> int | None:
    return exact_post_date_info(page,gid,pid).get('publishedAt')


def candidate_rows(db, groups: list[dict], days: int, *, include_unknown: bool, include_older: bool=False,
                   for_audit: bool=False, limit: int) -> list[sqlite3.Row]:
    """Select across groups; known recent first, then round-robin unknown for audit."""
    ids = [g['id'] for g in groups]
    if not ids:
        return []
    placeholders = ','.join('?' for _ in ids)
    cutoff = int((datetime.now(timezone.utc)-timedelta(days=days)).timestamp())
    one_day_ago=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds')
    rows = db.execute(f'''
      SELECT p.group_id,p.post_id,p.like_status,p.repost_status,
             d.published_at,d.exact,d.source
      FROM posts p
      LEFT JOIN post_access x ON x.group_id=p.group_id AND x.post_id=p.post_id
      LEFT JOIN post_publication d ON d.group_id=p.group_id AND d.post_id=p.post_id
      LEFT JOIN post_audit a ON a.group_id=p.group_id AND a.post_id=p.post_id
      WHERE p.group_id IN ({placeholders})
        AND (x.post_id IS NULL OR (?=1 AND x.state='unresolved' AND x.checked_at < ?))
        AND (p.like_status='pending' OR p.repost_status='pending')
        AND p.like_status NOT IN ('uncertain','error')
        AND p.repost_status NOT IN ('uncertain','error')
        AND (d.published_at >= ? OR (?=1 AND d.published_at IS NULL) OR (?=1 AND d.published_at IS NOT NULL))
        AND (?=0 OR a.post_id IS NULL OR a.checked_at < ?)
        AND (d.source IS NULL OR d.source!='date-unknown' OR d.checked_at < ?)
        AND (?=0 OR d.exact_attempt_at IS NULL OR d.exact_attempt_at < ?)
    ''', (*ids,int(for_audit),one_day_ago,cutoff,int(include_unknown),int(include_older),int(for_audit),one_day_ago,one_day_ago,int(for_audit),one_day_ago)).fetchall()
    recent = sorted((r for r in rows if r['published_at'] is not None and r['published_at']>=cutoff),
                    key=lambda r:(r['published_at'],r['post_id']),reverse=True)
    older = sorted((r for r in rows if r['published_at'] is not None and r['published_at']<cutoff),
                   key=lambda r:(r['published_at'],r['post_id']),reverse=True) if include_older else []
    known = recent + older  # recent is ALWAYS processed ahead of historical backlog
    if not include_unknown:
        return known[:limit]
    by_group=defaultdict(list)
    for r in rows:
        if r['published_at'] is None:
            by_group[r['group_id']].append(r)
    for g in by_group:
        by_group[g].sort(key=lambda r:r['post_id'],reverse=True)
    ordered = list(known)
    queues = {g['id']:deque(by_group.get(g['id'],[])) for g in groups}
    while any(queues.values()) and len(ordered)<limit:
        for g in groups:
            if queues[g['id']]:
                ordered.append(queues[g['id']].popleft())
                if len(ordered)>=limit:
                    break
    return ordered[:limit]


def _date_recent(published: int | None, days: int) -> bool:
    if published is None:
        return False
    now = datetime.now(timezone.utc).timestamp()
    return now-days*86400 <= published <= now+3600


def date_allowed(published: int | None, days: int, recent_only: bool) -> bool:
    """Always require an unambiguous exact date. Older posts are opt-in."""
    if published is None:
        return False
    if recent_only:
        return _date_recent(published, days)
    now = datetime.now(timezone.utc).timestamp()
    return 1200000000 <= published <= now + 3600


def apply_actual_date(db, gid: int, pid: int, page) -> int | None:
    data = exact_post_date_info(page,gid,pid)
    published = data.get('publishedAt')
    if published is not None:
        save_date(db,gid,pid,published,data.get('source','vk-post-json'),True,date_checked=True)
        return published
    if data.get('source') == 'date-conflict':
        save_date(db,gid,pid,None,'date-conflict',False,date_checked=True)
        return None
    # UI text/hover never authorizes a real click. It only improves sorting
    # and gives the owner an understandable date diagnostic.
    hint = read_post_date_hint(page,gid,pid)
    approximate, source = approximate_from_date_hint(hint)
    save_date(db,gid,pid,approximate,source,False,date_checked=True)
    return None


def inspect_one(page, db, group: dict, row, days: int, recent_only: bool=True):
    gid,pid=group['id'],row['post_id']
    engine.go_to_post(page,gid,pid)
    published = apply_actual_date(db,gid,pid,page)
    if not date_allowed(published,days,recent_only):
        print(f'   [SKIP DATE] {gid}_{pid}: '+('точная дата не найдена' if published is None else 'старше окна'))
        return 'date-skipped'
    state=engine.refresh_state(page,gid,pid)
    if state['source'] in ('none','conflict') or type(state['liked']) is not bool or type(state['reposted']) is not bool:
        raise engine.StopRun(f'Аудит {gid}_{pid}: неоднозначные состояния VK')
    footer=engine.wait_post_footer_info(page,gid,pid,'like')
    if footer['liked'] is not state['liked']:
        raise engine.StopRun(f'Аудит {gid}_{pid}: основной лайк не совпадает с VK JSON')
    for action, flag in (('like','liked'),('repost','reposted')):
        if row[f'{action}_status'] in engine.SUCCESS and not state[flag]:
            raise engine.StopRun(f'Аудит {gid}_{pid}: состояние {action} изменилось вопреки SQLite')
    engine.ensure_ok(page)
    # An earlier unresolved markup observation is superseded only after
    # positively identifying the same post and matching state + footer.
    engine.clear_post_access(db,gid,pid)
    engine.save_audit(db,gid,pid,state['liked'],state['reposted'],state['source'])
    if row['like_status']=='pending' and state['liked']:
        engine.status_write(db,gid,pid,'like','already','Аудит нескольких групп без клика')
    if row['repost_status']=='pending' and state['reposted']:
        engine.status_write(db,gid,pid,'repost','already','Аудит нескольких групп без клика')
    print(f'   [AUDIT] {gid}_{pid}: like={state["liked"]}, repost={state["reposted"]}')
    return 'checked'


class MultiBudget:
    """Profile-wide attempts across communities, with separate action budgets."""
    def __init__(self, db, limits: dict):
        self.db,self.limits=db,limits
        self.used=Counter()
        locked=engine.safety_lock_state(db,0)
        if locked:
            raise engine.StopRun(f'После предупреждения VK действует блокировка: {locked["reason"]}. '
                                 'Проверь аккаунт и используй vk_batch.py --ack-warning только после устранения причины.')

    def past_count(self, action: str | None=None) -> int:
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec='seconds')
        if action is None:
            return self.db.execute('SELECT COUNT(*) FROM action_attempts WHERE attempted_at>=?',(cutoff,)).fetchone()[0]
        return self.db.execute('SELECT COUNT(*) FROM action_attempts WHERE action=? AND attempted_at>=?',(action,cutoff)).fetchone()[0]

    def available_actions(self, configured: set[str]) -> set[str]:
        if self.used['total']>=self.limits['actions_per_run'] or self.past_count()>=self.limits['actions_per_24h']:
            return set()
        output=set()
        for action in configured:
            if (self.used[action] < self.limits[action+'s_per_run'] and
                self.past_count(action) < self.limits[action+'s_per_24h']):
                output.add(action)
        return output

    def reserve(self, post_id: int, action: str) -> None:
        # Called by the proven single-post engine immediately before click.
        if action not in ('like','repost') or action not in self.available_actions({action}):
            raise engine.SafetyLimitReached(f'Защитный лимит исчерпан для {action} или всех действий')
        gid=self.current_group_id
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (gid,post_id,action,engine.moment()))
        self.db.commit()
        self.used['total']+=1
        self.used[action]+=1


def print_multigroup_report(db, groups: list[dict], days: int) -> None:
    cutoff=int((datetime.now(timezone.utc)-timedelta(days=days)).timestamp())
    ids=[g['id'] for g in groups]
    if not ids:
        print('[REPORT] Нет включённых сообществ')
        return
    placeholders=','.join('?' for _ in ids)
    found=db.execute(f'SELECT COUNT(*) FROM posts WHERE group_id IN ({placeholders})',ids).fetchone()[0]
    fresh=db.execute(f'''SELECT COUNT(*) FROM post_publication
         WHERE group_id IN ({placeholders}) AND published_at>=?''',(*ids,cutoff)).fetchone()[0]
    exact=db.execute(f'''SELECT COUNT(*) FROM post_publication
         WHERE group_id IN ({placeholders}) AND published_at>=? AND exact=1''',(*ids,cutoff)).fetchone()[0]
    print(f'[REPORT] Сообществ={len(ids)}, всего ID в SQLite={found}, даты <= {days} суток: {fresh}, точных дат: {exact}')
    stats=db.execute(f'''SELECT like_status,repost_status,COUNT(*) AS n FROM posts
        WHERE group_id IN ({placeholders}) GROUP BY like_status,repost_status''',ids).fetchall()
    for r in stats:
        print(f'   like={r["like_status"]}, repost={r["repost_status"]}: {r["n"]}')
    counts=db.execute(f'''SELECT state, COUNT(*) AS n FROM post_access
         WHERE group_id IN ({placeholders}) GROUP BY state ORDER BY state''', ids).fetchall()
    if counts:
        print('[ACCESS] Пропуски по типам: ' +
              ', '.join(f'{r["state"]}={r["n"]}' for r in counts))
    else:
        print('[ACCESS] Нет пропусков в выбранных сообществах')
    print('[REPORT] Действия ограничиваются одним аккаунтом')
    engine.safety_report(db,0)


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description='VK Batch: 57 сообществ, один аккаунт, приоритет новых записей')
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--scan',action='store_true',help='Обнаружить посты выбранных сообществ; никаких действий')
    modes.add_argument('--audit',action='store_true',help='Проверить недавние посты и уже существующие действия; без кликов')
    modes.add_argument('--execute',action='store_true',help='Выполнить недостающие действия в пределах лимитов')
    modes.add_argument('--report',action='store_true',help='Показать локальную сводку, не открывая VK')
    modes.add_argument('--preview',action='store_true',help='Показать следующие публикации по приоритету, без VK и действий')
    modes.add_argument('--login-only',action='store_true',help='Ручной вход в VK без сканирования')
    modes.add_argument('--date-check',metavar='GROUPID_POSTID',help='Проверить точную дату одного поста; без действий')
    parser.add_argument('--only',help='ID групп через запятую, например 188206921,198577444')
    parser.add_argument('--max-groups',type=int,default=0,help='Ограничить число групп в сканировании/аудите; 0 = все включённые')
    parser.add_argument('--max-posts',type=int,default=None,help='Размер партии аудита/исполнения; аудит дополнительно ограничен конфигурацией')
    parser.add_argument('--read-pause',type=float,default=None,
                        help='Минимальный интервал между открытиями страниц VK (5..300 секунд)')
    parser.add_argument('--config',type=Path,default=CONFIG_PATH,help='Путь к vk_groups.json')
    args=parser.parse_args(argv)
    if args.max_groups<0 or (args.max_posts is not None and args.max_posts<1):
        parser.error('Числовые ограничения должны быть положительными (кроме --max-groups 0)')
    try:
        cfg=load_config(args.config)
        if args.read_pause is not None:
            if not 5 <= args.read_pause <= 300:
                parser.error('--read-pause должен быть в диапазоне 5..300 секунд')
            cfg['read_pause_seconds'] = args.read_pause
        groups=selected_groups(cfg,args.only,args.max_groups)
        if not groups:
            raise ValueError('Нет активных групп для запуска')
    except (ValueError,KeyError,FileNotFoundError,json.JSONDecodeError) as exc:
        parser.error(f'Ошибка конфигурации: {exc}')
    check_identity=None
    if args.date_check:
        match=re.fullmatch(r'(\d+)_(\d+)',args.date_check)
        if not match or int(match.group(1)) not in {g['id'] for g in groups}:
            parser.error('--date-check: укажи существующую группу из конфига, например 188206921_13376')
        check_identity=(int(match.group(1)),int(match.group(2)))
    days=cfg['lookback_days']
    limit=args.max_posts or cfg['limits']['posts_per_run']
    if args.audit and limit > cfg['audit_posts_per_run']:
        print(f'[READ CAP] Аудит ограничен {cfg["audit_posts_per_run"]} публикациями за запуск '
              f'(запрошено {limit}). Это локальный предохранитель, не квота VK.')
        limit = cfg['audit_posts_per_run']
    if args.report or args.preview:
        with closing(engine.db_open(engine.DB_PATH)) as db:
            init_dates(db)
            if args.report:
                print_multigroup_report(db,groups,days)
            else:
                upcoming=candidate_rows(db,groups,days,include_unknown=False,
                          include_older=not cfg['recent_only'],limit=limit)
                print(f'[PREVIEW] Следующих публикаций: {len(upcoming)} (приоритет {days} суток)')
                labels={g['id']:g['name'] for g in groups}
                for row in upcoming:
                    label=datetime.fromtimestamp(row['published_at'],tz=timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
                    print(f'   {label} | {labels[row["group_id"]][:45]}')
                    print(f'   https://vk.com/wall-{row["group_id"]}_{row["post_id"]} '
                          f'| лайк={row["like_status"]}, репост={row["repost_status"]}, дата={row["source"]}'
                          + ('' if row['exact'] else ' (приблизительно, без подтверждённого timestamp)'))
                if not upcoming:
                    print('[PREVIEW] Сначала --scan; для неизвестных дат --audit.')
        return 0
    if args.login_only:
        engine.PROFILE.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            return engine.login_only(pw)
    mode=('scan' if args.scan else 'audit' if args.audit else 'date-check' if args.date_check else 'execute')
    print(f'VK MULTI v6.15 | групп={len(groups)}, окно={days} дней, режим={mode}')
    print('Один аккаунт. Лимиты не гарантируют отсутствие блокировки VK; обхода проверок нет.')
    print(f'[READ] Минимальный интервал между открытиями страниц: {cfg["read_pause_seconds"]:g} с; '
          f'потолок аудита: {cfg["audit_posts_per_run"]} поста(ов)/запуск; '
          f'пауза повторного сканирования группы: {cfg["scan_min_interval_hours"]:g} ч; '
          f'максимум прокруток стены: {cfg["scan_scroll_cap"]}')
    engine.PROFILE.mkdir(parents=True,exist_ok=True)
    try:
        with closing(engine.db_open(engine.DB_PATH)) as db, sync_playwright() as pw:
            init_dates(db)
            init_scan_progress(db)
            engine.require_platform_unlocked(db)
            budget=MultiBudget(db,cfg['limits']) if args.execute else None
            scan_targets = None
            if args.scan:
                scan_limit = min(len(groups), cfg['scan_groups_per_run'], cfg['scan_groups_cap'])
                scan_targets = choose_scan_groups(db, groups, scan_limit,
                          min_interval_hours=cfg['scan_min_interval_hours'])
                if not scan_targets:
                    print('[SCAN CACHE] Все выбранные сообщества недавно проверялись. '
                          'Нового сканирования и запросов VK не будет.')
                    print_multigroup_report(db, groups, days)
                    return 0
            if budget:
                print('[LIMITS] Остаток за 24ч: '+str({a:cfg['limits'][a+'s_per_24h']-budget.past_count(a) for a in ('like','repost')}))
            context=pw.chromium.launch_persistent_context(
                user_data_dir=str(engine.PROFILE),headless=False,locale='ru-RU',
                viewport={'width':1320,'height':900})
            try:
                page=context.pages[0] if context.pages else context.new_page()
                engine.watch_rate_limit_responses(page)
                engine.configure_navigation_pacing(page, cfg['read_pause_seconds'], db=db)
                if check_identity:
                    gid,pid=check_identity
                    engine.go_to_post(page,gid,pid)
                    data=exact_post_date_info(page,gid,pid)
                    published=data.get('publishedAt')
                    hint=read_post_date_hint(page,gid,pid,hover=True)
                    if hint.get('found'):
                        print(f'[DATE UI] Ссылка: {hint["text"]!r}')
                        for attribute in ('title','aria','datetime','tooltip'):
                            if hint.get(attribute):
                                print(f'[DATE UI] {attribute}: {hint[attribute]!r}')
                    if published is None:
                        print(f'[DATE CHECK] {gid}_{pid}: точный timestamp VK не найден '
                              f'({data.get("source", "date-unknown")}); действия запрещены.')
                        approx,source=approximate_from_date_hint(hint)
                        if approx is not None:
                            local=datetime.fromtimestamp(approx).astimezone().isoformat(timespec='minutes')
                            print(f'[DATE APPROX] {local} ({source}); для сортировки, НЕ точная дата.')
                    else:
                        iso=datetime.fromtimestamp(published,tz=timezone.utc).isoformat(timespec='seconds')
                        print(f'[DATE CHECK] {gid}_{pid}: {iso}; источник={data.get("source")}; '
                              f'за {days} дней: {_date_recent(published,days)}')
                    return 0
                if args.scan:
                    # Even --scan with all groups selected visits only a small
                    # bounded batch. Repeated runs rotate fairly via SQLite.
                    scan_limit=min(len(groups),cfg['scan_groups_per_run'],cfg['scan_groups_cap'])
                    completed=db.execute('SELECT COUNT(*) FROM group_scan_progress WHERE group_id IN ('+
                        ','.join('?' for _ in groups)+')',[g['id'] for g in groups]).fetchone()[0]
                    print(f'[SCAN PLAN] За запуск максимум {scan_limit} групп; ранее просканировано {completed}/{len(groups)}. '
                          'Следующий запуск продолжит обход.')
                    new_posts=0
                    for i,g in enumerate(scan_targets,1):
                        print(f'[{i}/{len(scan_targets)}]',end=' ',flush=True)
                        result=scan_group(page,db,g,cfg)
                        if result is None:
                            continue
                        discovered,n=result
                        mark_group_scanned(db,g['id'],discovered)
                        new_posts+=n
                    print(f'[DONE] Обнаружение завершено. Новых постов в SQLite: {new_posts}')
                    print_multigroup_report(db,groups,days)
                    return 0
                candidates=candidate_rows(db,groups,days,include_unknown=args.audit,include_older=not cfg['recent_only'],for_audit=args.audit,limit=limit)
                print(f'[QUEUE] Выбрано {len(candidates)} публикаций; порядок: новые по дате, неизвестные только для аудита.')
                if not candidates:
                    print('[NOTE] Сначала запусти --scan, а при неизвестных датах --audit')
                    return 0
                group_by_id={g['id']:g for g in groups}
                consecutive_temporary=0
                for i,row in enumerate(candidates,1):
                    gid,pid=row['group_id'],row['post_id']
                    group=group_by_id[gid]
                    print(f'[{i}/{len(candidates)}] https://vk.com/wall-{gid}_{pid}')
                    if args.execute:
                        types={a for a in ('like','repost') if cfg['actions'][a] and group[a+'_enabled']}
                        types=budget.available_actions(types)
                        if types and not any(row[a+'_status']=='pending' for a in types):
                            print('   [SKIP] Действия, разрешённые лимитом, для этого поста уже учтены.')
                            continue
                        if not types:
                            print('[SAFETY PAUSE] Лимит действий исчерпан. Очередь остаётся в SQLite.')
                            break
                    try:
                        if args.audit:
                            inspect_one(page,db,group,row,days,cfg['recent_only'])
                        else:
                            # Exact post date is required; cached feed-relative times
                            # are only used for sorting, never to permit a click.
                            engine.go_to_post(page,gid,pid)
                            published=apply_actual_date(db,gid,pid,page)
                            if not date_allowed(published,days,cfg['recent_only']):
                                print('   [SKIP DATE] Запись старше окна или точная дата недоступна')
                                continue
                            budget.current_group_id=gid
                            engine.process_one(page,db,gid,row,cfg['action_pause_seconds'],
                                               safety=budget,actions=types,already_open=True)
                    except engine.PostMarkupUnknown as exc:
                        # Only read-only audit can defer unknown markup; actual
                        # actions still stop rather than risk a wrong post.
                        if not args.audit:
                            raise
                        engine.record_post_access(db,gid,pid,'unresolved',str(exc))
                        print(f'   [REVIEW] VK не отрисовал привычный контейнер поста; '
                              'не считаем запись скрытой и продолжаем аудит.')
                        consecutive_temporary=0
                        continue
                    except engine.PostUnavailable as exc:
                        engine.record_post_access(db,gid,pid,exc.kind,str(exc))
                        print(f'   [SKIP] {exc.kind}: {exc}')
                        consecutive_temporary=0
                        continue
                    except engine.PostTemporaryError as exc:
                        engine.record_post_access(db,gid,pid,'temporary',str(exc))
                        print(f'   [SKIP] Временная ошибка: {exc}')
                        consecutive_temporary+=1
                        if consecutive_temporary>=engine.MAX_CONSECUTIVE_TEMPORARY:
                            raise engine.StopRun('Два последовательных сбоя VK; остановка.') from exc
                        continue
                    consecutive_temporary=0
                print('[DONE] Режим завершён. Просмотри сводку перед следующей партией.')
                print_multigroup_report(db,groups,days)
                return 0
            finally:
                context.close()
    except engine.PlatformRateLimited as exc:
        with closing(engine.db_open(engine.DB_PATH)) as db:
            engine.lock_on_platform_warning(db,0,str(exc))
        print('[RATE LIMIT]',exc)
        print('[SAFETY] Ограничение VK сохранено в SQLite. '              'Остановлены сканирование, аудит и действия по всем группам. '              'Проверь восстановление доступа вручную; потом --ack-warning.')
        return 2
    except engine.SafetyLimitReached as exc:
        print('[SAFETY PAUSE]',exc)
        return 0
    except engine.StopRun as exc:
        reason=str(exc)
        if any(k in reason.lower() for k in
              ('vk запросил вход','vk сообщил об ограничении','появилась captcha',
               'подтвердите, что вы не робот','подозрительная активность')):
            with closing(engine.db_open(engine.DB_PATH)) as db:
                engine.lock_on_platform_warning(db,0,reason)
            print('[SAFETY] Предупреждение VK сохранено. Исполнение заблокировано до проверки аккаунта.')
        print('[STOP]',exc)
        return 2
    except (PWTimeout,PlaywrightError) as exc:
        print('[ERROR] VK/Playwright:',str(exc)[:350])
        return 2
    except KeyboardInterrupt:
        print('[STOP] Прервано Ctrl+C, данные SQLite сохранены.')
        return 130


if __name__=='__main__':
    sys.exit(main())
