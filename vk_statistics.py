"""Read-only public wall statistics from the existing API cache.

No API calls, schema migrations, actor overlays or action writes. Values refer
to their stored observation times, not to the time this report is requested.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


METRICS = ('like_count', 'comment_count', 'repost_count', 'view_count')
GROUP_POST_LIMIT = 100
SOURCE = 'vk-api-wall.get'
CONFIG_PATH = Path(__file__).with_name('vk_groups.json')


def _names() -> dict[int, str]:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {row['id']: row.get('name') or str(row['id'])
            for row in config.get('groups', [])
            if isinstance(row, dict) and type(row.get('id')) is int and row['id'] > 0}


def _columns(db, table):
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


def _source(db):
    """Build a compatible projection without upgrading a running worker's DB."""
    if not _columns(db, 'api_seen_posts'):
        return None
    columns = _columns(db, 'api_post_engagement')
    metrics = ','.join(f'e.{name} AS {name}' if name in columns else f'NULL AS {name}'
                       for name in (*METRICS, 'observed_at'))
    join = ('LEFT JOIN api_post_engagement e USING(group_id,post_id)' if columns else '')
    return (f'SELECT s.group_id,s.post_id,s.published_at,{metrics} '
            f'FROM api_seen_posts s {join} WHERE s.valid=1')


def _open(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        return None
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def posts(db_path, group_id=None, limit=100) -> list[dict]:
    """Latest <=100 valid cached API posts by publication date, optionally group.

    Missing metrics stay None, including on a legacy schema. Rows lacking a
    counter observation retain observed_at=None rather than borrowing seen_at.
    """
    if group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise ValueError('group_id must be a positive integer')
    if type(limit) is not int or limit <= 0:
        raise ValueError('limit must be a positive integer')
    db = _open(db_path)
    if db is None:
        return []
    with closing(db):
        source = _source(db)
        if source is None:
            return []
        clause = ' AND s.group_id=?' if group_id is not None else ''
        params = (group_id, min(limit,100)) if group_id is not None else (min(limit,100),)
        rows = [dict(row) for row in db.execute(source + clause +
            ' ORDER BY s.published_at DESC,s.group_id ASC,s.post_id DESC LIMIT ?', params)]
    names = _names()
    for row in rows:
        row['group_name'] = names.get(row['group_id'], str(row['group_id']))
        row['url'] = f"https://vk.com/wall-{row['group_id']}_{row['post_id']}"
    return rows


def groups(db_path) -> list[dict]:
    """Coverage-aware totals of the latest <=100 cached posts of each group.

    A metric total is None unless every post in that window has it. Partial
    sums have explicit *_known_sum and *_known_posts fields. These are cache
    totals, never claims about the entire remote wall. observed_at is the
    oldest observation; observed_until is the newest, with an explicit number
    of observed posts so missing observations remain visible.
    """
    db = _open(db_path)
    if db is None:
        return []
    with closing(db):
        source = _source(db)
        if source is None:
            return []
        sums = ','.join(f'COUNT({name}) AS {name}_known_posts,'
                        f'SUM({name}) AS {name}_known_sum,'
                        f'CASE WHEN COUNT({name})=COUNT(*) THEN SUM({name}) END AS {name}'
                        for name in METRICS)
        rows = db.execute(f'''WITH ranked AS (
            SELECT *,ROW_NUMBER() OVER (
                PARTITION BY group_id ORDER BY published_at DESC,post_id DESC
            ) AS position FROM ({source})
        ) SELECT group_id AS id,COUNT(*) AS posts,
            MIN(observed_at) AS observed_at,MAX(observed_at) AS observed_until,
            MIN(published_at) AS published_from,MAX(published_at) AS published_until,
            COUNT(observed_at) AS observed_posts,{sums}
            FROM ranked WHERE position<=? GROUP BY group_id''',(GROUP_POST_LIMIT,)).fetchall()
    names = _names()
    report = {row['id']: dict(row) for row in rows}
    for gid in names:
        if gid not in report:
            empty = dict(id=gid,posts=0,observed_at=None,observed_until=None,observed_posts=0,
                         published_from=None,published_until=None)
            for metric in METRICS:
                empty.update({metric:None,metric+'_known_posts':0,metric+'_known_sum':None})
            report[gid] = empty
    for gid, row in report.items():
        row['name'] = names.get(gid,str(gid))
        row.update(source=SOURCE,limit=GROUP_POST_LIMIT,coverage_posts=row['posts'])
    return [report[gid] for gid in sorted(report)]
