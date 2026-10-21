#!/usr/bin/env python3
"""Single-account VK wall history helper.

Scans the public group wall; on --execute runs the selected account's actions
on already-discovered posts, skipping previously liked/shared posts. The script
never attempts to bypass platform restrictions, CAPTCHA, or verification.

The main-post footer is selected by explicit VK test IDs, not by labels from comments.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PWTimeout, sync_playwright

from vk_test import (
    PROFILE, ALLOWED_HOSTS, PUBLISH_CONTROLS_JS, SHARE_DIALOG_JS,
)

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "vk_batch.sqlite3"
POST_URL = "https://vk.com/wall-{group_id}_{post_id}"
SUCCESS = {"already", "verified", "unavailable"}
FINISHED = SUCCESS | {"uncertain", "error"}
ACCESS_STATES = {"hidden", "deleted", "missing", "temporary", "unresolved"}
MAX_CONSECUTIVE_TEMPORARY = 2
DEFAULT_ACTIONS_PER_RUN = 2
DEFAULT_ACTIONS_PER_24H = 6

# Safety-only observer: record just that VK returned HTTP 429, never URLs,
# cookies, headers or responses. Stop rather than retry a rejected request.
_RATE_LIMIT_RESPONSES = WeakKeyDictionary()
# Human-visible pacing for ordinary page navigation, independent of the
# separate action budget. It is not a way to work around HTTP 429.
_NAVIGATION_PACING = WeakKeyDictionary()


def configure_navigation_pacing(page, interval_seconds: float, db=None) -> None:
    """Pace navigations in the session AND across runs when db is supplied.

    All VK 429/CAPTCHA conditions remain fatal. This is conservative traffic
    reduction, not a VK-approved rate or a mechanism for continuing past 429.
    """
    if not 5 <= interval_seconds <= 300:
        raise ValueError('Пауза чтения должна быть от 5 до 300 секунд')
    if db is not None:
        db.execute("""CREATE TABLE IF NOT EXISTS read_navigation_pacing (
            id INTEGER PRIMARY KEY CHECK(id=0), last_navigation_at REAL NOT NULL
        )""")
        db.commit()
    _NAVIGATION_PACING[page] = {'interval': float(interval_seconds),
                                'last_started': None, 'db': db}


def pace_navigation(page) -> None:
    """Wait before a new page load and record the ATTEMPT before requesting VK."""
    state = _NAVIGATION_PACING.get(page)
    if state is None:
        return
    db = state['db']
    ensure_ok(page)  # Do not wait/retry if VK already rejected the prior page.
    now = time.time() if db is not None else time.monotonic()
    last = state['last_started']
    if db is not None:
        require_platform_unlocked(db)
        row = db.execute('SELECT last_navigation_at FROM read_navigation_pacing WHERE id=0').fetchone()
        if row is not None:
            last = row['last_navigation_at']
    if last is not None:
        remaining = state['interval'] - (now - last)
        if remaining > 0:
            print(f'[READ PAUSE] {remaining:.1f} с перед следующим открытием страницы VK')
            page.wait_for_timeout(int(remaining * 1000) + 1)
    # A previous page may have received HTTP 429 while the next navigation
    # was waiting. Check it BEFORE any further request.
    ensure_ok(page)
    started_at = time.time() if db is not None else time.monotonic()
    if db is not None:
        require_platform_unlocked(db)
        db.execute("""INSERT INTO read_navigation_pacing(id,last_navigation_at)
             VALUES(0,?) ON CONFLICT(id) DO UPDATE SET
             last_navigation_at=excluded.last_navigation_at""", (started_at,))
        db.commit()
    state['last_started'] = started_at


def watch_rate_limit_responses(page) -> None:
    observed = {'limited': False}

    def on_response(response) -> None:
        try:
            if response.status == 429 and urlsplit(response.url).hostname in ALLOWED_HOSTS:
                observed['limited'] = True
        except (AttributeError, ValueError):
            pass  # only a best-effort supplemental signal; the DOM guard remains

    _RATE_LIMIT_RESPONSES[page] = observed
    page.on('response', on_response)


def require_platform_unlocked(db) -> None:
    """Refuse further web requests after a known platform limit.

    The operator may clear the latch only after manually confirming that VK
    works normally again. This applies equally to read-only audit and actions.
    """
    lock = safety_lock_state(db, 0)
    if lock is not None:
        raise StopRun('VK ограничил запросы или запросил проверку: блокировка с '
                      f'{lock["blocked_at"]}: {lock["reason"]}. '
                      'Не запускай сканирование/аудит до восстановления доступа. '
                      'После ручной проверки: py vk_batch.py --ack-warning')



def moment() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_group_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or url.hostname not in ALLOWED_HOSTS:
        raise ValueError("Ссылка сообщества должна вести на https://vk.com или https://vk.ru")
    if not url.path.startswith("/") or url.path in ("/", "/feed"):
        raise ValueError("Укажи ссылку на конкретное сообщество")
    return f"https://vk.com{url.path}"


def db_open(path: Path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            group_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            seen_at TEXT NOT NULL,
            like_status TEXT NOT NULL DEFAULT 'pending',
            repost_status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT,
            updated_at TEXT,
            PRIMARY KEY(group_id, post_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS post_audit (
            group_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            liked INTEGER NOT NULL,
            reposted INTEGER NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY(group_id, post_id),
            FOREIGN KEY(group_id, post_id) REFERENCES posts(group_id, post_id)
        )
    """)
    # Keep navigation/accessibility separate from like/repost status. A hidden
    # post is not a successful action and may become accessible in the future.
    db.execute("""
        CREATE TABLE IF NOT EXISTS post_access (
            group_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY(group_id, post_id),
            FOREIGN KEY(group_id, post_id) REFERENCES posts(group_id, post_id)
        )
    """)
    # Safety ledger: one record per attempted external action, saved BEFORE
    # the click. A crash or an uncertain result still consumes the budget.
    db.execute("""
        CREATE TABLE IF NOT EXISTS action_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('like', 'repost')),
            attempted_at TEXT NOT NULL
        )
    """)
    # Persistent circuit breaker: VK warnings survive script restarts.
    db.execute("""
        CREATE TABLE IF NOT EXISTS safety_lock (
            group_id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL,
            blocked_at TEXT NOT NULL
        )
    """)
    db.commit()
    return db


