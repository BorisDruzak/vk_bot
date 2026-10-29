#!/usr/bin/env python3
"""VK public wall read-only API client and resumable weekly discovery.

Tokens are only transmitted as HTTPS POST form body. SQLite contains public
IDs/dates and timestamped nullable engagement observations requested by the
operator; never access tokens, browser cookies, or full post bodies.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import vk_batch as engine
import vk_groups as groups
import vk_read_trace as read_trace

API_VERSION = '5.199'
API_URL = 'https://api.vk.com/method/wall.get'
_READ_METHOD_URLS = {'wall.get':API_URL,
                    'users.get':'https://api.vk.com/method/users.get'}
RATE_CODES = {6, 9, 29}
API_SOURCE = 'vk-api-wall.get'
PAGE_SIZE = 100
PARTIAL_RETRY_LATER = {'invalid-items', 'ordering-anomaly', 'total-changed',
                       'repeated-items', 'unexpected-short-page'}


class VkApiFailure(Exception):
    """Read operation stopped without retry."""
    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class VkApiLimited(VkApiFailure):
    """VK protection requires a durable account-wide stop."""


class VkApiProtection(VkApiLimited):
    """CAPTCHA or validation required; never continue with another source."""


class VkApiAuthFailure(VkApiFailure):
    """VK did not authorize wall.get for this app or community."""


class VkApiTransientRead(VkApiFailure):
    """Read-only transport failure; never an authorization or rate-limit signal."""


class VkApiReadDeadline(VkApiTransientRead):
    """The bounded public HTTP helper was reaped; no response may be reused."""


_TRANSPORT_SCRIPT=(
    'import sys,time;_t=time.monotonic_ns();'
    'sys.stderr.write(\'VKREAD {"version":1,"phase":"helper_imports","state":"started","tick_ns":\'+str(_t)+\'}\\n\');'
    'sys.stderr.flush();import vk_api_read;vk_api_read._transport_stdio(import_started_ns=_t)')
_HTTP_TRACE=None  # Set only inside the disposable HTTP-only helper.


class _ReadResult(tuple):
    """Two-value transport protocol; telemetry is never inserted into VK JSON."""
    def __new__(cls,status,payload,detail):
        value=super().__new__(cls,(status,payload))
        value.read_diagnostics=read_trace.sanitize_summary(detail)
        return value


def bounded_transport(params,*,timeout=20,diagnostic=None):
    """One public read under a total wall-clock bound, including slow streams.

    The helper does HTTP only: it cannot touch SQLite, profiles or action state.
    Its token travels over stdin, never argv/environment/logs. The existing
    VKReadClient owns pacing, reservation, guards and PublicPages projection.
    """
    executable=Path(sys.executable)
    if executable.name.lower()=='pythonw.exe':executable=executable.with_name('python.exe')
    import vk_diagnostics as diagnostics
    started=time.monotonic_ns();read_id=uuid.uuid4().hex
    context={'read_id':read_id,'timeout_ms':max(0,int(timeout*1000))}
    owner=params.get('owner_id') if isinstance(params,dict) else None
    if type(owner) is int and 0<abs(owner)<=2**53-1:context['source_group']=abs(owner)
    # Emit only after transport returns: a slow diagnostic DB write must never
    # postpone HTTP after VKReadClient's final deadline/stop check and reserve.
    def detail(raw,kind=None):
        return {**read_trace.summarize(raw,started,read_error=kind),
                'read_id':read_id,'timeout_ms':context['timeout_ms']}
    def failed(exc,raw,kind=None,**extra):
        exc.read_diagnostics={**detail(raw,kind),**read_trace.sanitize_summary(extra)}
        diagnostics.emit(diagnostic,'public_read_failed',**{**context,**exc.read_diagnostics})
        return exc
    try:
        result=subprocess.run([str(executable),'-X','utf8','-c',_TRANSPORT_SCRIPT],
            input=json.dumps(params).encode('utf-8'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parent,timeout=timeout,
            env={k:v for k,v in os.environ.items() if k.upper() not in ('VK_API_TOKEN','VK_API_TOKEN_FILE')},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except subprocess.TimeoutExpired as exc:
        # run() kills and waits for this read-only helper; never the worker.
        raise failed(VkApiReadDeadline('Public read deadline'),exc.stderr,'helper_deadline') from None
    except OSError as exc:
        error=failed(VkApiFailure('Public read helper unavailable'),None,'helper_start_failed',
            **{k:v for k,v in read_trace.classify_error(exc).items() if k!='read_error'})
        raise error from None
    stderr=getattr(result,'stderr',None)
    if result.returncode or len(result.stdout)>16_000_000:
        error=failed(VkApiFailure('Invalid public read helper result'),stderr,'helper_exit' if result.returncode else 'invalid_helper_result',
            read_exit_code=result.returncode)
        raise error
    try:payload=json.loads(result.stdout)
    except (ValueError,TypeError):raise failed(VkApiFailure('Invalid public read helper result'),stderr,'invalid_helper_result') from None
    if type(payload) is not dict:raise failed(VkApiFailure('Invalid public read helper result'),stderr,'invalid_helper_result')
    error=payload.get('error')
    if error=='limited':raise failed(VkApiLimited('Public API limit',code=payload.get('code')),stderr)
    if error=='transient':raise failed(VkApiTransientRead('Public API transport failure'),stderr)
    if error or type(payload.get('status')) is not int or not isinstance(payload.get('payload'),dict):
        raise failed(VkApiFailure('Public API read failed'),stderr)
    summary=detail(stderr);summary['http_status']=payload['status']
    api_error=payload['payload'].get('error')
    if isinstance(api_error,dict) and type(api_error.get('error_code')) is int:
        code=api_error['error_code']
        summary.update(read_error='vk_access_denied' if code in (5,7,28) else 'vk_rate_limited' if code in RATE_CODES else 'vk_api_error',vk_error_code=code)
    diagnostics.emit(diagnostic,'public_read_finished',**{**context,**summary})
    return _ReadResult(payload['status'],payload['payload'],summary)


def _transport_stdio(*,import_started_ns=None):
    global _HTTP_TRACE
    trace=read_trace.ReadTrace(sys.stderr);_HTTP_TRACE=trace
    if type(import_started_ns) is int:
        trace.mark('helper_imports','completed',duration_ms=max(0,(time.monotonic_ns()-import_started_ns)//1_000_000))
    try:
        with trace.span('input_read'):params=json.loads(sys.stdin.buffer.read(65536))
        with trace.observe_network():status,payload=_default_transport(params)
        result={'status':status,'payload':payload}
        trace.mark(trace.last['phase'] if trace.last else 'http_open','completed',http_status=status)
    except VkApiLimited as exc:trace.failure(exc);result={'error':'limited','code':exc.code}
    except VkApiTransientRead as exc:trace.failure(exc);result={'error':'transient'}
    except Exception as exc:trace.failure(exc);result={'error':'failed'}
    finally:_HTTP_TRACE=None
    sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))


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
    db.execute('''CREATE TABLE IF NOT EXISTS api_post_engagement (
       group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,
       like_count INTEGER CHECK(like_count IS NULL OR
          (typeof(like_count)='integer' AND like_count>=0)),
       repost_count INTEGER CHECK(repost_count IS NULL OR
          (typeof(repost_count)='integer' AND repost_count>=0)),
       observed_at TEXT NOT NULL,
       PRIMARY KEY(group_id,post_id)
    )''')
    if 'comment_count' not in {r['name'] for r in db.execute('PRAGMA table_info(api_post_engagement)')}:
        db.execute('ALTER TABLE api_post_engagement ADD COLUMN comment_count INTEGER '
                   'CHECK(comment_count IS NULL OR (typeof(comment_count)=\'integer\' AND comment_count>=0))')
    if 'view_count' not in {r['name'] for r in db.execute('PRAGMA table_info(api_post_engagement)')}:
        db.execute('ALTER TABLE api_post_engagement ADD COLUMN view_count INTEGER '
                   'CHECK(view_count IS NULL OR (typeof(view_count)=\'integer\' AND view_count>=0))')
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
    if not isinstance(params,dict):raise ValueError('Invalid public read parameters')
    supplied=dict(params);method=supplied.pop('_read_method','wall.get')
    if type(method) is not str or method not in _READ_METHOD_URLS:
        raise ValueError('Unsupported public read method')
    body = urllib.parse.urlencode(supplied).encode('utf-8')
    endpoint=API_URL if method=='wall.get' else _READ_METHOD_URLS[method]
    request = urllib.request.Request(endpoint, data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'User-Agent': 'VK-public-wall-read-client/1.0'}, method='POST')
    try:
        if _HTTP_TRACE:_HTTP_TRACE.mark('http_open','started')
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.getcode()
            if _HTTP_TRACE:_HTTP_TRACE.mark('response_headers','completed',http_status=status)
            with (_HTTP_TRACE.span('response_body') if _HTTP_TRACE else nullcontext()):
                content = response.read(8_000_001)
            if _HTTP_TRACE:_HTTP_TRACE.mark('response_body','completed',bytes_received=len(content))
            if len(content) > 8_000_000:
                if _HTTP_TRACE:_HTTP_TRACE.mark('response_body','failed',read_error='response_too_large',bytes_received=len(content))
                raise VkApiFailure('VK API: ответ превысил локальный предел 8 МБ')
    except urllib.error.HTTPError as exc:
        if _HTTP_TRACE:_HTTP_TRACE.failure(exc)
        if exc.code == 429:
            raise VkApiLimited('VK API: HTTP 429') from None
        if exc.code in (500,502,503,504):
            raise VkApiTransientRead('VK API: temporary read transport failure') from None
        raise VkApiFailure(f'VK API: HTTP {exc.code} (без повтора)') from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if _HTTP_TRACE:_HTTP_TRACE.failure(exc)
        raise VkApiTransientRead('VK API: сетевая ошибка или тайм-аут (без повтора в запросе)') from None
    try:
        with (_HTTP_TRACE.span('json_parse') if _HTTP_TRACE else nullcontext()):data = json.loads(content)
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
                 clock: Callable[[], float] = time.time,
                 shared_path=None, max_age_seconds: float = 300,
                 fresh_after=None, before_request=None):
        if not token or not token.strip():
            raise VkApiAuthFailure('Нет локального VK_API_TOKEN для официального API')
        if not 5 <= pause_seconds <= 300 or not 1 <= per_run <= 25 or not 1 <= per_24h <= 5000:
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
        self.pages = None
        self.max_age_seconds = max_age_seconds
        self.fresh_after = fresh_after
        self.before_request = before_request
        if shared_path is None:
            filename = next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'), '')
            marker = Path(filename).parent/'.vk-public-data.json' if filename else None
            if marker is not None and marker.is_file():
                try:
                    config = json.loads(marker.read_text(encoding='utf-8'))
                    if (type(config) is not dict or type(config.get('version')) is not int
                            or config['version'] != 1 or type(config.get('enabled')) is not bool):
                        raise ValueError()
                except (OSError, ValueError, TypeError):
                    raise ValueError('Invalid shared public data configuration') from None
                if config['enabled']:
                    shared_path = marker.with_name('vk_public_data.sqlite3')
        if shared_path is not None and shared_path is not False:
            from vk_public_data import PublicPages
            self.pages = PublicPages(shared_path)

    def has_cached_wall(self, group_id: int, *, count: int = PAGE_SIZE, offset: int = 0) -> bool:
        engine.require_platform_unlocked(self.db)
        return self.pages is not None and self.pages.lookup(group_id,count,offset,
            max_age_seconds=self.max_age_seconds, fresh_after=self.fresh_after,
            clock=self.clock) is not None

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
        if self.before_request is not None:
            self.before_request()
        at = datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat(timespec='seconds')
        self.db.execute('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES (?,?)',
                        (group_id,at))
        self.db.execute('''INSERT INTO read_navigation_pacing(id,last_navigation_at) VALUES (0,?)
            ON CONFLICT(id) DO UPDATE SET last_navigation_at=excluded.last_navigation_at''',
            (self.clock(),))
        self.db.commit()
        self.used += 1

    def wall_get(self, group_id: int, *, count: int = PAGE_SIZE, offset: int = 0) -> dict:
        if (type(group_id) is not int or group_id <= 0 or type(count) is not int
                or not 1 <= count <= PAGE_SIZE or type(offset) is not int or offset < 0):
            raise ValueError('Неверный group_id/count/offset')
        engine.require_platform_unlocked(self.db)
        if self.pages is None:
            return self._wall_get_network(group_id,count=count,offset=offset)
        result = self.pages.get(group_id,count,offset,
            lambda: self._wall_get_network(group_id,count=count,offset=offset),
            max_age_seconds=self.max_age_seconds,fresh_after=self.fresh_after,clock=self.clock)
        if '_observed_at' in result:
            project_public_page(self.db,group_id,result,now=int(self.clock()))
        return result

    def resolve_screen_name(self, alias: str) -> int:
        """Resolve a fresh own-header alias to a user ID; never a wall cache read.

        The caller owns the common read lease and supplies bounded transport.
        No alias or response is persisted. This mapping alone is not login proof;
        the caller must recheck the exact own-header alias in the same profile.
        """
        if (type(alias) is not str or
                re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,63}',alias) is None):
            raise ValueError('Invalid own-profile alias')
        self._reserve(0)
        read_result=None
        try:
            read_result=self.transport({'_read_method':'users.get',
                'user_ids':alias,'fields':'screen_name','access_token':self.token,'v':API_VERSION})
            status,payload=read_result
            if status==429:raise VkApiLimited('VK API: HTTP 429')
            if status!=200:raise VkApiFailure(f'VK API: HTTP {status}')
            if not isinstance(payload,dict):raise VkApiFailure('VK API: unexpected identity response')
            if 'error' in payload:
                error=payload['error'] if isinstance(payload['error'],dict) else {}
                code=error.get('error_code')
                if type(code) is not int:raise VkApiFailure('VK API: invalid identity error code')
                if code in RATE_CODES:raise VkApiLimited('VK API: read rate limit',code=code)
                if code in (14,17):raise VkApiProtection('VK API: read protection required',code=code)
                if code in (5,7,28,1051):raise VkApiAuthFailure('VK API: identity read authorization failed',code=code)
                raise VkApiFailure('VK API: identity method failed',code=code)
            result=payload.get('response')
            if not isinstance(result,list) or len(result)!=1 or not isinstance(result[0],dict):
                raise VkApiFailure('VK API: exact user identity unavailable')
            user=result[0];screen_name=user.get('screen_name')
            if (type(user.get('id')) is not int or not 0<user['id']<=2**53-1 or
                    user.get('type','user')!='user' or 'deactivated' in user or
                    type(screen_name) is not str or not screen_name.isascii() or
                    screen_name.casefold()!=alias.casefold()):
                raise VkApiFailure('VK API: exact user identity unavailable')
            return user['id']
        except VkApiFailure as exc:
            if not getattr(exc,'read_diagnostics',None):
                detail=read_trace.sanitize_summary(getattr(read_result,'read_diagnostics',None))
                if detail:exc.read_diagnostics=detail
            if isinstance(exc,VkApiLimited):engine.lock_on_platform_warning(self.db,0,str(exc))
            raise

    def _wall_get_network(self, group_id: int, *, count: int, offset: int) -> dict:
        self._reserve(group_id)
        read_result=None
        def attach_read(exc):
            if not getattr(exc,'read_diagnostics',None):
                detail=read_trace.sanitize_summary(getattr(read_result,'read_diagnostics',None))
                if detail:exc.read_diagnostics=detail
        try:
            read_result = self.transport({
                'owner_id': -group_id,'filter':'owner','count':count,
                'offset':offset,'access_token':self.token,'v':API_VERSION})
            status,payload=read_result
            if status == 429:
                raise VkApiLimited('VK API: HTTP 429')
            if status != 200:
                raise VkApiFailure(f'VK API: HTTP {status}')
            if not isinstance(payload,dict):
                raise VkApiFailure('VK API: неожиданный ответ')
            if 'error' in payload:
                error = payload['error'] if isinstance(payload['error'],dict) else {}
                code = error.get('error_code')
                if type(code) is not int:
                    raise VkApiFailure('VK API: некорректный код ошибки')
                if code in RATE_CODES:
                    raise VkApiLimited(f'VK API: ограничение запросов, код {code}',code=code)
                if code in (14,17):
                    raise VkApiProtection(f'VK API: требуется проверка, код {code}',code=code)
                if code in (5,7,28):
                    raise VkApiAuthFailure(f'VK API: ошибка авторизации/прав доступа, код {code}',code=code)
                raise VkApiFailure(f'VK API: ошибка метода wall.get, код {code}',code=code if type(code) is int else None)
            result = payload.get('response')
            if not isinstance(result,dict) or not isinstance(result.get('items'),list):
                raise VkApiFailure('VK API: response.items отсутствует')
            return result
        except VkApiLimited as exc:
            attach_read(exc)
            engine.lock_on_platform_warning(self.db,0,str(exc))
            raise
        except VkApiFailure as exc:
            attach_read(exc)
            raise


def _pinned(item: dict) -> bool | None:
    value = item.get('is_pinned',0)
    if value in (0,False):
        return False
    if value in (1,True):
        return True
    return None


def _engagement_count(item: dict, field: str) -> int | None:
    block=item.get(field)
    count=block.get('count') if isinstance(block,dict) else None
    # SQLite stores signed64-bit integers. Booleans and malformed/missing
    # values are unknown; in particular missing counters must never mean zero.
    return count if type(count) is int and 0<=count<=2**63-1 else None


def _valid_post(item, gid: int, now: int) -> bool:
    return (isinstance(item,dict) and type(item.get('owner_id')) is int
            and item['owner_id']==-gid
            and type(item.get('id')) is int and item['id']>0
            and type(item.get('date')) is int
            and 1200000000<=item['date']<=now+3600
            and _pinned(item) is not None)


def observation_time(payload: dict) -> str:
    """Keep original API time, never relabel cache reads as fresh."""
    stamp=payload.get('_observed_at',engine.moment())
    try:
        at=datetime.fromisoformat(stamp)
        if at.tzinfo is None or at.utcoffset() is None:raise ValueError()
        return at.astimezone(timezone.utc).isoformat()
    except (ValueError,TypeError):
        raise VkApiFailure('Invalid public observation timestamp') from None


def project_public_page(db, gid: int, payload: dict, *, now: int) -> None:
    """Common numerical projection for the web; no actor rows or scan cursors."""
    stamp=observation_time(payload); observed=datetime.fromisoformat(stamp)
    for item in payload['items']:
        if not _valid_post(item,gid,now):continue
        pid=item['id']
        previous=db.execute('SELECT seen_at FROM api_seen_posts WHERE group_id=? AND post_id=?',
                            (gid,pid)).fetchone()
        seen=stamp
        if previous:
            try:
                if datetime.fromisoformat(previous['seen_at'])>observed:seen=previous['seen_at']
            except (ValueError,TypeError):pass
        db.execute('''INSERT INTO api_seen_posts(group_id,post_id,published_at,seen_at)
            VALUES(?,?,?,?) ON CONFLICT(group_id,post_id) DO UPDATE SET
            valid=CASE WHEN api_seen_posts.published_at=excluded.published_at
            THEN api_seen_posts.valid ELSE 0 END,seen_at=excluded.seen_at''',
            (gid,pid,item['date'],seen))
        previous=db.execute('SELECT observed_at FROM api_post_engagement WHERE group_id=? AND post_id=?',
                            (gid,pid)).fetchone()
        if previous:
            try:
                if datetime.fromisoformat(previous['observed_at'])>observed:continue
            except (ValueError,TypeError):pass
        db.execute('''INSERT INTO api_post_engagement
            (group_id,post_id,like_count,repost_count,comment_count,view_count,observed_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_id,post_id) DO UPDATE SET
            like_count=excluded.like_count,repost_count=excluded.repost_count,
            comment_count=excluded.comment_count,view_count=excluded.view_count,
            observed_at=excluded.observed_at''',
            (gid,pid,*(_engagement_count(item,k) for k in ('likes','reposts','comments','views')),stamp))
    db.commit()


def scan_one_group(db, client: VKReadClient, group: dict, *, days: int = 7,
                   max_pages: int = 1, restart: bool = False) -> dict:
    """Get enough wall.get pages to prove weekly coverage or mark it partial.

    Full = short final page / API total exhausted / chronologically older
    *unpinned* post encountered.  An old pinned post alone is not a boundary.
    Partial checkpoints preserve next offset. A changed total invalidates the
    checkpoint (offset pagination can shift when new posts arrive).
    restart=True explicitly refreshes the head instead of resuming an older
    checkpoint; legacy callers preserve the original continuation behavior.
    """
    if not 1<=max_pages<=10 or not 1<=days<=365:
        raise ValueError('max_pages 1..10, days 1..365')
    init_api(db)
    gid = group['id']
    now = int(datetime.now(timezone.utc).timestamp())
    previous = _progress(db,gid)
    can_resume = bool(not restart and previous and previous['status']=='partial' and
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
        if client.remaining()<=0 and not (hasattr(client,'has_cached_wall') and
                client.has_cached_wall(gid,count=PAGE_SIZE,offset=offset)):
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
        engagement=[]
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
            engagement.append((pid,_engagement_count(item,'likes'),_engagement_count(item,'reposts'),
                               _engagement_count(item,'comments'),_engagement_count(item,'views')))
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
        project_public_page(db,gid,{**payload,'items':[item for item in items if _valid_post(item,gid,now)]},now=now)
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


def engagement_report(db, selected, *, since: int | None = None,
                      until: int | None = None) -> dict:
    """Known/unknown totals for exact valid API posts in an optional UTC range.

    `since` is inclusive, `until` exclusive. Individual likes/reposts sums can
    cover different subsets; counts disclose their coverage. The combined sum
    includes only posts with both counters known. None is never coerced to0.
    Observations are snapshots, not a claim of current live reaction counts.
    """
    if any(value is not None and type(value) is not int for value in (since,until)):
        raise ValueError('Границы отчёта должны быть UTC timestamp или None')
    if since is not None and until is not None and since>=until:
        raise ValueError('Начало отчёта должно предшествовать концу')
    init_api(db)
    ids=sorted({g['id'] for g in selected if g.get('enabled',True)})
    empty=dict(posts=0,likes_known_posts=0,reposts_known_posts=0,known_total_posts=0,
               unknown_total_posts=0,like_count=None,repost_count=None,
               engagement_total=None,last_observed_at=None,comment_count=None,
               comments_known_posts=0,known_activity_posts=0,activity_total=None)
    if not ids:
        return empty
    marks=','.join('?' for _ in ids)
    bounds='';params=list(ids)
    if since is not None:
        bounds+=' AND a.published_at>=?';params.append(since)
    if until is not None:
        bounds+=' AND a.published_at<?';params.append(until)
    row=db.execute(f'''SELECT COUNT(*) AS posts,
        COUNT(e.like_count) AS likes_known_posts,COUNT(e.repost_count) AS reposts_known_posts,
        COUNT(CASE WHEN e.like_count IS NOT NULL AND e.repost_count IS NOT NULL THEN 1 END) AS known_total_posts,
        SUM(e.like_count) AS like_count,SUM(e.repost_count) AS repost_count,
        COUNT(e.comment_count) AS comments_known_posts,SUM(e.comment_count) AS comment_count,
        COUNT(CASE WHEN e.like_count IS NOT NULL AND e.repost_count IS NOT NULL
            AND e.comment_count IS NOT NULL THEN 1 END) AS known_activity_posts,
        SUM(CASE WHEN e.like_count IS NOT NULL AND e.repost_count IS NOT NULL
            AND e.comment_count IS NOT NULL THEN e.like_count+e.repost_count+e.comment_count END) AS activity_total,
        SUM(CASE WHEN e.like_count IS NOT NULL AND e.repost_count IS NOT NULL
            THEN e.like_count+e.repost_count END) AS engagement_total,
        MAX(e.observed_at) AS last_observed_at
      FROM api_seen_posts a JOIN post_publication d
        ON d.group_id=a.group_id AND d.post_id=a.post_id AND d.exact=1
          AND d.published_at=a.published_at
      LEFT JOIN api_post_engagement e ON e.group_id=a.group_id AND e.post_id=a.post_id
      WHERE a.valid=1 AND a.group_id IN ({marks}) {bounds}''',params).fetchone()
    result=dict(row);result['unknown_total_posts']=result['posts']-result['known_total_posts']
    return result


def configured_token() -> str | None:
    """Environment first, then a local untracked single-token file. Never log it."""
    token=os.environ.get('VK_API_TOKEN','').strip()
    if token:
        return token
    explicit=os.environ.get('VK_API_TOKEN_FILE','').strip()
    path=Path(explicit) if explicit else Path(__file__).resolve().parent/'api_keys.txt'
    try:
        with path.open('rb') as stream:
            raw=stream.read(16385)
    except FileNotFoundError:
        if not explicit:
            return None
        raise VkApiAuthFailure('Локальный файл ключа VK API не найден') from None
    except OSError:
        raise VkApiAuthFailure('Не удалось прочитать локальный файл ключа VK API') from None
    try:
        text=raw.decode('utf-8-sig').strip()
    except UnicodeError:
        raise VkApiAuthFailure('Некорректный формат локального файла ключа VK API') from None
    if text.startswith('VK_API_TOKEN='):
        text=text[len('VK_API_TOKEN='):].strip()
        if len(text)>=2 and text[0] in ('"',"'") and text[-1]==text[0]:
            text=text[1:-1]
    if len(raw)>16384 or not text or any(char.isspace() for char in text) or '=' in text:
        raise VkApiAuthFailure('В локальном файле ожидается один ключ VK API')
    return text
