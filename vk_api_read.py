#!/usr/bin/env python3
"""VK public wall read-only API client and resumable weekly discovery.

Tokens are only transmitted as HTTPS POST form body.  SQLite contains no
access tokens, browser cookies, full post bodies, or mutable engagement counts.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

import vk_batch as engine
import vk_groups as groups

API_VERSION = '5.199'
API_URL = 'https://api.vk.com/method/wall.get'
RATE_CODES = {6, 9, 29}
API_SOURCE = 'vk-api-wall.get'
PAGE_SIZE = 100
PARTIAL_RETRY_LATER = {'invalid-items', 'ordering-anomaly', 'total-changed',
                       'repeated-items', 'unexpected-short-page'}


class VkApiFailure(Exception):
    """Read operation stopped without retry."""


class VkApiLimited(VkApiFailure):
    """VK or HTTP limited the client; persist an account-wide lock."""


class VkApiAuthFailure(VkApiFailure):
    """VK did not authorize wall.get for this app or community."""


def init_api(db) -> None:
    """Only additive, idempotent migration; v6.17 scans are *not* presumed complete."""
    db.execute('''CREATE TABLE IF NOT EXISTS api_read_attempts (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       group_id INTEGER NOT NULL,
       attempted_at TEXT NOT NULL
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS api_group_scan_progress (
       group_id INTEGER PRIMARY KEY,
       last_scanned_at TEXT NOT NULL,
       discovered INTEGER NOT NULL,
       status TEXT NOT NULL DEFAULT 'partial',
       last_attempt_at TEXT,
       pages INTEGER NOT NULL DEFAULT 0,
       recent INTEGER NOT NULL DEFAULT 0,
       next_offset INTEGER NOT NULL DEFAULT 0,
       snapshot_count INTEGER,
       scan_cutoff INTEGER,
       last_nonpinned_at INTEGER,
       reason TEXT NOT NULL DEFAULT 'legacy-unverified'
    )''')
    existing = {r['name'] for r in db.execute('PRAGMA table_info(api_group_scan_progress)')}
    additional = {
        'status': "TEXT NOT NULL DEFAULT 'partial'",
        'last_attempt_at': 'TEXT',
        'pages': 'INTEGER NOT NULL DEFAULT 0',
        'recent': 'INTEGER NOT NULL DEFAULT 0',
        'next_offset': 'INTEGER NOT NULL DEFAULT 0',
        'snapshot_count': 'INTEGER',
        'scan_cutoff': 'INTEGER',
        'last_nonpinned_at': 'INTEGER',
        'reason': "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    }
    for name, definition in additional.items():
        if name not in existing:
            db.execute(f'ALTER TABLE api_group_scan_progress ADD COLUMN {name} {definition}')
    db.execute('''CREATE TABLE IF NOT EXISTS api_seen_posts (
       group_id INTEGER NOT NULL,
       post_id INTEGER NOT NULL,
       published_at INTEGER NOT NULL,
       seen_at TEXT NOT NULL,
       valid INTEGER NOT NULL DEFAULT 1,
       PRIMARY KEY(group_id,post_id)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS api_scan_seen (
       group_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
       PRIMARY KEY(group_id,post_id)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS read_navigation_pacing (
       id INTEGER PRIMARY KEY CHECK(id=0),
       last_navigation_at REAL NOT NULL
    )''')
    # v6.17 stored API provenance only in post_publication.source.
    # Backfill it without touching existing like/repost statuses or timestamps.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_publication'").fetchone():
        db.execute('''INSERT OR IGNORE INTO api_seen_posts
           (group_id,post_id,published_at,seen_at)
           SELECT group_id,post_id,published_at,checked_at FROM post_publication
           WHERE exact=1 AND source=? AND published_at IS NOT NULL''', (API_SOURCE,))
    db.commit()


def _progress(db, group_id: int):
    return db.execute('SELECT * FROM api_group_scan_progress WHERE group_id=?',
                      (group_id,)).fetchone()


def choose_api_scan_groups(db, selected: list[dict], limit: int, *,
                           min_interval_hours: float = 12.0) -> list[dict]:
    """Unscanned first, then incomplete (resume), then stale/failed scans.

    Recent complete scans and recently failed scans are not retried. Partials
    caused *only* by budget exhaustion can resume immediately on next run.
    """
    init_api(db)
    if limit <= 0:
        return []
    state = {r['group_id']: r for r in db.execute('SELECT * FROM api_group_scan_progress')}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_interval_hours)
    due = []
    for g in selected:
        row = state.get(g['id'])
        if row is None:
            due.append((0, '', g))
            continue
        stamp_str = row['last_attempt_at'] or row['last_scanned_at']
        try:
            stamp = datetime.fromisoformat(stamp_str)
            fresh = stamp.tzinfo is not None and stamp > cutoff
        except (ValueError, TypeError):
            fresh = False
        status = row['status']
        if status == 'complete' and fresh:
            continue
        if (status == 'error' or row['reason'] in PARTIAL_RETRY_LATER) and fresh:
            continue
        priority = 1 if status == 'partial' else 2
        due.append((priority, stamp_str or '', g))
    return [g for _, _, g in sorted(due, key=lambda x: (x[0], x[1], x[2]['id']))[:limit]]


def _save_progress(db, gid: int, *, status: str, fetched: int, recent: int,
                   pages: int, offset: int, total: int | None, cutoff: int,
                   last_nonpinned: int | None, reason: str) -> None:
    if status not in {'complete', 'partial', 'error'}:
        raise ValueError('Bad API scan state')
    at = engine.moment()
    db.execute('''INSERT INTO api_group_scan_progress
        (group_id,last_scanned_at,discovered,status,last_attempt_at,pages,recent,
         next_offset,snapshot_count,scan_cutoff,last_nonpinned_at,reason)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(group_id) DO UPDATE SET
         last_scanned_at=excluded.last_scanned_at,
         discovered=excluded.discovered,status=excluded.status,
         last_attempt_at=excluded.last_attempt_at,pages=excluded.pages,
         recent=excluded.recent,next_offset=excluded.next_offset,
         snapshot_count=excluded.snapshot_count,scan_cutoff=excluded.scan_cutoff,
         last_nonpinned_at=excluded.last_nonpinned_at,reason=excluded.reason''',
        (gid,at,fetched,status,at,pages,recent,offset,total,cutoff,last_nonpinned,reason))
    db.commit()


def mark_api_group_scanned(db, group_id: int, discovered: int) -> None:
    """Legacy helper used by v6.17 callers, intentionally conservative."""
    init_api(db)
    _save_progress(db, group_id, status='partial', fetched=discovered, recent=0,
                   pages=0, offset=0, total=None, cutoff=0,
                   last_nonpinned=None, reason='legacy-unverified')


def mark_api_group_error(db, gid: int, error: str) -> None:
    """Expose sanitized diagnostic without mistaking a failure for a scan."""
    init_api(db)
    previous = _progress(db, gid)
    _save_progress(db, gid, status='error',
                   fetched=previous['discovered'] if previous else 0,
                   recent=previous['recent'] if previous else 0,
                   pages=previous['pages'] if previous else 0,
                   offset=0, total=None, cutoff=0,last_nonpinned=None,
                   reason=str(error)[:140])


def api_scan_report(db, selected: list[dict], *,
                    min_interval_hours: float = 12.0, details: bool = False) -> dict:
    """Local-only 57-group coverage report. Never calls the VK network."""
    init_api(db)
    rows = {r['group_id']: r for r in db.execute('SELECT * FROM api_group_scan_progress')}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=min_interval_hours)
    counts = {'complete': 0, 'stale': 0, 'partial': 0, 'error': 0, 'not_checked': 0}
    lines = []
    for g in selected:
        row = rows.get(g['id'])
        if row is None:
            status, info = 'not_checked', 'ещё не запрашивалась через API'
        else:
            try:
                scanned = datetime.fromisoformat(row['last_attempt_at'] or row['last_scanned_at'])
                fresh = scanned.tzinfo is not None and scanned >= cutoff
            except (ValueError, TypeError):
                fresh = False
            status = row['status'] if row['status'] in ('complete','partial','error') else 'partial'
            if status == 'complete' and not fresh:
                status = 'stale'
            at = row['last_attempt_at'] or row['last_scanned_at']
            info = (f'запрос={at} стр.={row["pages"]}, '
                    f'за окно={row["recent"]}, причина={row["reason"]}')
        counts[status] += 1
        lines.append((g['id'], status, info, g['name']))
    print(f'[API REPORT] Всего групп: {len(selected)}; полных свежих={counts["complete"]}, '
          f'устаревших={counts["stale"]}, частичных={counts["partial"]}, '
          f'с ошибкой={counts["error"]}, не проверены={counts["not_checked"]}')
    if details:
        for gid,status,info,name in lines:
            print(f'   {gid} | {status:<11} | {name[:38]} | {info}')
    return counts


def _default_transport(params: dict) -> tuple[int, dict]:
    body = urllib.parse.urlencode(params).encode('utf-8')
    request = urllib.request.Request(API_URL, data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'User-Agent': 'VK-public-wall-read-client/1.0'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.getcode()
            content = response.read(8_000_001)
            if len(content) > 8_000_000:
                raise VkApiFailure('VK API: ответ превысил локальный предел 8 МБ')
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise VkApiLimited('VK API: HTTP 429') from None
        raise VkApiFailure(f'VK API: HTTP {exc.code} (без повтора)') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise VkApiFailure('VK API: сетевая ошибка или тайм-аут (без повтора)') from None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        raise VkApiFailure('VK API: ответ не является корректным JSON') from None
    if not isinstance(data, dict):
        raise VkApiFailure('VK API: неожиданный формат JSON')
    return status, data


class VKReadClient:
    """Bounded public wall reader with account-wide lock and request ledger."""
    def __init__(self, db, token: str, *, pause_seconds: float = 20.0,
                 per_run: int = 3, per_24h: int = 30,
                 transport: Callable[[dict], tuple[int,dict]] | None = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        if not token or not token.strip():
            raise VkApiAuthFailure('Нет локального VK_API_TOKEN для официального API')
        if not 5 <= pause_seconds <= 300 or not 1 <= per_run <= 25 or not 1 <= per_24h <= 100:
            raise ValueError('Недопустимые значения защитного бюджета чтения')
        self.db = db
        self.token = token.strip()
        self.pause = pause_seconds
        self.per_run = per_run
        self.per_24h = per_24h
        self.transport = transport or _default_transport
        self.sleep = sleeper
        self.clock = clock
        self.used = 0
        init_api(db)

    def remaining(self) -> int:
        now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        cutoff = (now - timedelta(hours=24)).isoformat(timespec='seconds')
        used_24h = self.db.execute(
            'SELECT COUNT(*) FROM api_read_attempts WHERE attempted_at >= ?',
            (cutoff,)).fetchone()[0]
        return max(0, min(self.per_run-self.used, self.per_24h-used_24h))

    def _reserve(self, group_id: int) -> None:
        engine.require_platform_unlocked(self.db)
        if self.remaining() < 1:
            raise VkApiFailure('Локальный бюджет запросов API исчерпан. Новые запросы не отправлялись.')
        previous = self.db.execute(
            'SELECT last_navigation_at FROM read_navigation_pacing WHERE id=0').fetchone()
        now = self.clock()
        if previous:
            seconds = self.pause-(now-previous['last_navigation_at'])
            if seconds > 0:
                print(f'[API PAUSE] {seconds:.1f} с перед запросом VK API')
                self.sleep(seconds)
        engine.require_platform_unlocked(self.db)
        at = datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat(timespec='seconds')
        self.db.execute('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES (?,?)',
                        (group_id,at))
        self.db.execute('''INSERT INTO read_navigation_pacing(id,last_navigation_at) VALUES (0,?)
            ON CONFLICT(id) DO UPDATE SET last_navigation_at=excluded.last_navigation_at''',
            (self.clock(),))
        self.db.commit()
        self.used += 1

    def wall_get(self, group_id: int, *, count: int = PAGE_SIZE, offset: int = 0) -> dict:
        if type(group_id) is not int or group_id <= 0 or not 1 <= count <= PAGE_SIZE or offset < 0:
            raise ValueError('Неверный group_id/count/offset')
        self._reserve(group_id)
        try:
            status,payload = self.transport({
                'owner_id': -group_id,'filter':'owner','count':count,
                'offset':offset,'access_token':self.token,'v':API_VERSION})
            if status == 429:
                raise VkApiLimited('VK API: HTTP 429')
            if status != 200:
                raise VkApiFailure(f'VK API: HTTP {status}')
            if not isinstance(payload,dict):
                raise VkApiFailure('VK API: неожиданный ответ')
            if 'error' in payload:
                error = payload['error'] if isinstance(payload['error'],dict) else {}
                code = error.get('error_code')
                if code in RATE_CODES:
                    raise VkApiLimited(f'VK API: ограничение запросов, код {code}')
                if code in (5,7,28):
                    raise VkApiAuthFailure(f'VK API: ошибка авторизации/прав доступа, код {code}')
                raise VkApiFailure(f'VK API: ошибка метода wall.get, код {code}')
            result = payload.get('response')
            if not isinstance(result,dict) or not isinstance(result.get('items'),list):
                raise VkApiFailure('VK API: response.items отсутствует')
            return result
        except VkApiLimited as exc:
            engine.lock_on_platform_warning(self.db,0,str(exc))
            raise


def _pinned(item: dict) -> bool | None:
    value = item.get('is_pinned',0)
    if value in (0,False):
        return False
    if value in (1,True):
        return True
    return None


def _valid_post(item, gid: int, now: int) -> bool:
    return (isinstance(item,dict) and type(item.get('owner_id')) is int
            and item['owner_id']==-gid
            and type(item.get('id')) is int and item['id']>0
            and type(item.get('date')) is int
            and 1200000000<=item['date']<=now+3600
            and _pinned(item) is not None)


def scan_one_group(db, client: VKReadClient, group: dict, *, days: int = 7,
                   max_pages: int = 1) -> dict:
    """Get enough wall.get pages to prove weekly coverage or mark it partial.

    Full = short final page / API total exhausted / chronologically older
    *unpinned* post encountered.  An old pinned post alone is not a boundary.
    Partial checkpoints preserve next offset. A changed total invalidates the
    checkpoint (offset pagination can shift when new posts arrive).
    """
    if not 1<=max_pages<=10 or not 1<=days<=365:
        raise ValueError('max_pages 1..10, days 1..365')
    init_api(db)
    gid = group['id']
    now = int(datetime.now(timezone.utc).timestamp())
    previous = _progress(db,gid)
    can_resume = bool(previous and previous['status']=='partial' and
                      previous['next_offset']>0 and
                      previous['scan_cutoff'] and
                      previous['scan_cutoff']>=now-days*86400-86400 and
                      previous['reason'] not in PARTIAL_RETRY_LATER)
    if can_resume:
        offset = previous['next_offset']
        cutoff = previous['scan_cutoff']
        fetched_total = previous['discovered']
        recent_total = previous['recent']
        pages_total = previous['pages']
        original_count = previous['snapshot_count']
        last_nonpinned = previous['last_nonpinned_at']
    else:
        offset, fetched_total, recent_total, pages_total = 0,0,0,0
        cutoff = now-days*86400
        original_count = None
        last_nonpinned = None
    if not can_resume:
        db.execute('DELETE FROM api_scan_seen WHERE group_id=?',(gid,))
        db.commit()
    fetched_here=0
    pages_here=0
    result_status='partial'
    reason='more-pages'
    while pages_here<max_pages:
        if client.remaining()<=0:
            reason='request-budget'
            break
        payload = client.wall_get(gid,count=PAGE_SIZE,offset=offset)
        items=payload['items']
        count=payload.get('count')
        if count is not None and (type(count) is not int or count<0):
            reason='invalid-items'
            break
        if original_count is not None and count is not None and count!=original_count:
            # Existing continuation is unsafe. Restart on a *future* run, no
            # extra requests here and no claim of completed archive coverage.
            _save_progress(db,gid,status='partial',fetched=0,recent=0,pages=0,
                           offset=0,total=None,cutoff=0,last_nonpinned=None,
                           reason='total-changed')
            return {'group_id':gid,'fetched':0,'recent':0,'pages':pages_here+1,
                    'status':'partial','reason':'total-changed',
                    'possibly_incomplete':True,'saved_token':False,
                    'next_offset':0}
        if original_count is None:
            original_count=count
        pages_here+=1
        ids=[]
        dates=[]
        invalid=False
        ordering_problem=False
        repeated=False
        crossed_boundary=False
        local_seen=set()
        last_local=last_nonpinned
        for item in items:
            if not _valid_post(item,gid,now):
                invalid=True
                continue
            pid,ts=item['id'],item['date']
            if pid in local_seen:
                repeated=True
                continue
            local_seen.add(pid)
            if not _pinned(item):
                if last_local is not None and ts>last_local:
                    ordering_problem=True
                last_local=ts
                if ts<cutoff:
                    crossed_boundary=True
            ids.append(pid)
            dates.append((pid,ts))
        if ids:
            placeholders=','.join('?' for _ in ids)
            reused=db.execute(f'''SELECT 1 FROM api_scan_seen
                WHERE group_id=? AND post_id IN ({placeholders}) LIMIT 1''',
                (gid,*ids)).fetchone()
            if reused:
                repeated=True
            db.executemany('INSERT OR IGNORE INTO api_scan_seen(group_id,post_id) VALUES(?,?)',
                           [(gid,pid) for pid in ids])
            db.commit()
        engine.add_posts(db,gid,ids)
        stamp=engine.moment()
        db.executemany('''INSERT INTO api_seen_posts
            (group_id,post_id,published_at,seen_at) VALUES (?,?,?,?)
            ON CONFLICT(group_id,post_id) DO UPDATE SET
                valid=CASE WHEN api_seen_posts.published_at=excluded.published_at
                      THEN api_seen_posts.valid ELSE 0 END,
                seen_at=excluded.seen_at''',
            [(gid,pid,ts,stamp) for pid,ts in dates])
        db.commit()
        for pid,ts in dates:
            groups.save_date(db,gid,pid,ts,API_SOURCE,True,date_checked=True)
        fresh=sum(1 for _,ts in dates if ts>=cutoff)
        fetched_here+=len(dates)
        fetched_total+=len(dates)
        recent_total+=fresh
        pages_total+=1
        offset+=len(items)
        last_nonpinned=last_local
        if invalid:
            reason='invalid-items'
            break
        if repeated:
            reason='repeated-items'
            break
        if ordering_problem:
            reason='ordering-anomaly'
            break
        if count is not None and offset>count:
            reason='unexpected-short-page'
            break
        if len(items)<PAGE_SIZE and count is not None and offset<count:
            reason='unexpected-short-page'
            break
        if len(items)<PAGE_SIZE or (count is not None and offset>=count):
            result_status,reason='complete','end-of-wall'
            break
        if crossed_boundary:
            result_status,reason='complete','older-unpinned'
            break
        # Full fresh page => request next offset, up to the local budget.
        _save_progress(db,gid,status='partial',fetched=fetched_total,recent=recent_total,
                       pages=pages_total,offset=offset,total=original_count,
                       cutoff=cutoff,last_nonpinned=last_nonpinned,
                       reason='more-pages')
    else:
        reason='page-cap'
    if reason in PARTIAL_RETRY_LATER:
        offset=0  # Do not resume a malformed/non-chronological snapshot.
    _save_progress(db,gid,status=result_status,fetched=fetched_total,recent=recent_total,
                   pages=pages_total,offset=0 if result_status=='complete' else offset,
                   total=original_count,cutoff=cutoff,last_nonpinned=last_nonpinned,
                   reason=reason)
    return {'group_id':gid,'fetched':fetched_here,'recent':recent_total,
            'pages':pages_here,'status':result_status,'reason':reason,
            'possibly_incomplete':result_status!='complete',
            'saved_token':False,'next_offset':offset if result_status!='complete' else 0}


def configured_token() -> str | None:
    return os.environ.get('VK_API_TOKEN','').strip() or None