def actions_24h(db, group_id: int) -> int:
    """Profile-wide total across all source communities (one DB = one account)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds')
    return db.execute('SELECT COUNT(*) FROM action_attempts WHERE attempted_at>=?',
                      (cutoff,)).fetchone()[0]


def safety_lock_state(db, group_id: int):
    # group_id=0 reserves the account-wide latch for this local browser profile.
    return db.execute('SELECT reason,blocked_at FROM safety_lock WHERE group_id=0').fetchone()


def safety_report(db, group_id: int) -> None:
    print(f'[SAFETY] Попыток реальных действий за последние 24 ч: {actions_24h(db, group_id)}')
    blocked = safety_lock_state(db, group_id)
    if blocked:
        print(f'[SAFETY] Выполнение ЗАБЛОКИРОВАНО с {blocked["blocked_at"]}: {blocked["reason"]}')
        print('[SAFETY] После ручной проверки состояния аккаунта: --ack-warning')
    else:
        print('[SAFETY] Предупреждений VK, требующих ручной проверки: нет')


def lock_on_platform_warning(db, group_id: int, reason: str) -> None:
    db.execute('INSERT INTO safety_lock(group_id,reason,blocked_at) VALUES(?,?,?) '
               'ON CONFLICT(group_id) DO UPDATE SET reason=excluded.reason,blocked_at=excluded.blocked_at',
               (0, reason[:200], moment()))
    db.commit()


class SafetyBudget:
    """Protective resource caps, NOT a VK-approved or ban-proof threshold."""
    def __init__(self, db, group_id: int, per_run: int, per_24h: int):
        if not (per_run > 0 and per_24h > 0):
            raise ValueError('Значения защитных лимитов должны быть > 0')
        self.db, self.group_id = db, group_id
        self.per_run, self.per_24h = per_run, per_24h
        self.used_run = 0
        blocked = safety_lock_state(db, group_id)
        if blocked:
            raise StopRun('После предупреждения VK активна защитная блокировка: '
                          f'{blocked["reason"]}. Сначала вручную проверь аккаунт.')

    def reserve(self, post_id: int, action: str) -> None:
        if self.used_run >= self.per_run:
            raise SafetyLimitReached(f'Защитный лимит за запуск: {self.per_run} попыток действий. '
                          'Очередь сохранена; продолжение только новым запуском.')
        used_24h = actions_24h(self.db, self.group_id)
        if used_24h >= self.per_24h:
            raise SafetyLimitReached(f'Защитный лимит за 24 часа: {self.per_24h} попыток действий. '
                          'Не увеличивай его ради обхода ограничений VK.')
        if action not in ('like', 'repost'):
            raise ValueError('Неизвестное действие')
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) '
                        'VALUES(?,?,?,?)', (self.group_id, post_id, action, moment()))
        self.db.commit()
        self.used_run += 1


def add_posts(db, group_id: int, post_ids: list[int]) -> int:
    old = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO posts(group_id, post_id, seen_at) VALUES(?, ?, ?)",
        [(group_id, pid, moment()) for pid in post_ids],
    )
    db.commit()
    return db.total_changes - old


def get_tasks(db, group_id: int, limit: int) -> list[sqlite3.Row]:
    rows = db.execute("""
        SELECT p.post_id, p.like_status, p.repost_status FROM posts p
        LEFT JOIN post_access x ON x.group_id=p.group_id AND x.post_id=p.post_id
        WHERE p.group_id=? AND x.post_id IS NULL
          AND (p.like_status='pending' OR p.repost_status='pending')
        ORDER BY p.post_id DESC
    """, (group_id,))
    if limit:
        return rows.fetchmany(limit)
    return rows.fetchall()


def get_audit_tasks(db, group_id: int, limit: int) -> list[sqlite3.Row]:
    """Pending-only audit selection. Never retry uncertain or error states."""
    rows = db.execute("""
        SELECT p.post_id, p.like_status, p.repost_status FROM posts p
        LEFT JOIN post_audit a ON a.group_id=p.group_id AND a.post_id=p.post_id
        LEFT JOIN post_access x ON x.group_id=p.group_id AND x.post_id=p.post_id
        WHERE p.group_id=?
          AND x.post_id IS NULL
          AND (p.like_status='pending' OR p.repost_status='pending')
          AND p.like_status NOT IN ('uncertain', 'error')
          AND p.repost_status NOT IN ('uncertain', 'error')
        ORDER BY CASE WHEN a.post_id IS NULL THEN 0 ELSE 1 END,
                 p.post_id DESC
    """, (group_id,))
    return rows.fetchmany(limit) if limit else rows.fetchall()


def get_audited_missing_tasks(db, group_id: int, limit: int) -> list[sqlite3.Row]:
    """Select only inspected posts with still-pending actions known absent.

    process_one will always re-read VK immediately before any click, so cached
    data is never used as permission to toggle a reaction.
    """
    cursor = db.execute("""
        SELECT p.post_id,p.like_status,p.repost_status
        FROM posts p JOIN post_audit a ON a.group_id=p.group_id AND a.post_id=p.post_id
        LEFT JOIN post_access x ON x.group_id=p.group_id AND x.post_id=p.post_id
        WHERE p.group_id=?
          AND x.post_id IS NULL
          AND ((p.like_status='pending' AND a.liked=0) OR
               (p.repost_status='pending' AND a.reposted=0))
          AND p.like_status NOT IN ('uncertain','error')
          AND p.repost_status NOT IN ('uncertain','error')
        ORDER BY a.checked_at DESC, p.post_id DESC
    """, (group_id,))
    return cursor.fetchmany(limit) if limit else cursor.fetchall()


def save_audit(db, group_id: int, post_id: int, liked: bool, reposted: bool,
               source: str) -> None:
    """Cache status only. Do not store tokens, cookies, profile, or HTML."""
    if type(liked) is not bool or type(reposted) is not bool:
        raise ValueError('Audit requires two verified booleans')
    db.execute("""
        INSERT INTO post_audit(group_id,post_id,checked_at,liked,reposted,source)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(group_id,post_id) DO UPDATE SET
            checked_at=excluded.checked_at,
            liked=excluded.liked,
            reposted=excluded.reposted,
            source=excluded.source
    """, (group_id, post_id, moment(), int(liked), int(reposted), source[:20]))
    db.commit()


def record_post_access(db, group_id: int, post_id: int, state: str, reason: str) -> None:
    """Record only the reason for a no-click skip; never touch action flags."""
    if state not in ACCESS_STATES:
        raise ValueError('Invalid post access state')
    db.execute("""
        INSERT INTO post_access(group_id,post_id,state,reason,checked_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(group_id,post_id) DO UPDATE SET
            state=excluded.state,reason=excluded.reason,checked_at=excluded.checked_at
    """, (group_id, post_id, state, reason[:180], moment()))
    db.commit()


def clear_post_access(db, group_id: int, post_id: int) -> None:
    db.execute('DELETE FROM post_access WHERE group_id=? AND post_id=?',
               (group_id, post_id))
    db.commit()


def report_access(db, group_id: int, limit: int = 12) -> None:
    groups = db.execute("""
        SELECT state,COUNT(*) AS n FROM post_access
        WHERE group_id=? GROUP BY state ORDER BY state
    """, (group_id,)).fetchall()
    if not groups:
        print('[ACCESS] Пропущенных недоступных публикаций нет.')
        return
    print('[ACCESS] Пропущенные и неразобранные записи: ' +
          ', '.join(f'{r["state"]}={r["n"]}' for r in groups))
    for row in db.execute("""
        SELECT post_id,state,reason FROM post_access
        WHERE group_id=? ORDER BY checked_at DESC,post_id DESC LIMIT ?
    """, (group_id, limit)).fetchall():
        print(f'   {POST_URL.format(group_id=group_id,post_id=row["post_id"])} '
              f'— {row["state"]}: {row["reason"]}')


def audit_pending(page, db, group_id: int, limit: int, pause: float = 0.0) -> dict:
    """Inspect pending posts without pressing any reaction/repost button.

    Confirmed pre-existing actions may be marked 'already' in SQLite.
    Missing actions remain pending. Unknown/conflicting states stop the audit.
    """
    tasks = get_audit_tasks(db, group_id, limit)
    stats = {'inspected':0, 'like_missing':0, 'repost_missing':0,
             'both_missing':0, 'already_like':0, 'already_repost':0,
             'skipped_unavailable':0, 'skipped_temporary':0, 'skipped_unresolved':0}
    print(f'[AUDIT] К проверке: {len(tasks)}; никаких лайков или репостов.')
    consecutive_temporary = 0
    for index, row in enumerate(tasks, start=1):
        pid = row['post_id']
        try:
            go_to_post(page, group_id, pid)
        except PostUnavailable as exc:
            record_post_access(db, group_id, pid, exc.kind, str(exc))
            stats['skipped_unavailable'] += 1
            consecutive_temporary = 0
            print(f'[SKIP {index}/{len(tasks)}] {pid}: {exc.kind} — {exc}')
            continue
        except PostMarkupUnknown as exc:
            record_post_access(db, group_id, pid, 'unresolved', str(exc))
            stats['skipped_unresolved'] += 1
            consecutive_temporary = 0
            print(f'[REVIEW {index}/{len(tasks)}] {pid}: нестандартная разметка, без кликов')
            continue
        except PostTemporaryError as exc:
            record_post_access(db, group_id, pid, 'temporary', str(exc))
            stats['skipped_temporary'] += 1
            consecutive_temporary += 1
            print(f'[SKIP {index}/{len(tasks)}] {pid}: временная ошибка — {exc}')
            if consecutive_temporary >= MAX_CONSECUTIVE_TEMPORARY:
                raise StopRun('Два последовательных сбоя загрузки VK. '
                              'Останавливаем аудит, чтобы не помечать всё подряд.') from exc
            continue
        consecutive_temporary = 0
        state = refresh_state(page, group_id, pid)
        if state['source'] in ('none', 'conflict') or type(state['liked']) is not bool or type(state['reposted']) is not bool:
            raise StopRun(f'Аудит {pid}: VK не вернул два однозначных состояния, выполнение остановлено')
        footer = wait_post_footer_info(page, group_id, pid, 'like')
        if footer['liked'] is not state['liked']:
            raise StopRun(f'Аудит {pid}: кнопка основного поста и VK не согласованы')
        for action, flag in (('like', 'liked'), ('repost', 'reposted')):
            previous = row[f'{action}_status']
            if previous in SUCCESS and state[flag] is False:
                raise StopRun(f'Аудит {pid}: {action} был {previous} в SQLite, но сейчас VK показывает False')
        ensure_ok(page)
        save_audit(db, group_id, pid, state['liked'], state['reposted'], state['source'])
        if row['like_status'] == 'pending' and state['liked']:
            status_write(db, group_id, pid, 'like', 'already', 'Наблюдение без клика')
            stats['already_like'] += 1
        if row['repost_status'] == 'pending' and state['reposted']:
            status_write(db, group_id, pid, 'repost', 'already', 'Наблюдение без клика')
            stats['already_repost'] += 1
        needs_like = row['like_status'] == 'pending' and not state['liked']
        needs_repost = row['repost_status'] == 'pending' and not state['reposted']
        stats['like_missing'] += int(needs_like)
        stats['repost_missing'] += int(needs_repost)
        stats['both_missing'] += int(needs_like and needs_repost)
        stats['inspected'] += 1
        print(f'[AUDIT {index}/{len(tasks)}] {pid}: '
              f'like={"NEED" if needs_like else "OK"}, '
              f'repost={"NEED" if needs_repost else "OK"}; '
              f'VK={state["source"]}')
        # A fixed pause avoids back-to-back navigation while VK updates its UI.
        # It is not a fingerprinting or anti-detection technique.
        if index < len(tasks) and pause:
            page.wait_for_timeout(int(pause * 1000))
    print('[AUDIT] Завершено:', stats)
    return stats


def audit_report(db, group_id: int, limit: int = 20) -> None:
    total = db.execute('SELECT COUNT(*) FROM post_audit WHERE group_id=?',
                       (group_id,)).fetchone()[0]
    print(f'[AUDIT REPORT] Проверенных записей в локальном кэше: {total}')
    rows = db.execute("""
        SELECT a.post_id,a.checked_at,a.liked,a.reposted,p.like_status,p.repost_status
        FROM post_audit a
        JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
        LEFT JOIN post_access x ON x.group_id=a.group_id AND x.post_id=a.post_id
        WHERE a.group_id=? AND (
            (a.liked=0 AND p.like_status='pending') OR
            (a.reposted=0 AND p.repost_status='pending'))
          AND x.post_id IS NULL
        ORDER BY a.post_id DESC
        LIMIT ?
    """, (group_id, limit)).fetchall()
    total_missing = db.execute("""
        SELECT COUNT(*) FROM post_audit a
        JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
        LEFT JOIN post_access x ON x.group_id=a.group_id AND x.post_id=a.post_id
        WHERE a.group_id=? AND (
            (a.liked=0 AND p.like_status='pending') OR
            (a.reposted=0 AND p.repost_status='pending'))
          AND x.post_id IS NULL
    """, (group_id,)).fetchone()[0]
    print(f'[AUDIT REPORT] Кандидатов с отсутствующим действием (на момент проверки): {total_missing}')
    for row in rows:
        needed=[]
        if row['liked']==0 and row['like_status']=='pending': needed.append('лайк')
        if row['reposted']==0 and row['repost_status']=='pending': needed.append('репост')
        print(f'   {POST_URL.format(group_id=group_id, post_id=row["post_id"])} '
              f'— {" + ".join(needed)}; проверено {row["checked_at"]}')
    if total_missing > len(rows):
        print(f'   ... ещё {total_missing-len(rows)} уже проверенных кандидатов; вывод ограничен первыми {len(rows)}')
    print('[NOTE] Это снимок состояния: перед действием скрипт всё равно перечитывает VK.')


def target_row(db, group_id: int, post_id: int, *, create: bool = False):
    """Select exactly one explicit post without consuming the normal queue.

    Creating a row is permitted ONLY when the operator supplies --post-id and
    --execute. It does not affect already-discovered post IDs or their states.
    """
    if create:
        add_posts(db, group_id, [post_id])
    return db.execute(
        "SELECT post_id,like_status,repost_status FROM posts "
        "WHERE group_id=? AND post_id=?",
        (group_id, post_id),
    ).fetchone()


def status_write(db, group_id: int, post_id: int, action: str, state: str, detail: str = ""):
    if action not in ("like", "repost"):
        raise ValueError("Invalid action")
    if state not in FINISHED:
        raise ValueError("Invalid final state")
    db.execute(
        f"UPDATE posts SET {action}_status=?,detail=?,updated_at=? "
        "WHERE group_id=? AND post_id=?",
        (state, detail[:240], moment(), group_id, post_id),
    )
    db.commit()


# Only public IDs of posts from exactly the requested source are collected.
# No cookies, login information or network tokens are returned to Python.
DISCOVER_JS = r"""
({groupId}) => {
  const id = String(groupId);
  const ids = new Set();
  const byId = new RegExp('^post-' + id + '_(\\d+)$');
  const href = new RegExp('(?:^|/)wall-' + id + '_(\\d+)(?:$|[?/#])');
  for (const el of document.querySelectorAll('[id^="post-"]')) {
    const hit = byId.exec(el.id || '');
    if (hit) ids.add(Number(hit[1]));
  }
  for (const el of document.querySelectorAll('[data-post-id]')) {
    const raw = el.getAttribute('data-post-id') || '';
    const hit = /(-?\d+)_(\d+)/.exec(raw);
    if (hit && hit[1] === '-' + id) ids.add(Number(hit[2]));
  }
  // Some SPA versions render post permalinks before adding a root id.
  // Limit the fallback to the group page's primary content.
  const main = document.querySelector('[role="main"]') || document.querySelector('#page_body');
  if (main) {
    for (const link of main.querySelectorAll('a[href*="wall-"]')) {
      const hit = href.exec(link.getAttribute('href') || '');
      if (hit) ids.add(Number(hit[1]));
    }
  }
  const scrollRoot = document.scrollingElement || document.documentElement;
  return {ids:[...ids].filter(x => Number.isSafeInteger(x) && x > 0),
    scrollY:scrollRoot.scrollTop, height:scrollRoot.scrollHeight,
    viewport:scrollRoot.clientHeight};
}
"""


# Read current post flags without tokens or raw HTML leaving the browser.
# VK's window.cur.apiPrefetchCache can disappear after SPA hydration. The original
# inline JSON prefetch script normally remains in the DOM: parse its JSON inside
# the page (no eval), and return ONLY booleans for the exact requested post.
POST_STATE_JS = r"""
({groupId, postId}) => {
  const ownerId = -Number(groupId), id = Number(postId);
  const result = {liked:null, reposted:null, canLike:null, canRepost:null, source:'none'};
  const snapshots = [];

  const live = window.cur && window.cur.apiPrefetchCache;
  if (Array.isArray(live) && live.length) snapshots.push({entries:live, where:'live'});

  // Inline prefetch data is regular JSON in VK's initial document. It survives
  // even when the SPA clears window.cur.apiPrefetchCache. DO NOT return or print
  // raw script text: the same document may contain a session token elsewhere.
  for (const script of document.querySelectorAll('script:not([src])')) {
    const raw = script.textContent || '';
    const marker = /window\.cur\.apiPrefetchCache\s*=\s*/g;
    const start = marker.exec(raw);
    if (!start) continue;
    const after = raw.slice(marker.lastIndex);
    const trailer = /;\s*window\.apiPrefetchReadyResolve\s*\(\s*\)/.exec(after);
    if (!trailer) continue;
    const json = after.slice(0, trailer.index).trim();
    if (json.length < 2 || json.length > 8000000 || json[0] !== '[') continue;
    try {
      const entries = JSON.parse(json); // JSON only. Never eval or Function.
      if (Array.isArray(entries)) snapshots.push({entries, where:'inline'});
    } catch (_) { /* invalid SSR data; do not guess */ }
  }

  function read(entries) {
    const state = {liked:null, reposted:null, canLike:null, canRepost:null};
    for (const entry of entries) {
      if (entry?.method === 'wall.getById') {
        for (const post of entry?.response?.items || []) {
          if (Number(post?.owner_id) !== ownerId || Number(post?.id) !== id) continue;
          if (post.likes?.user_likes != null) state.liked = Boolean(post.likes.user_likes);
          if (post.reposts?.user_reposted != null) state.reposted = Boolean(post.reposts.user_reposted);
          if (post.likes?.can_like != null) state.canLike = Boolean(post.likes.can_like);
          if (post.likes?.repost_disabled != null) state.canRepost = !Boolean(post.likes.repost_disabled);
        }
      }
      if (entry?.method === 'likes.isLiked' &&
          Number(entry?.request?.owner_id) === ownerId &&
          Number(entry?.request?.item_id) === id && entry?.request?.type === 'post') {
        if (entry.response?.liked != null && state.liked === null)
          state.liked = Boolean(entry.response.liked);
        if (entry.response?.copied != null && state.reposted === null)
          state.reposted = Boolean(entry.response.copied);
      }
    }
    return state;
  }

  let selected = false;
  for (const snapshot of snapshots) {
    const value = read(snapshot.entries);
    if (value.liked === null && value.reposted === null) continue;
    if (!selected) {
      Object.assign(result, value);
      result.source = snapshot.where;
      selected = true;
      continue;
    }
    // Fail closed when two sources disagree, rather than toggling a known like.
    for (const field of ['liked','reposted','canLike','canRepost']) {
      if (value[field] != null && result[field] != null && value[field] !== result[field]) {
        result[field] = null;
        result.source = 'conflict';
      } else if (result[field] === null && result.source !== 'conflict') {
        result[field] = value[field];
      }
    }
  }
  return result;
}
"""


PROTECTION_JS = r"""
() => {
  const url = location.href.toLowerCase();
  if (/(?:captcha|challenge|blocked|security|verify)/.test(url) ||
      /^(https?:\/\/)?(id|login)\.vk\./.test(location.hostname))
    return 'VK запросил вход/дополнительную проверку';
  const text = (document.body?.innerText || '').slice(0, 18000);
  const tooMany = /too many requests per second|too many requests|слишком много запросов(?: в секунду)?|rate limit exceeded/i;
  const loadFailed = /не удалось загрузить посты|не удалось загрузить публикации|failed to load posts|не удалось загрузить записи/i;
  // A readable rate-limit panel can be on a wall, not just an individual post.
  // Require the platform's load-error context, not a phrase inside a post.
  if (tooMany.test(text) && loadFailed.test(text))
    return 'VK_RATE_LIMITED: VK ограничил частоту запросов (Too many requests per second)';
  const warning = [
    /слишком много однотипных действий/i,
    /подозрительн(?:ая|ую) активност/i,
    /подтвердите, что вы не робот/i,
    /действие временно невозможно/i,
  ].find(pattern => pattern.test(text));
  if (warning) return 'VK сообщил об ограничении либо проверке';
  if (document.querySelector('[data-testid*="captcha"],input[name="captcha_key"]'))
    return 'Появилась CAPTCHA';
  return null;
}
"""


class StopRun(RuntimeError):
    """Halt safely on ambiguous actions, VK restrictions or unexpected markup."""


class SafetyLimitReached(StopRun):
    """A protective cap was reached before another click. Not a VK error."""


class PlatformRateLimited(StopRun):
    """VK rejected read/navigation traffic. Fatal for every mode, not a skip."""


class PostUnavailable(StopRun):
    """VK explicitly says a post is hidden, deleted, or missing (no click)."""

    def __init__(self, kind: str, message: str):
        if kind not in ('hidden', 'deleted', 'missing'):
            raise ValueError('Invalid inaccessible-post kind')
        super().__init__(message)
        self.kind = kind


class PostTemporaryError(StopRun):
    """Navigation failed BEFORE any click; one post can be safely skipped."""


class PostMarkupUnknown(StopRun):
    """Expected post root not rendered at its URL; this is NOT proof of deletion.

    In read-only audit mode the individual post may be deferred for manual
    diagnosis. In execution mode this remains a hard stop: no clicks are safe.
    """


def ensure_ok(page):
    signal = _RATE_LIMIT_RESPONSES.get(page)
    if signal is not None and signal['limited']:
        raise PlatformRateLimited('VK вернул HTTP 429 (ограничение частоты запросов). '
                                  'Сканирование и действия остановлены; не повторяй запросы.')
    warning = page.evaluate(PROTECTION_JS)
    if warning is not None and not isinstance(warning, str):
        raise StopRun('Неожиданный ответ проверки защитного состояния VK')
    if warning and warning.startswith('VK_RATE_LIMITED:'):
        raise PlatformRateLimited(warning.removeprefix('VK_RATE_LIMITED:').strip() +
                                  '. Сканирование и действия остановлены.')
    if warning:
        raise StopRun(warning + ". Автоматические действия остановлены.")


def refresh_state(page, group_id: int, post_id: int, attempts: int = 10) -> dict:
    for _ in range(attempts):
        ensure_ok(page)
        data = page.evaluate(POST_STATE_JS, {"groupId": group_id, "postId": post_id})
        if data["liked"] is not None and data["reposted"] is not None:
            return data
        page.wait_for_timeout(250)
    return data  # preserve a partial reading for safe per-action decisions


# The standalone wall page may render either #post-<owner>_<id> or
# [data-post-id="<owner>_<id>"]. The POC's FIND_CONTROLS accepts BOTH;
# v6.1's go_to_post incorrectly required only the ID variant.
# This selector is exact by post identity. Do not fall back to a nearby post.
POST_ROOT_READY_JS = r"""
({groupId, postId}) => {
  const postKey = '-' + Number(groupId) + '_' + Number(postId);
  const byId = document.getElementById('post' + postKey);
  const byData = [...document.querySelectorAll('[data-post-id]')]
    .find(el => el.getAttribute('data-post-id') === postKey) || null;
  const visible = el => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const css = getComputedStyle(el);
    return rect.width > 5 && rect.height > 5 && css.display !== 'none' &&
      css.visibility !== 'hidden' && css.opacity !== '0';
  };
  return visible(byId) || visible(byData);
}
"""


# This is a *page-state* probe, not a selector for buttons. On a VK error page,
# the expected post root never appears. Recognize only explicit messages in the
# main area, with no target post root present; ordinary text in a post must not
# make that post 'unavailable'. Return null while VK is still hydrating.
# No account/session details are returned to Python.
POST_PAGE_ACCESS_JS = r"""
({groupId, postId}) => {
  const key = '-' + Number(groupId) + '_' + Number(postId);
  const root = document.getElementById('post' + key) ||
    document.querySelector('[data-testid="post"][data-post-id="' + key + '"]');
  // Returning this kind immediately avoids treating VK's rate-limit panel
  // as missing post markup or retrying the same rejected navigation.
  const visibleText = (document.body?.innerText || '').slice(0, 10000);
  if (/too many requests per second|too many requests|слишком много запросов(?: в секунду)?|rate limit exceeded/i.test(visibleText) &&
      /не удалось загрузить посты|не удалось загрузить публикации|failed to load posts|не удалось загрузить записи/i.test(visibleText))
    return {kind:'rate_limited'};
  if (root) {
    const rect = root.getBoundingClientRect();
    const style = getComputedStyle(root);
    if (rect.width > 5 && rect.height > 5 && style.display !== 'none' &&
        style.visibility !== 'hidden') return {kind:'ready'};
    return null;
  }
  // Error text may be outside #page-single-post in different VK SPA builds.
  // Read only the user-visible main areas, not session data or script HTML.
  const areas = ['#page-single-post','[role="main"]','#spa_layout_content','#page_body']
    .map(selector => document.querySelector(selector)).filter(Boolean);
  for (const area of areas) {
    const text = (area.innerText || '').slice(0, 6000).trim();
    if (/пользователь предпоч[её]л скрыть эту страницу/i.test(text))
      return {kind:'hidden'};
    if (/эта (?:запись|публикация|страница) удалена|запись была удалена/i.test(text))
      return {kind:'deleted'};
    if (/такой записи не существует|публикация не найдена|запись не найдена|страница не найдена/i.test(text))
      return {kind:'missing'};
    if (/временно недоступн|попробуйте позже|ошибка загрузки|ошибка сервера/i.test(text))
      return {kind:'temporary'};
  }
  return null;
}
"""

POST_DIAG_JS = r"""
({groupId, postId}) => {
  const expected = '/wall-' + Number(groupId) + '_' + Number(postId);
  const postKey = '-' + Number(groupId) + '_' + Number(postId);
  const byId = document.getElementById('post' + postKey);
  const byData = [...document.querySelectorAll('[data-post-id]')]
    .find(el => el.getAttribute('data-post-id') === postKey) || null;
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 5 && r.height > 5 && cs.display !== 'none' &&
      cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  let matchingLinks = 0;
  for (const a of document.querySelectorAll('a[href*="wall-"]')) {
    try {
      if (new URL(a.href, location.href).pathname === expected) matchingLinks++;
    } catch (_) { /* bad hrefs do not change the diagnostic */ }
  }
  // Metadata only: never return innerHTML, page text, cookies or tokens.
  const posts = [...document.querySelectorAll('[data-testid="post"][data-post-id]')];
  const exactLinks = [...document.querySelectorAll('a[href*="wall-"]')]
    .filter(a => { try { return new URL(a.href, location.href).pathname === expected; }
                   catch (_) { return false; } });
  const article = exactLinks.map(a => a.closest('article')).find(Boolean) || null;
  const ancestor = exactLinks.length ? exactLinks[0].closest('[data-testid],article,section') : null;
  return {
    host: location.hostname, path: location.pathname,
    correctRoute: location.pathname === expected,
    documentState: document.readyState,
    idRoot: Boolean(byId), dataRoot: Boolean(byData),
    visibleRoot: visible(byId) || visible(byData),
    matchingLinks,
    skeletons: document.querySelectorAll('[data-post-skeleton], [data-testid="loading-skeleton"]').length,
    postCount: posts.length,
    postKeys: posts.slice(0, 5).map(el => (el.getAttribute('data-post-id') || '').slice(0, 40)),
    singlePostWrapper: Boolean(document.querySelector('#page-single-post')),
    articles: document.querySelectorAll('article').length,
    footerLikeCount: document.querySelectorAll('[data-testid="post_footer_action_like"]').length,
    footerShareCount: document.querySelectorAll('[data-testid="post_footer_action_share"]').length,
    permalinkArticle: Boolean(article),
    permalinkArticleLikes: article ? article.querySelectorAll('[data-testid="post_footer_action_like"]').length : 0,
    permalinkAncestorTag: ancestor?.tagName || '',
    permalinkAncestorTestId: (ancestor?.getAttribute('data-testid') || '').slice(0, 60),
  };
}
"""


def diagnose_post(page, group_id: int, post_id: int) -> dict:
    """Only public routing and selector diagnostics; never dump HTML/tokens."""
    data = page.evaluate(POST_DIAG_JS, {'groupId': group_id, 'postId': post_id})
    print('[DIAG] адрес:', f"{data['host']}{data['path']}")
    print('[DIAG] маршрут конкретного поста:', data['correctRoute'])
    print('[DIAG] DOM id / data-post-id / видимый контейнер:',
          data['idRoot'], data['dataRoot'], data['visibleRoot'])
    print('[DIAG] точные ссылки на пост / загрузочные заглушки:',
          data['matchingLinks'], data['skeletons'])
    print('[DIAG] document.readyState:', data['documentState'])
    print('[DIAG] post-root count / sample IDs:', data.get('postCount'), data.get('postKeys'))
    print('[DIAG] page-single-post / articles / like-buttons / share-buttons:',
          data.get('singlePostWrapper'), data.get('articles'),
          data.get('footerLikeCount'), data.get('footerShareCount'))
    print('[DIAG] exact permalink inside article / buttons in it / closest structure:',
          data.get('permalinkArticle'), data.get('permalinkArticleLikes'),
          data.get('permalinkAncestorTag'), data.get('permalinkAncestorTestId'))
    return data


def go_to_post(page, group_id: int, post_id: int):
    url = POST_URL.format(group_id=group_id, post_id=post_id)
    expected_path = f'/wall-{group_id}_{post_id}'
    for attempt in range(2):
        pace_navigation(page)
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except (PWTimeout, PlaywrightError) as exc:
            # If the navigation timeout followed a VK HTTP 429, never retry it.
            signal = _RATE_LIMIT_RESPONSES.get(page)
            if signal is not None and signal['limited']:
                raise PlatformRateLimited('VK вернул HTTP 429 при открытии публикации') from exc
            # Only navigation (before any interaction) may be retried. Never
            # retry a like or repost when its outcome is uncertain.
            if attempt == 0:
                page.wait_for_timeout(1200)
                continue
            raise PostTemporaryError('VK не удалось загрузить запись (сбой сети/навигации)') from exc
        ensure_ok(page)  # login, CAPTCHA and platform limits are ALWAYS fatal
        try:
            handle = page.wait_for_function(POST_PAGE_ACCESS_JS,
                arg={'groupId': group_id, 'postId': post_id}, timeout=16000 if attempt == 0 else 20000)
            result = handle.json_value()
        except PWTimeout as exc:
            ensure_ok(page)
            data = diagnose_post(page, group_id, post_id)
            # An incomplete SPA load may affect the whole session. It is not
            # evidence that a post is hidden/deleted.
            if data['correctRoute'] and data['skeletons']:
                if attempt == 0:
                    page.wait_for_timeout(1200)
                    continue
                raise PostTemporaryError('Страница VK не загрузилась (остались заглушки)') from exc
            msg = (f'Запись {url} не обнаружена в DOM. '
                   f'Нужный маршрут={data["correctRoute"]}, id={data["idRoot"]}, '
                   f'data-post-id={data["dataRoot"]}, skeletons={data["skeletons"]}. '
                   'Причина неизвестна: действия заблокированы.')
            if data['correctRoute']:
                # The VK SPA sometimes shows a permalink with no post root.
                # A second READ-ONLY navigation can recover (seen with 1711/1624).
                # Retrying clicks is never permitted; this runs only before them.
                if attempt == 0:
                    print('[RETRY] Правильный адрес, но пост не появился в DOM; повторное открытие без кликов.')
                    page.wait_for_timeout(750)
                    continue
                # A genuinely changed markup variant must not be guessed.
                raise PostMarkupUnknown(msg) from exc
            raise StopRun(msg) from exc
        ensure_ok(page)
        current = urlsplit(page.url)
        if current.hostname not in ALLOWED_HOSTS or current.path != expected_path:
            diagnose_post(page, group_id, post_id)
            raise StopRun('VK открыл другой маршрут; действия заблокированы.')
        kind = result.get('kind') if isinstance(result, dict) else None
        if kind == 'ready':
            return
        if kind == 'rate_limited':
            raise PlatformRateLimited('VK показал Too many requests per second. '
                                      'Прекращено чтение всех групп без повторов.')
        if kind in ('hidden', 'deleted', 'missing'):
            descriptions = {'hidden':'VK сообщает, что страница скрыта',
                            'deleted':'VK сообщает об удалённой записи',
                            'missing':'VK сообщает, что запись не найдена'}
            raise PostUnavailable(kind, descriptions[kind])
        if kind == 'temporary':
            if attempt == 0:
                page.wait_for_timeout(1200)
                continue
            raise PostTemporaryError('VK показывает временную ошибку страницы')
        raise StopRun('Неизвестное состояние страницы VK; никаких кликов.')


def inspect_post_only(page, group_id: int, post_id: int):
    """Read-only live diagnostic, useful when a scanned ID is not renderable."""
    url = POST_URL.format(group_id=group_id, post_id=post_id)
    print('[INSPECT] Открываю:', url)
    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    ensure_ok(page)
    # Give the VK SPA a short chance to render before reporting.
    try:
        page.wait_for_function(POST_ROOT_READY_JS,
            arg={'groupId': group_id, 'postId': post_id}, timeout=10000)
    except PWTimeout:
        print('[INSPECT] За 10 с точный контейнер не появился.')
    diagnose_post(page, group_id, post_id)
    state = refresh_state(page, group_id, post_id)
    print('[DIAG] состояния (без токенов):',
          'liked=', state['liked'], 'reposted=', state['reposted'], 'source=', state['source'])
    footer = post_footer_info(page, group_id, post_id, 'like')
    print('[DIAG] основная кнопка поста:',
          f'ok={footer["ok"]}', f'liked={footer["liked"]}',
          f'visible={footer["visibleLikes"]}', f'ariaHidden={footer["ariaHidden"]}',
          f'reason={footer["reason"]}')
    try:
        ensure_ok(page)
    except StopRun as exc:
        print('[INSPECT] Страница требует внимания:', exc)
    print('[INSPECT] Лайки и репосты НЕ выполнялись.')
    try:
        input('Посмотри запись в браузере; Enter — закрыть окно...')
    except EOFError:
        pass


# Main-post interaction guard. The v6.5 generic label search included comment buttons
# inside a post container. Main post "Like" MUST use its dedicated test id;
# the comment heart is never an acceptable fallback.
# aria-hidden can be true on an otherwise visibly clickable VK post-footer
# element; use geometry/hit-testing rather than accessibility exposure alone.
POST_FOOTER_ACTION_JS = r"""
({groupId, postId, action, mark}) => {
  const postKey = '-' + Number(groupId) + '_' + Number(postId);
  const root = document.getElementById('post' + postKey) ||
    [...document.querySelectorAll('[data-post-id]')]
      .find(e => e.getAttribute('data-post-id') === postKey) || null;
  const out = {ok:false, reason:'', postRoot:!!root, primaryArticle:false,
    likeElements:0, visibleLikes:0, liked:null, ariaHidden:null,
    targetCount:0, action};
  if (!root) {out.reason='Точный контейнер поста не найден'; return out;}

  const single = document.querySelector('#page-single-post');
  const primary = single?.querySelector(':scope > div > article') ||
                  single?.querySelector(':scope > article') || null;
  // Never accept a continuation-feed post as the main post.
  if (single && primary && !primary.contains(root)) {
    out.reason='Контейнер не принадлежит основному article'; return out;
  }
  if (root.getAttribute('data-post-nesting-lvl') &&
      root.getAttribute('data-post-nesting-lvl') !== '0') {
    out.reason='Выбрана вложенная запись, а не основной пост'; return out;
  }
  const scope = root;
  out.primaryArticle = !!(primary || root.closest('article'));
  // "Rendered" is NOT "in the viewport". The post can be longer than one
  // screen, and the real footer button is below the fold. Playwright will
  // scroll it into view and check click actionability before the actual click.
  // aria-hidden=true on VK footer-like must not be used as a visibility test.
  const visible = e => {
    if (!e || !e.isConnected || e.closest('[hidden], [inert]')) return false;
    const r = e.getBoundingClientRect();
    if (r.width < 9 || r.height < 9) return false;
    for (let p=e; p && p.nodeType === 1; p=p.parentElement) {
      const css = getComputedStyle(p);
      if (css.display === 'none' || css.visibility === 'hidden' ||
          Number(css.opacity) === 0 || css.pointerEvents === 'none') return false;
    }
    return true;
  };
  // A repost embedded in the post body has its own data-testid="post" and
  // can also have footer buttons. Ignore it as well as comments.
  const inPrimary = e => {
    if (!scope.contains(e) ||
        e.closest('[data-testid="wall_comments_comment_root"]')) return false;
    const closestPost = e.closest('[data-testid="post"][data-post-id]');
    return !closestPost || closestPost === root;
  };
  const allLikes = [...scope.querySelectorAll('[data-testid="post_footer_action_like"]')]
    .filter(inPrimary);
  const likes = allLikes.filter(visible);
  out.likeElements = allLikes.length;
  out.visibleLikes = likes.length;
  if (likes.length !== 1) {
    out.reason='У основного поста нет единственной видимой кнопки footer-like';
    return out;
  }
  const like = likes[0];
  const rawLiked = like.getAttribute('data-user-likes');
  out.liked = rawLiked === 'true' ? true : rawLiked === 'false' ? false : null;
  out.ariaHidden = like.getAttribute('aria-hidden');
  if (out.liked === null) {
    out.reason='Нет достоверного data-user-likes у кнопки поста'; return out;
  }
  const label = (like.getAttribute('aria-label') || '').toLowerCase();
  if (out.liked === false && /убрать|снять|удалить|remove|unlike/.test(label)) {
    out.reason='Несогласованность data-user-likes и названия кнопки'; return out;
  }
  if (out.liked === true && /отправить реакцию|поставить лайк/.test(label)) {
    out.reason='Несогласованность data-user-likes и названия кнопки'; return out;
  }
  if (mark) document.querySelectorAll('[data-vk-batch-footer-action]')
    .forEach(e => e.removeAttribute('data-vk-batch-footer-action'));
  if (action === 'like') {
    out.targetCount = 1;
    out.ok = true;
    if (mark) like.setAttribute('data-vk-batch-footer-action','like');
    return out;
  }
  if (action !== 'share') {out.reason='Неизвестное действие'; return out;}
  const lr = like.getBoundingClientRect();
  const sameToolbar = e => {
    const r=e.getBoundingClientRect();
    return Math.abs((r.top+r.bottom)/2-(lr.top+lr.bottom)/2) <= 35;
  };
  const shareText = /поделиться|репост|share|repost/i;
  const candidates = [...scope.querySelectorAll('button,[role="button"]')]
    .filter(e => e !== like && inPrimary(e) && visible(e) && sameToolbar(e))
    .filter(e => {
      const label = [e.getAttribute('aria-label'),e.getAttribute('title'),
        (e.innerText || '').slice(0,100),e.getAttribute('data-testid')].join(' ');
      return shareText.test(label);
    });
  // Nested interactive wrappers must not count twice; prefer the deepest one.
  const leaves = candidates.filter(e => !candidates.some(other =>
    other !== e && e.contains(other)));
  out.targetCount = leaves.length;
  if (leaves.length !== 1) {
    out.reason='Нет единственной кнопки репоста в строке действий основного поста';
    return out;
  }
  out.ok = true;
  if (mark) leaves[0].setAttribute('data-vk-batch-footer-action','share');
  return out;
}
"""


def post_footer_info(page, group_id: int, post_id: int, action: str, mark: bool = False) -> dict:
    return page.evaluate(POST_FOOTER_ACTION_JS, {
        'groupId': group_id, 'postId': post_id, 'action': action, 'mark': mark,
    })


def wait_post_footer_info(page, group_id: int, post_id: int, action: str, mark: bool = False) -> dict:
    last = None
    for attempt in range(12):
        ensure_ok(page)
        last = post_footer_info(page, group_id, post_id, action, mark=mark)
        if last['ok']:
            return last
        if attempt < 11:
            page.wait_for_timeout(350)
    raise StopRun(f'Нет безопасной кнопки {action} основного поста: {last["reason"]} '
                  f'(likeElements={last["likeElements"]}, visibleLikes={last["visibleLikes"]}, '
                  f'targetCount={last["targetCount"]})')


def post_footer_button(page, group_id: int, post_id: int, action: str):
    info = wait_post_footer_info(page, group_id, post_id, action, mark=True)
    if action == 'like' and info['liked']:
        raise StopRun('Кнопка основного поста показывает, что лайк уже стоит; повтор запрещён')
    locator = page.locator(f'[data-vk-batch-footer-action="{action}"]')
    # Off-screen is expected on long posts. This must happen before reserving
    # the SQLite action, and Playwright will verify click actionability.
    try:
        locator.scroll_into_view_if_needed(timeout=12000)
    except (PWTimeout, PlaywrightError) as exc:
        raise StopRun(f'Кнопка {action} основного поста недоступна после прокрутки') from exc
    return locator


def verified_like_button(page, group_id: int, post_id: int):
    return post_footer_button(page, group_id, post_id, 'like')


def click_like(page, group_id: int, post_id: int, *, prepared_button=None):
    # Re-resolve immediately before clicking: a SPA update could have replaced
    # the previously selected node or toggled its state.
    locator = post_footer_button(page, group_id, post_id, 'like')
    locator.click(timeout=12000)
    page.wait_for_timeout(500)
    ensure_ok(page)


SHARE_DIAG_JS = r"""
() => {
  // Privacy-preserving diagnostic: count semantic elements and report ONLY
  // a fixed allowlist of generic share labels; never report chats or names.
  const norm = s => (s || '').replace(/\s+/g,' ').trim();
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect(), cs=getComputedStyle(el);
    return r.width>2 && r.height>2 && cs.display!=='none' &&
      cs.visibility!=='hidden' && cs.opacity!=='0';
  };
  const allowed = [
    'Поделиться','Экспортировать','На своей стене','На своей странице',
    'В личном сообщении','В личные сообщения','Личным сообщением',
    'В сообщество','В истории','Отправить','Опубликовать','Поделиться постом'
  ];
  const labels = new Set();
  for(const el of document.querySelectorAll('span,label,[role="radio"],button,h2,h3')) {
    if(!visible(el)) continue;
    const value=norm(el.innerText);
    if(allowed.includes(value)) labels.add(value);
  }
  return {
    visibleDialogs:[...document.querySelectorAll('[role="dialog"],[aria-modal="true"]')].filter(visible).length,
    visibleRadioInputs:[...document.querySelectorAll('input[type="radio"]')].filter(visible).length,
    visibleRoleRadios:[...document.querySelectorAll('[role="radio"],[role="menuitemradio"]')].filter(visible).length,
    visibleFinalButtons:[...document.querySelectorAll('button[data-testid="share_tab_submit"]')].filter(visible).length,
    allowedLabels:[...labels].sort()
  };
}
"""


def wait_share_destination(page, *, timeout_ms: int = 9000, mark: bool = True) -> dict:
    """Wait for the modal render, not merely for the share opener click.

    An absent modal never authorizes reposting. We do not click 'try again',
    repeat the share opener, or assume the target destination.
    """
    last = {'ok': False, 'reason': 'Окно репоста ещё не отрисовано'}
    steps = max(1, (timeout_ms + 349) // 350)
    for attempt in range(steps):
        ensure_ok(page)
        last = page.evaluate(SHARE_DIALOG_JS, {'action': 'state'})
        if last.get('ok'):
            if not mark:
                return last
            prepared = page.evaluate(SHARE_DIALOG_JS, {'action': 'prepare'})
            if prepared.get('ok'):
                return prepared
            last = prepared
        if attempt < steps - 1:
            page.wait_for_timeout(350)
    diag = page.evaluate(SHARE_DIAG_JS)
    print('[SHARE DIAG] Окна:', diag['visibleDialogs'],
          'радио input:', diag['visibleRadioInputs'],
          'радио role:', diag['visibleRoleRadios'],
          'кнопки отправки:', diag['visibleFinalButtons'])
    print('[SHARE DIAG] Общие подписи интерфейса:', ', '.join(diag['allowedLabels']) or 'не обнаружены')
    return {'ok': False, 'reason': str(last.get('reason') or 'Нет достоверного диалога репоста'),
            'diagnostic': diag}


def inspect_share_only(page, group_id: int, post_id: int) -> None:
    """Opens ONLY the share chooser. Does not pick a destination or submit."""
    go_to_post(page, group_id, post_id)
    state = refresh_state(page, group_id, post_id)
    print('[SHARE INSPECT] Состояние: repost=', state.get('reposted'),
          'источник=', state.get('source'))
    if state.get('reposted') is not False:
        print('[SHARE INSPECT] Репост уже существует или состояние неопределённо; окно не открываем.')
        return
    post_footer_button(page, group_id, post_id, 'share').click(timeout=12000)
    result = wait_share_destination(page, mark=False)
    if result['ok']:
        print('[SHARE INSPECT] Радиокнопки найдены. На своей стене=', result['state']['wall'],
              'в личном сообщении=', result['state']['direct_message'])
        print('[SHARE INSPECT] Контрольное окно распознано; публикации не было.')
    else:
        print('[SHARE INSPECT] Окно пока не распознано:', result['reason'])
    print('[SHARE INSPECT] На своей стене / в личном сообщении ничего не выбирали, публикацию не отправляли.')
    try:
        input('[SHARE INSPECT] Посмотри на окно VK, затем нажми Enter для закрытия...')
    except (EOFError, KeyboardInterrupt):
        pass


def click_share_then_publish(page, group_id: int, post_id: int, *, before_publish=None):
    # Share must come from the SAME main-post toolbar as the verified like.
    # A generic 'Поделиться' somewhere in comments or recommended posts is not valid.
    post_footer_button(page, group_id, post_id, 'share').click(timeout=12000)
    # Share is a UI-only opener. VK may mount the real radio controls
    # asynchronously, even after the click reports success.
    check = wait_share_destination(page, mark=True)
    if not check['ok']:
        raise StopRun('Не удалось проверить окно репоста до публикации: ' + check['reason'])
    if check["state"]["direct_message"] is True and check["state"]["wall"] is True:
        raise StopRun("VK показывает противоречивое состояние назначения")
    if check["state"]["wall"] is not True:
        if check["method"] == "element":
            page.locator('[data-vk-poc-own="true"]').click(timeout=12000)
        elif check["method"] == "point":
            page.mouse.click(check["point"]["x"], check["point"]["y"])
        else:
            raise StopRun("Неизвестный способ выбора личной стены")
        page.wait_for_timeout(350)
    state = page.evaluate(SHARE_DIALOG_JS, {"action": "state"})
    if not state["ok"] or state["state"]["wall"] is not True or \
            state["state"]["direct_message"] is not False:
        raise StopRun("VK не подтвердил назначение «На своей стене»")

    publish = page.evaluate(PUBLISH_CONTROLS_JS)
    if not publish["ok"] or len(publish.get("candidates", [])) != 1:
        raise StopRun("Нет единственной кнопки финальной публикации в окне VK")
    if publish["candidates"][0]["disabled"]:
        raise StopRun("Кнопка публикации неактивна; ограничение VK")
    state = page.evaluate(SHARE_DIALOG_JS, {"action": "state"})
    if not state["ok"] or state["state"]["wall"] is not True or \
            state["state"]["direct_message"] is not False:
        raise StopRun("Назначение изменилось перед публикацией")
    # This is the FIRST operation that can create an externally visible repost.
    # Persist uncertain and reserve the attempt only here, never for the popup.
    if before_publish is not None:
        before_publish()
    page.locator('[data-vk-poc-publish="0"]').click(timeout=12000)
    page.wait_for_timeout(700)
    ensure_ok(page)


def after_action_state(page, group_id: int, post_id: int):
    # A new document is required. VK's in-memory prefetch cache may retain
    # the old values even when the UI has already changed.
    go_to_post(page, group_id, post_id)
    state = refresh_state(page, group_id, post_id)
    footer = post_footer_info(page, group_id, post_id, 'like')
    state['main_post_liked'] = footer['liked'] if footer['ok'] else None
    return state


def process_one(page, db, group_id: int, row: sqlite3.Row, pause: float, safety: SafetyBudget | None = None, actions: set[str] | None = None, *, already_open: bool = False) -> bool:
    """True on success, False when a terminal check requires stopping."""
    pid = row["post_id"]
    url = POST_URL.format(group_id=group_id, post_id=pid)
    print(f"\n[POST] {url}")
    if not already_open:
        go_to_post(page, group_id, pid)
    else:
        # The caller verified this exact root via go_to_post already.
        # Do not make a second navigation only to validate the timestamp.
        ensure_ok(page)
        matches = page.evaluate(POST_ROOT_READY_JS, {"groupId": group_id, "postId": pid})
        if not matches:
            raise StopRun("Основной пост исчез после проверки даты; нажатия запрещены")
    data = refresh_state(page, group_id, pid)
    print(f"   Предсостояние: like={data['liked']}, repost={data['reposted']} ({data['source']})")

    for action, desired_flag, field in (("like", "liked", "canLike"),
                                        ("repost", "reposted", "canRepost")):
        if actions is not None and action not in actions:
            continue
        if row[f"{action}_status"] != "pending":
            continue
        # Re-read flags after the previous step and browser reload.
        current = data[desired_flag]
        # For the LIKE, the live main-post footer MUST agree with the VK post
        # state even when we would skip the click as 'already'.
        if action == 'like' and current is not None:
            live_main = wait_post_footer_info(page, group_id, pid, 'like')
            if live_main['liked'] != current:
                raise StopRun('Основной лайк поста не совпадает с VK prefetch; '
                              'ничего не нажато и статус не изменён')
        if current is True:
            status_write(db, group_id, pid, action, "already", "Уже отмечено в VK")
            print(f"   [{action.upper()}] уже выполнено — пропуск")
            continue
        if current is None:
            status_write(db, group_id, pid, action, "uncertain", "Не удалось проверить состояние до клика")
            raise StopRun(f"{action}: состояние неизвестно; никаких кликов по нему не делаем")
        if data[field] is False:
            status_write(db, group_id, pid, action, "unavailable", "VK запрещает действие")
            print(f"   [{action.upper()}] действие недоступно по данным VK")
            continue
        # The exact button is resolved BEFORE marking the DB uncertain.
        # No generic label fallback is allowed (comments can also contain Лайк).
        ensure_ok(page)
        prepared_like = verified_like_button(page, group_id, pid) if action == "like" else None
        # For reposts the first Share click just opens a dialog; it MUST NOT
        # be counted as a repost attempt or poison the DB with 'uncertain'.
        # The callback is executed immediately before the FINAL publish click.
        repost_submit_started = False
        def before_repost_publish():
            nonlocal repost_submit_started
            if safety is not None:
                safety.reserve(pid, 'repost')
            status_write(db, group_id, pid, 'repost', 'uncertain',
                         'Финальная публикация подготовлена; требуется подтверждение VK')
            repost_submit_started = True
        try:
            if action == 'like':
                if safety is not None:
                    safety.reserve(pid, 'like')
                status_write(db, group_id, pid, 'like', 'uncertain',
                             'Подготовка клика; требуется перепроверка')
                click_like(page, group_id, pid, prepared_button=prepared_like)
            else:
                click_share_then_publish(page, group_id, pid,
                                         before_publish=before_repost_publish)
            status_write(db, group_id, pid, action, "uncertain", "Клик выполнен, подтверждение VK ещё не получено")
            time.sleep(pause)  # stable UI/pacing, not a way to evade anti-spam
            data = after_action_state(page, group_id, pid)
            if data[desired_flag] is True and (action != 'like' or data.get('main_post_liked') is True):
                status_write(db, group_id, pid, action, "verified", "VK подтвердил состояние основного поста после загрузки")
                print(f"   [{action.upper()}] подтверждено VK")
            else:
                raise StopRun(f"{action}: клик был, но VK не подтвердил результат; повтор запрещён")
        except (PostUnavailable, PostTemporaryError) as exc:
            # A failure after a click is NOT a safe no-click skip. Do not let
            # the batch catch this subclass and silently move on.
            raise StopRun(f'{action}: VK стал недоступен после клика; '
                          'статус uncertain, повтор запрещён') from exc
        except StopRun:
            raise
        except (PWTimeout, PlaywrightError) as exc:
            if action == 'repost' and not repost_submit_started:
                # A broken chooser cannot have passed our final submit gate.
                # Keep pending, but stop this run for a human UI check.
                # Still pending: no real repost attempt was reserved or submitted.
                pass
            else:
                status_write(db, group_id, pid, action, 'uncertain',
                             f'Ошибка около реального действия: {str(exc)[:140]}')
            raise StopRun(f"{action}: ошибка интерфейса VK; дальнейшие действия остановлены") from exc
    return True


def recheck_access_one(page, db, group_id: int, post_id: int) -> bool:
    """Read-only manual retry of a previously skipped post."""
    row = db.execute('SELECT state FROM post_access WHERE group_id=? AND post_id=?',
                     (group_id, post_id)).fetchone()
    if row is None:
        print(f'[ACCESS RECHECK] {post_id}: нет сохранённого пропуска; изменений нет.')
        return False
    try:
        go_to_post(page, group_id, post_id)
    except PostUnavailable as exc:
        record_post_access(db, group_id, post_id, exc.kind, str(exc))
        print(f'[ACCESS RECHECK] {post_id}: всё ещё {exc.kind}; запись не возвращена в очередь.')
        return False
    except PostTemporaryError as exc:
        record_post_access(db, group_id, post_id, 'temporary', str(exc))
        print(f'[ACCESS RECHECK] {post_id}: временный сбой; запись не возвращена в очередь.')
        return False
    except PostMarkupUnknown as exc:
        record_post_access(db, group_id, post_id, 'unresolved', str(exc))
        print(f'[ACCESS RECHECK] {post_id}: контейнер VK не распознан; никаких действий.')
        return False
    clear_post_access(db, group_id, post_id)
    print(f'[ACCESS RECHECK] {post_id}: доступ восстановлен; статусы действий сохранены.')
    return True



def recheck_one(page, db, group_id: int, post_id: int, *, confirm_no_click_like: bool = False,
                confirm_comment_click_like: bool = False,
                confirm_repost_dialog_no_submit: bool = False) -> None:
    """Recheck exact post; re-arm only proven no-click states.

    --confirm-no-click-like is a one-post, operator-acknowledged recovery for
    the v6.4 '0 candidates' failure shown in the terminal. No click was made.
    Any attempted action remains blocked without positive confirmation by VK.
    """
    row = db.execute(
        'SELECT like_status,repost_status,detail FROM posts WHERE group_id=? AND post_id=?',
        (group_id, post_id),
    ).fetchone()
    if row is None:
        raise StopRun('Этого поста нет в SQLite. Сначала выполни --scan-only.')
    go_to_post(page, group_id, post_id)
    state = refresh_state(page, group_id, post_id)
    print('[RECHECK] like=', state['liked'], 'repost=', state['reposted'],
          'source=', state['source'])
    if confirm_repost_dialog_no_submit:
        # ONLY for the v6.21 failure 'Не найдены обе радиокнопки...'.
        # The operator explicitly confirms that the run stopped at the chooser,
        # before the final publish button, and that exact log was observed.
        if row['repost_status'] != 'uncertain' or \
                row['detail'] != 'Подготовка клика; требуется перепроверка':
            raise StopRun('Это восстановление применимо только к неопределённому '
                          'репосту v6.21 до финального нажатия')
        if state['reposted'] is not False or state['source'] in ('none', 'conflict'):
            raise StopRun('VK не подтвердил отсутствие репоста; статус не меняем')
        update = db.execute(
            'UPDATE posts SET repost_status=?,detail=?,updated_at=? '
            'WHERE group_id=? AND post_id=? AND repost_status=? AND detail=?',
            ('pending', 'Оператор подтвердил лог v6.21: финальная публикация не нажата',
             moment(), group_id, post_id, 'uncertain', 'Подготовка клика; требуется перепроверка'))
        if update.rowcount != 1:
            db.rollback()
            raise StopRun('Состояние поста изменилось; восстановление отменено')
        db.commit()
        print('[RECHECK] repost: VK подтверждает отсутствие репоста; '
              'по подтверждённому оператором логу v6.21 -> pending. Кликов не было.')
        return
    if confirm_comment_click_like:
        # v6.5 could accidentally click a comment heart. Operator confirms
        # the specific incident; DOM and VK SSR MUST both show main post unliked.
        footer = wait_post_footer_info(page, group_id, post_id, 'like')
        if footer['liked'] is not False or state['liked'] is not False:
            raise StopRun('Нельзя исправить состояние: основной пост должен достоверно быть без лайка')
        if row['like_status'] not in ('pending', 'uncertain', 'error', 'verified', 'already'):
            raise StopRun('Статус лайка уже завершён или не требует восстановления')
        db.execute('UPDATE posts SET like_status=?,detail=?,updated_at=? '
                   'WHERE group_id=? AND post_id=?',
                   ('pending', 'Оператор подтвердил: предыдущий клик был по комментарию; '
                    'основной пост без лайка', moment(), group_id, post_id))
        db.commit()
        print('[RECHECK] Исправлен только статус лайка основного поста -> pending; '
              'лайк комментария не изменён.')
        return
    for action, flag in (('like', 'liked'), ('repost', 'reposted')):
        if row[f'{action}_status'] != 'uncertain':
            continue
        # A confirmed True is always safe to record: no click is necessary,
        # even when a previous run had already attempted the action.
        # Only False needs evidence that NO click had been attempted.
        if state[flag] is True:
            if action == 'like':
                footer = wait_post_footer_info(page, group_id, post_id, 'like')
                if footer['liked'] is not True:
                    print('[RECHECK] like: VK prefetch и кнопка основного поста не согласованы; '
                          'SQLite не меняем.')
                    continue
            status_write(db, group_id, post_id, action, 'already',
                         'Повторно проверено: VK показывает выполненное действие')
            print(f'[RECHECK] {action}: VK подтверждает действие, отмечено в SQLite без клика.')
            continue
        if state[flag] is None:
            print(f'[RECHECK] {action}: состояние по-прежнему неизвестно.')
            continue
        if (action == 'like' and confirm_no_click_like and
                row['detail'] == 'Подготовка клика; требуется перепроверка' and
                state[flag] is False and state['source'] not in ('none', 'conflict')):
            # This old detail is NOT itself proof of no click. Only use when
            # the operator has the [STOP] 0-candidates log for this exact post.
            db.execute(
                "UPDATE posts SET like_status='pending',detail=?,updated_at=? "
                'WHERE group_id=? AND post_id=?',
                ('Оператор подтвердил по логу: клик не выполнялся', moment(), group_id, post_id),
            )
            db.commit()
            print('[RECHECK] like: возвращено в pending по подтверждению отсутствия клика.')
            continue
        if row['detail'] != 'Не удалось проверить состояние до клика':
            print(f'[RECHECK] {action}: не изменён (не доказано, что клика не было).')
            continue
        db.execute(
            f"UPDATE posts SET {action}_status='pending',detail=?,updated_at=? "
            'WHERE group_id=? AND post_id=?',
            ('Без клика: повторная проверка дала False', moment(), group_id, post_id),
        )
        db.commit()
        print(f'[RECHECK] {action}: возвращено в очередь (действия не выполнялись).')


def scan_wall(page, db, group_id: int, group_url: str, scrolls: int, stale_limit: int):
    print(f"\n[SCAN] Открываю стену: {group_url}")
    page.goto(group_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1300)
    ensure_ok(page)
    page.mouse.move(720, 620)  # hover the main feed before wheel scrolling
    newly_total, no_new, reason = 0, 0, "лимит прокруток"
    for i in range(scrolls + 1):
        data = page.evaluate(DISCOVER_JS, {"groupId": group_id})
        new = add_posts(db, group_id, data["ids"])
        newly_total += new
        no_new = no_new + 1 if new == 0 else 0
        if new or i % 20 == 0:
            print(f"   прокрутка {i}/{scrolls}, найдено на экране={len(data['ids'])}, новых={new}")
        if i == scrolls:
            break
        if no_new >= stale_limit and len(data["ids"]) > 0:
            reason = f"не появлялись новые посты {no_new} прокруток подряд"
            break
        page.mouse.wheel(0, 1850)
        page.wait_for_timeout(750)
        ensure_ok(page)
    total = db.execute("SELECT COUNT(*) FROM posts WHERE group_id=?", (group_id,)).fetchone()[0]
    print(f"[SCAN] В БД {total} постов (+{newly_total} за проход); завершено: {reason}.")
    if total == 0:
        raise StopRun("Не найдено записей. Проверь ссылку, ID сообщества и вход в VK")
    if reason == "лимит прокруток":
        print("[NOTE] Достигнут лимит прокруток. Это НЕ доказывает, что сканирован весь архив!")
    else:
        print("[NOTE] Прокрутка остановилась без новых ID. Это не гарантирует полноту архива VK.")
    return total


def login_only(playwright) -> int:
    """Keep the persistent browser open while its owner signs into VK manually.

    Unlike a wall scan, this mode never inspects the page for protection messages
    and never performs likes/reposts. VK ID may redirect through a separate tab.
    """
    print(f"[LOGIN] Используемый профиль Chromium: {PROFILE}")
    print("[LOGIN] Ни сканирования, ни лайков, ни репостов в этом режиме нет.")
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE), headless=False, locale="ru-RU",
        viewport={"width": 1320, "height": 900},
    )
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("crash", lambda _: print("\n[CRASH] Вкладка Chromium завершилась аварийно."))
        try:
            page.goto("https://vk.com/feed", wait_until="domcontentloaded", timeout=30000)
        except (PWTimeout, PlaywrightError) as exc:
            # A redirect or slow login page must not prematurely destroy the browser.
            print(f"[LOGIN] Перенаправление/загрузка VK: {str(exc)[:180]}")
            print("[LOGIN] Если окно браузера осталось открытым, продолжай вход вручную.")
        print("[LOGIN] Войди в VK самостоятельно, включая любые проверки VK ID.")
        print("[LOGIN] После входа открой https://vk.com/feed и убедись, что видна твоя лента.")
        while True:
            choice = input("После успешного входа введи ГОТОВО; СТАТУС — проверить окно; ВЫХОД — закрыть: ").strip().upper()
            if choice == "СТАТУС":
                tabs = [tab for tab in context.pages if not tab.is_closed()]
                print(f"[LOGIN] Открытых вкладок: {len(tabs)}")
                for tab in tabs[:8]:
                    # Do not expose sensitive URL query parameters in console logs.
                    url = urlsplit(tab.url)
                    print(f"  - {url.hostname or 'неизвестный адрес'}{url.path[:90]}")
                continue
            if choice == "ВЫХОД":
                print("[LOGIN] Выход без подтверждения авторизации. Данные профиля не удаляются.")
                return 1
            if choice == "ГОТОВО":
                tabs = [tab for tab in context.pages if not tab.is_closed()]
                vk_tabs = [tab for tab in tabs if urlsplit(tab.url).hostname in ALLOWED_HOSTS]
                if not vk_tabs:
                    print("[LOGIN] Не найдено открытых вкладок VK. Возможно, Chromium завершился или вход не закончен.")
                    print("[LOGIN] Проверь окно и повтори; сканирование пока не запускай.")
                    continue
                print("[LOGIN] Вход подтверждён оператором. Chromium-профиль сохранится после закрытия окна.")
                print("[LOGIN] Это ещё не автоматическая проверка прав аккаунта — сначала запусти --scan-only.")
                return 0
            print("[LOGIN] Ожидаю ГОТОВО, СТАТУС или ВЫХОД. Браузер остаётся открытым.")
    finally:
        context.close()


def print_report(db, group_id: int) -> None:
    total = db.execute("SELECT COUNT(*) FROM posts WHERE group_id=?", (group_id,)).fetchone()[0]
    print(f"[REPORT] Всего обнаружено постов: {total}")
    if total:
        row = db.execute('SELECT MIN(post_id),MAX(post_id) FROM posts WHERE group_id=?',
                         (group_id,)).fetchone()
        print(f'[REPORT] Диапазон найденных ID: {row[0]}..{row[1]}; это не гарантия полноты архива')
    for column in ("like", "repost"):
        rows = db.execute(
            f"SELECT {column}_status AS state,COUNT(*) AS n FROM posts "
            f"WHERE group_id=? GROUP BY {column}_status ORDER BY {column}_status",
            (group_id,),
        ).fetchall()
        print(f"   {column}: " + (", ".join(f"{r['state']}={r['n']}" for r in rows) or "нет записей"))
    report_access(db, group_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="VK batch v6.15: история группы и защитные лимиты")
    parser.add_argument("--group-id", type=int, default=188206921, help="ID группы, без минуса")
    parser.add_argument("--group-url", default="https://vk.com/official_smr174", help="HTTPS-ссылка на группу")
    parser.add_argument("--execute", action="store_true", help="Выполнить действия без запроса ДА для каждого поста")
    parser.add_argument("--scan-only", action="store_true", help="Только собрать ссылки; никаких лайков/репостов")
    parser.add_argument("--skip-scan", action="store_true", help="Не прокручивать группу, использовать найденные ранее ID")
    parser.add_argument("--report", action="store_true", help="Только отчёт SQLite; VK не открывается")
    parser.add_argument("--audit-pending", type=int, metavar="N", help="Без кликов проверить N ожидающих публикаций; 0 = все ожидающие")
    parser.add_argument("--audit-report", action="store_true", help="Показать проверенные кандидаты без открытия VK")
    parser.add_argument("--only-audited", action="store_true", help="С --skip-scan брать только проверенные записи с отсутствующими действиями")
    parser.add_argument("--login-only", action="store_true", help="Ручной вход в VK без сканирования и действий")
    parser.add_argument("--diagnose-post", type=int, metavar="ID", help="Открыть один пост, проверить DOM и состояния, не выполнять действия")
    parser.add_argument("--inspect-share", type=int, metavar="ID", help="Открыть только окно выбора репоста, не выполнять публикацию")
    parser.add_argument("--recheck-post", type=int, metavar="ID", help="Перепроверить пост без действий и безопасно уточнить uncertain")
    parser.add_argument("--recheck-access", type=int, metavar="ID", help="Повторно проверить ранее пропущенную запись без действий")
    parser.add_argument("--confirm-comment-click-like", action="store_true",
                        help="При --recheck-post: подтвердить, что v6.5 поставил лайк комментарию, не основному посту")
    parser.add_argument("--confirm-no-click-like", action="store_true",
                        help="Только с --recheck-post: подтвердить по логу, что предыдущая ошибка лайка произошла ДО клика")
    parser.add_argument("--confirm-repost-dialog-no-submit", action="store_true",
                        help="Только с --recheck-post: подтвердить лог v6.21 о сбое окна репоста ДО публикации")
    parser.add_argument("--post-id", type=int, metavar="ID", help="Работать строго с одним ID поста: без --execute только чтение; с --execute выполнить недостающие действия")
    parser.add_argument("--max-posts", type=int, default=10, help="Максимум обработанных записей за запуск; 0 = без лимита")
    parser.add_argument("--scan-scrolls", type=int, default=120, help="Лимит прокруток группы (по умолчанию 120)")
    parser.add_argument("--stale-scrolls", type=int, default=12, help="Остановить сканирование без новых ID")
    parser.add_argument("--pause", type=float, default=4.0, help="Пауза после действия для стабильной работы (секунды)")
    parser.add_argument("--max-actions-run", type=int, default=DEFAULT_ACTIONS_PER_RUN,
                        help="Защитный лимит ПОПЫТОК лайка/репоста за запуск; по умолчанию 2 (НЕ безопасная квота VK)")
    parser.add_argument("--max-actions-24h", type=int, default=DEFAULT_ACTIONS_PER_24H,
                        help="Защитный лимит ПОПЫТОК за скользящие 24 часа; по умолчанию 6 (НЕ безопасная квота VK)")
    parser.add_argument("--safety-report", action="store_true",
                        help="Локально показать историю попыток и защитную блокировку; VK не открывается")
    parser.add_argument("--ack-warning", action="store_true",
                        help="Явно снять блокировку после РУЧНОЙ проверки и устранения предупреждения VK")
    parser.add_argument("--mark-rate-limit", action="store_true",
                        help="Локально записать уже увиденную ошибку Too many requests per second; без открытия VK")
    args = parser.parse_args()
    if args.inspect_share is not None and args.inspect_share <= 0:
        parser.error('ID публикации должен быть положительным')
    if args.diagnose_post is not None and args.diagnose_post <= 0:
        parser.error('ID диагностируемого поста должен быть положительным')
    if args.recheck_post is not None and args.recheck_post <= 0:
        parser.error('ID перепроверяемого поста должен быть положительным')
    if args.recheck_access is not None and args.recheck_access <= 0:
        parser.error('ID проверки доступа должен быть положительным')
    if args.post_id is not None and args.post_id <= 0:
        parser.error('ID целевого поста должен быть положительным')
    if args.confirm_comment_click_like and args.recheck_post is None:
        parser.error('--confirm-comment-click-like используется только с --recheck-post ID')
    if args.confirm_comment_click_like and args.confirm_no_click_like:
        parser.error('Выбери только один вариант исправления статуса')
    if args.confirm_repost_dialog_no_submit and args.recheck_post is None:
        parser.error('--confirm-repost-dialog-no-submit только с --recheck-post ID')
    if sum((args.confirm_no_click_like, args.confirm_comment_click_like,
            args.confirm_repost_dialog_no_submit)) > 1:
        parser.error('Восстанавливай только одно действие за раз')
    if args.confirm_no_click_like and args.recheck_post is None:
        parser.error('--confirm-no-click-like используется только с --recheck-post ID')
    if args.inspect_share is not None and (args.execute or args.scan_only or
                                           args.post_id is not None or args.recheck_post is not None or
                                           args.recheck_access is not None or args.diagnose_post is not None or
                                           args.login_only or args.audit_pending is not None or
                                           args.report or args.audit_report or args.ack_warning):
        parser.error('--inspect-share — отдельный диагностический режим без публикации')
    if args.only_audited and (not args.skip_scan or args.post_id is not None or
                              args.report or args.audit_report or args.login_only or
                              args.scan_only or args.diagnose_post is not None or
                              args.recheck_post is not None or args.recheck_access is not None or
                              args.audit_pending is not None):
        parser.error('--only-audited используется только с --skip-scan в режиме обычной очереди')
    if args.audit_pending is not None and args.audit_pending < 0:
        parser.error('Число записей для аудита должно быть >= 0')
    if args.audit_pending is not None and (args.execute or args.scan_only or args.post_id is not None or
                                           args.report or args.audit_report or args.login_only or
                                           args.diagnose_post is not None or args.recheck_post is not None or
                                           args.recheck_access is not None):
        parser.error('--audit-pending — отдельный режим без действий')
    if args.audit_report and (args.execute or args.scan_only or args.post_id is not None or
                              args.login_only or args.report or args.diagnose_post is not None or
                              args.recheck_post is not None or args.recheck_access is not None):
        parser.error('--audit-report — отдельный режим без действий')
    if args.post_id is not None and (args.report or args.login_only or args.scan_only or
                                     args.diagnose_post is not None or args.recheck_post is not None or
                                     args.recheck_access is not None):
        parser.error('--post-id несовместим с --report, --login-only, --scan-only и режимами диагностики')
    if args.group_id <= 0 or args.max_posts < 0 or args.scan_scrolls < 1 or \
       args.stale_scrolls < 2 or not 0 <= args.pause <= 60 or \
       args.max_actions_run <= 0 or args.max_actions_24h <= 0:
        parser.error("Неверные значения параметров")
    try:
        group_url = validate_group_url(args.group_url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.scan_only and args.execute:
        parser.error("Выбери либо --scan-only, либо --execute")
    if args.execute and args.post_id is None and args.max_posts == 0:
        parser.error('В режиме исполнения --max-posts 0 (безлимитно) запрещён. Укажи размер партии.')
    if args.safety_report or args.ack_warning or args.mark_rate_limit:
        if (args.execute or args.scan_only or args.audit_pending is not None or
            args.audit_report or args.post_id is not None or args.report or
            args.login_only or args.diagnose_post is not None or args.inspect_share is not None or
            args.recheck_post is not None or args.recheck_access is not None):
            parser.error('Команды защиты — отдельные локальные операции')
        if sum((args.safety_report, args.ack_warning, args.mark_rate_limit)) > 1:
            parser.error('Выбери только одну локальную операцию защиты')
        with closing(db_open(DB_PATH)) as db:
            if args.safety_report:
                safety_report(db, args.group_id)
            elif args.mark_rate_limit:
                lock_on_platform_warning(db, args.group_id,
                                         'Оператор увидел VK: Too many requests per second')
                print('[RATE LIMIT] Ограничение записано в SQLite. Новые запросы VK '
                      'заблокированы до ручной проверки и --ack-warning.')
            else:
                row = safety_lock_state(db, args.group_id)
                if row:
                    db.execute('DELETE FROM safety_lock WHERE group_id=0')
                    db.commit()
                    print('[SAFETY] Защитная блокировка снята по решению оператора. '
                          'Это не подтверждает отсутствие ограничений VK.')
                else:
                    print('[SAFETY] Нет блокировки для снятия')
        return 0
    if args.recheck_access is not None and (args.execute or args.scan_only or args.skip_scan or
                                            args.report or args.audit_report or args.login_only or
                                            args.diagnose_post is not None or args.recheck_post is not None):
        parser.error('--recheck-access — отдельный режим только чтения')
    if args.report or args.audit_report:
        with closing(db_open(DB_PATH)) as db:
            if args.report:
                print_report(db, args.group_id)
            else:
                audit_report(db, args.group_id)
        return 0
    if args.diagnose_post is not None or args.inspect_share is not None or args.recheck_post is not None or args.recheck_access is not None:
        print('VK BATCH v6.15 | диагностика одной публикации, только чтение')
        PROFILE.mkdir(parents=True, exist_ok=True)
        try:
            with closing(db_open(DB_PATH)) as check_db:
                require_platform_unlocked(check_db)
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE), headless=False, locale='ru-RU',
                    viewport={'width': 1320, 'height': 900},
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    watch_rate_limit_responses(page)
                    if args.diagnose_post is not None:
                        inspect_post_only(page, args.group_id, args.diagnose_post)
                    elif args.inspect_share is not None:
                        inspect_share_only(page, args.group_id, args.inspect_share)
                    else:
                        with closing(db_open(DB_PATH)) as db:
                            if args.recheck_access is not None:
                                recheck_access_one(page, db, args.group_id, args.recheck_access)
                            else:
                                recheck_one(page, db, args.group_id, args.recheck_post,
                                            confirm_no_click_like=args.confirm_no_click_like,
                                            confirm_comment_click_like=args.confirm_comment_click_like,
                                            confirm_repost_dialog_no_submit=args.confirm_repost_dialog_no_submit)
                    return 0
                finally:
                    context.close()
        except PlatformRateLimited as exc:
            with closing(db_open(DB_PATH)) as db:
                lock_on_platform_warning(db, args.group_id, str(exc))
            print('[RATE LIMIT] Диагностика остановлена:', exc)
            return 2
        except (PWTimeout, PlaywrightError, StopRun) as exc:
            print('[ERROR] Диагностика:', str(exc)[:400])
            return 2
    if args.login_only:
        print("VK BATCH v6.1 | ручная авторизация без действий")
        print("Не передавай папку профиля, cookies, токены и HTML авторизованной страницы.")
        PROFILE.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as pw:
                return login_only(pw)
        except (PWTimeout, PlaywrightError) as exc:
            print("[ERROR] Chromium/Playwright:", str(exc)[:600])
            return 2
        except (KeyboardInterrupt, EOFError):
            print("[STOP] Вход прерван оператором. Профиль не удалён.")
            return 130
    print("VK BATCH v6.15 | один аккаунт, история публикаций группы")
    print("Внимание: массовые реакции/репосты могут привести к ограничениям VK.")
    print("Нет обхода капчи, подмены браузера, смены IP, маскировки или обхода лимитов.")
    print("Профиль локальный, не пересылай .vk-local-profile, cookies и HTML авторизованной страницы.")
    if not args.execute:
        print("[DRY] Действия отключены. Для выполнения укажи --execute.")
    PROFILE.mkdir(exist_ok=True)
    try:
        with closing(db_open(DB_PATH)) as db, sync_playwright() as pw:
            require_platform_unlocked(db)
            safety = SafetyBudget(db, args.group_id, args.max_actions_run, args.max_actions_24h) if args.execute else None
            if safety:
                print(f'[SAFETY] Лимиты: за запуск {safety.per_run} попыток; '
                      f'за 24 ч {safety.per_24h}; уже {actions_24h(db,args.group_id)} за 24 ч')
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE), headless=False, locale="ru-RU",
                viewport={"width": 1320, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                watch_rate_limit_responses(page)
                if args.audit_pending is not None:
                    if not args.skip_scan:
                        print('[AUDIT] Режим не прокручивает стену; использует только SQLite.')
                    audit_pending(page, db, args.group_id, args.audit_pending, args.pause)
                    audit_report(db, args.group_id)
                    print_report(db, args.group_id)
                    return 0
                if args.post_id is not None:
                    pid = args.post_id
                    url = POST_URL.format(group_id=args.group_id, post_id=pid)
                    print(f'[TARGET] Только {url}; общий список постов НЕ перебираем.')
                    row = target_row(db, args.group_id, pid, create=args.execute)
                    if not args.execute:
                        try:
                            go_to_post(page, args.group_id, pid)
                        except PostUnavailable as exc:
                            print(f'[TARGET] Недоступен: {exc.kind} — {exc}. Действий не было.')
                            return 0
                        except PostTemporaryError as exc:
                            print(f'[TARGET] Временная ошибка: {exc}. Действий не было.')
                            return 2
                        state = refresh_state(page, args.group_id, pid)
                        print(f'[TARGET] VK: like={state["liked"]}, '
                              f'repost={state["reposted"]} ({state["source"]})')
                        footer = post_footer_info(page, args.group_id, pid, 'like')
                        print(f'[TARGET] Основной лайк: ok={footer["ok"]}, '
                              f'liked={footer["liked"]}, visible={footer["visibleLikes"]}, '
                              f'reason={footer["reason"]}')
                        if row is not None:
                            print(f'[TARGET] SQLite: like={row["like_status"]}, '
                                  f'repost={row["repost_status"]}')
                        else:
                            print('[TARGET] Этой записи пока нет в SQLite. Только чтение.')
                        print('[TARGET] Действий не было. Для запуска добавь --execute.')
                        return 0
                    if row is None:
                        raise StopRun('Не удалось создать целевую запись SQLite.')
                    access = db.execute('SELECT state FROM post_access WHERE group_id=? AND post_id=?',
                                        (args.group_id, pid)).fetchone()
                    if access is not None:
                        raise StopRun(f'Пост {pid} пропущен ранее ({access["state"]}). '
                                      f'Для повторной проверки: --recheck-access {pid}')
                    print(f'[TARGET] SQLite: like={row["like_status"]}, '
                          f'repost={row["repost_status"]}')
                    if 'uncertain' in (row['like_status'], row['repost_status']):
                        raise StopRun('Для выбранной записи есть uncertain. '
                                      'Сначала --recheck-post ID; не повторяем клики вслепую.')
                    try:
                        process_one(page, db, args.group_id, row, args.pause, safety=safety)
                    except PostUnavailable as exc:
                        record_post_access(db, args.group_id, pid, exc.kind, str(exc))
                        print(f'[SKIP] Пост {pid}: {exc.kind} — {exc}. Реакций не было.')
                        return 0
                    except PostTemporaryError as exc:
                        record_post_access(db, args.group_id, pid, 'temporary', str(exc))
                        print(f'[SKIP] Пост {pid}: временная ошибка — {exc}. Реакций не было.')
                        return 2
                    print(f'\n[DONE] Целевой пост {pid} обработан.')
                    fresh = target_row(db, args.group_id, pid)
                    print(f'[TARGET] Итог SQLite: like={fresh["like_status"]}, '
                          f'repost={fresh["repost_status"]}')
                    return 0
                if not args.skip_scan:
                    scan_wall(page, db, args.group_id, group_url, args.scan_scrolls, args.stale_scrolls)
                pending = (get_audited_missing_tasks(db, args.group_id, args.max_posts)
                           if args.only_audited else get_tasks(db, args.group_id, args.max_posts))
                if args.only_audited:
                    print('[QUEUE] Только ранее проверенные записи с отсутствующими действиями; '
                          'перед кликом VK проверяется заново')
                print(f"[QUEUE] К обработке в этом запуске: {len(pending)}")
                if args.only_audited:
                    for row in pending[:12]:
                        print('   ', POST_URL.format(group_id=args.group_id, post_id=row['post_id']))
                print_report(db, args.group_id)
                if not args.execute:
                    print("[DRY] Для запуска на одной-двух записях: py vk_batch.py --execute --max-posts 2")
                    return 0
                if not pending:
                    print("[DONE] Необработанных записей нет.")
                    return 0
                print("[START] Без подтверждений по каждому посту. Остановить — Ctrl+C.")
                skipped_unavailable = skipped_temporary = consecutive_temporary = 0
                for idx, row in enumerate(pending, 1):
                    print(f"[{idx}/{len(pending)}]", end=" ", flush=True)
                    try:
                        process_one(page, db, args.group_id, row, args.pause, safety=safety)
                    except PostUnavailable as exc:
                        record_post_access(db, args.group_id, row['post_id'], exc.kind, str(exc))
                        skipped_unavailable += 1
                        consecutive_temporary = 0
                        print(f'   [SKIP] {row["post_id"]}: {exc.kind} — {exc}. Следующий пост.')
                        continue
                    except PostTemporaryError as exc:
                        record_post_access(db, args.group_id, row['post_id'], 'temporary', str(exc))
                        skipped_temporary += 1
                        consecutive_temporary += 1
                        print(f'   [SKIP] {row["post_id"]}: временная ошибка — {exc}')
                        if consecutive_temporary >= MAX_CONSECUTIVE_TEMPORARY:
                            raise StopRun('Два последовательных сбоя загрузки VK; '
                                          'останавливаем очередь, чтобы не пропустить весь архив.') from exc
                        continue
                    consecutive_temporary = 0
                print(f"\n[DONE] Очередь обработана; пропущено недоступных={skipped_unavailable}, "
                      f"временных ошибок={skipped_temporary}.")
                print_report(db, args.group_id)
                return 0
            finally:
                context.close()
    except PlatformRateLimited as exc:
        with closing(db_open(DB_PATH)) as db:
            lock_on_platform_warning(db, args.group_id, str(exc))
        print('\n[RATE LIMIT]', exc)
        print('[SAFETY] Ограничение VK сохранено в SQLite для всего аккаунта. '
              'Сканирование, аудит и действия заблокированы до ручной проверки.')
        return 2
    except SafetyLimitReached as exc:
        print('\n[SAFETY PAUSE]', exc)
        print('Никаких дополнительных кликов не было; незавершённые действия остаются в очереди.')
        return 0
    except StopRun as exc:
        # Make account-verification/anti-spam signals sticky between restarts.
        reason = str(exc)
        if args.execute and any(x in reason.lower() for x in
                                ('vk запросил вход', 'vk сообщил об ограничении',
                                 'появилась captcha', 'подтвердите, что вы не робот',
                                 'подозрительная активность')):
            with closing(db_open(DB_PATH)) as safety_db:
                lock_on_platform_warning(safety_db, args.group_id, reason)
            print('[SAFETY] Предупреждение сохранено. Следующие действия заблокированы '
                  'до ручной проверки аккаунта.')
        print("\n[STOP]", exc)
        print("Исправь причину, проверь состояние в VK. Не повторяй подозрительный клик вслепую.")
        return 2
    except (PWTimeout, PlaywrightError) as exc:
        print("\n[ERROR] VK/Playwright:", str(exc)[:450])
        return 2
    except KeyboardInterrupt:
        print("\n[STOP] Остановлено Ctrl+C. Пройденные результаты сохранены в SQLite.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
