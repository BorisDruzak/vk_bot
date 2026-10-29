"""Append-only, public-only wall.get observations shared by local readers.

This store owns no account state or API budget. The supplied fetch retains all
network guards and accounting. A busy OS lease propagates to the caller; it
never permits a second network request or stale fallback.
"""
from __future__ import annotations

import copy
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


NAMESPACE = 'wall.get:5.199:owner'
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_INTEGER = 2**63 - 1
_TABLES = {'public_pages', 'public_post_observations'}
_PAGE_COLUMNS = {'id', 'namespace', 'group_id', 'page_count', 'page_offset',
                 'observed_at', 'observed_epoch', 'payload_json'}
_POST_COLUMNS = {'page_id', 'owner_id', 'post_id', 'published_at', 'observed_at',
                 'like_count', 'comment_count', 'repost_count', 'view_count'}


def _integer(value, *, positive=False):
    return type(value) is int and (1 if positive else 0) <= value <= MAX_INTEGER


def _epoch(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Public observation bound must have a timezone')
        value = value.timestamp()
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError('Invalid public observation time')
    # Check representability before a fetch can spend network budget.
    datetime.fromtimestamp(value, timezone.utc)
    return float(value)


def _request(group_id, count, offset, max_age_seconds, fresh_after):
    if (not _integer(group_id, positive=True) or not _integer(count, positive=True)
            or count > 100 or not _integer(offset)):
        raise ValueError('Invalid bounded wall page identity')
    if (type(max_age_seconds) not in (int, float)
            or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
        raise ValueError('Invalid public observation lifetime')
    return None if fresh_after is None else _epoch(fresh_after)


def _project_item(item, group_id, now, *, depth=0):
    if (type(item) is not dict or not _integer(item.get('id'), positive=True)
            or type(item.get('owner_id')) is not int
            or not 0 < abs(item['owner_id']) <= MAX_INTEGER
            or (depth == 0 and item['owner_id'] != -group_id)):
        raise ValueError('Invalid public post identity')
    result = {'id': item['id'], 'owner_id': item['owner_id']}
    if depth == 0 or 'date' in item:
        if not _integer(item.get('date'), positive=True) or item['date'] > now:
            raise ValueError('Invalid public post date')
        result['date'] = item['date']
    if 'is_pinned' in item:
        if type(item['is_pinned']) not in (int, bool) or item['is_pinned'] not in (0, 1):
            raise ValueError('Invalid public post pin')
        result['is_pinned'] = item['is_pinned']
    if 'post_type' in item:
        if item['post_type'] not in ('post', 'copy', 'reply', 'suggest', 'postpone'):
            raise ValueError('Invalid public post type')
        result['post_type'] = item['post_type']
    for name in ('likes', 'comments', 'reposts', 'views'):
        if name not in item:
            continue
        counter = item[name]
        if counter is None:
            result[name] = None
        elif type(counter) is dict:
            if 'count' not in counter:
                result[name] = {}
            elif counter['count'] is None or _integer(counter['count']):
                result[name] = {'count': counter['count']}
            else:
                raise ValueError('Invalid public counter')
        else:
            raise ValueError('Invalid public counter object')
    if 'copy_history' in item:
        history = item['copy_history']
        if history is None:
            result['copy_history'] = None
        elif type(history) is list and len(history) <= 100 and depth < 5:
            result['copy_history'] = [_project_item(p, group_id, now, depth=depth + 1)
                                      for p in history]
        else:
            raise ValueError('Invalid public source chain')
    return result


def _projection(payload, group_id, count, now):
    if (type(payload) is not dict or 'error' in payload
            or not _integer(payload.get('count')) or type(payload.get('items')) is not list
            or len(payload['items']) > count or len(payload['items']) > payload['count']):
        raise ValueError('Invalid successful wall page')
    items = [_project_item(item, group_id, now) for item in payload['items']]
    if len({item['id'] for item in items}) != len(items):
        raise ValueError('Duplicate public post identity')
    projected = {'count': payload['count'], 'items': items}
    serialized = json.dumps(projected, ensure_ascii=True, allow_nan=False,
                            separators=(',', ':'))
    if len(serialized.encode('utf-8')) > MAX_PAGE_BYTES:
        raise ValueError('Public wall page is too large')
    return projected, serialized


def _schema(db, *, create=False):
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables - _TABLES:
        if create:
            raise ValueError('Refusing foreign database for public observations')
        return False
    if tables:
        if tables != _TABLES:
            if create:
                raise ValueError('Incomplete public observation schema')
            return False
        for table, columns in (('public_pages', _PAGE_COLUMNS),
                               ('public_post_observations', _POST_COLUMNS)):
            if {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')} != columns:
                if create:
                    raise ValueError('Unsupported public observation schema')
                return False
        return True
    if not create:
        return False
    db.execute('''CREATE TABLE public_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        namespace TEXT NOT NULL, group_id INTEGER NOT NULL,
        page_count INTEGER NOT NULL, page_offset INTEGER NOT NULL,
        observed_at TEXT NOT NULL, observed_epoch REAL NOT NULL,
        payload_json TEXT NOT NULL)''')
    db.execute('''CREATE INDEX public_page_identity ON public_pages
        (namespace,group_id,page_count,page_offset,id DESC)''')
    db.execute('''CREATE TABLE public_post_observations (
        page_id INTEGER NOT NULL REFERENCES public_pages(id),
        owner_id INTEGER NOT NULL,post_id INTEGER NOT NULL,published_at INTEGER NOT NULL,
        observed_at TEXT NOT NULL,like_count INTEGER,comment_count INTEGER,
        repost_count INTEGER,view_count INTEGER,
        PRIMARY KEY(page_id,owner_id,post_id))''')
    return True


class PublicPages:
    def __init__(self, path):
        self.path = Path(path)

    def lookup(self, group_id, count, offset, *, max_age_seconds=300,
               fresh_after=None, clock=time.time):
        """Read a fresh exact page without network or creating any database."""
        floor = _request(group_id, count, offset, max_age_seconds, fresh_after)
        now = _epoch(clock())
        if not self.path.is_file():
            return None
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro',
                                     uri=True)) as db:
            if not _schema(db):
                return None
            row = db.execute('''SELECT id,observed_at,observed_epoch,payload_json
                FROM public_pages WHERE namespace=? AND group_id=?
                AND page_count=? AND page_offset=? ORDER BY id DESC LIMIT 1''',
                (NAMESPACE, group_id, count, offset)).fetchone()
        if row is None:
            return None
        observation_id, stamp, epoch, serialized = row
        try:
            # Independently validate persisted metadata: no future, naive, or
            # disagreement between the ISO timestamp and the numeric bound.
            if (type(stamp) is not str or _epoch(stamp) != epoch or epoch > now
                    or now - epoch > max_age_seconds
                    or (floor is not None and epoch < floor)
                    or type(serialized) is not str or len(serialized) > MAX_PAGE_BYTES):
                return None
            payload, _ = _projection(json.loads(serialized), group_id, count, epoch)
        except (ValueError, TypeError, OverflowError, RecursionError):
            return None
        return {**payload, '_observed_at': stamp,
                '_observation_id': observation_id, '_cache_hit': True}

    def get(self, group_id, count, offset, fetch, *, max_age_seconds=300,
            fresh_after=None, clock=time.time):
        """Reuse a fresh page or observe once under a fail-closed OS lease.

        Malformed network payloads are returned unmodified for the caller's
        existing partial/error handling, and are never stored as cache success.
        """
        # Lazy import avoids vk_api_read -> public store -> scheduler cycles.
        from vk_scheduler import singleton

        options = dict(max_age_seconds=max_age_seconds,
                       fresh_after=fresh_after, clock=clock)
        cached = self.lookup(group_id, count, offset, **options)
        if cached is not None:
            return cached
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with singleton(self.path.with_suffix('.guard')):
            cached = self.lookup(group_id, count, offset, **options)
            if cached is not None:
                return cached
            # Validate/create before spending network budget. An actor database
            # at the wrong configured path must be rejected, never migrated.
            with closing(sqlite3.connect(self.path)) as db:
                with db:
                    _schema(db, create=True)
            payload = fetch()
            epoch = _epoch(clock())
            try:
                projected, serialized = _projection(payload, group_id, count, epoch)
            except (ValueError, TypeError, OverflowError, RecursionError):
                return copy.deepcopy(payload)
            observed = datetime.fromtimestamp(epoch, timezone.utc)
            # time.time can carry fractions below datetime's microsecond
            # precision. Store matching ISO/numeric values, rounding down so
            # an immediate lookup cannot see its own observation as future.
            if observed.timestamp() > epoch:
                observed -= timedelta(microseconds=1)
            epoch = observed.timestamp()
            stamp = observed.isoformat()
            with closing(sqlite3.connect(self.path)) as db:
                with db:
                    cursor = db.execute('''INSERT INTO public_pages
                        (namespace,group_id,page_count,page_offset,observed_at,
                         observed_epoch,payload_json) VALUES(?,?,?,?,?,?,?)''',
                        (NAMESPACE, group_id, count, offset, stamp, epoch, serialized))
                    observation_id = cursor.lastrowid
                    for item in projected['items']:
                        counters = [(item.get(name) or {}).get('count')
                                    for name in ('likes', 'comments', 'reposts', 'views')]
                        db.execute('''INSERT INTO public_post_observations
                            (page_id,owner_id,post_id,published_at,observed_at,
                             like_count,comment_count,repost_count,view_count)
                            VALUES(?,?,?,?,?,?,?,?,?)''',
                            (observation_id, item['owner_id'], item['id'],
                             item['date'], stamp, *counters))
            return {**projected, '_observed_at': stamp,
                    '_observation_id': observation_id, '_cache_hit': False}
