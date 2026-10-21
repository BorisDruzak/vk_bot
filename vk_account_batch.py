#!/usr/bin/env python3
"""Read-only VK API catalog import for account #2's independent action database.

No private, account-specific statuses, attempts, safety locks, sessions or cookies
are imported from account #1. Only public wall.get identifiers and dates.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import vk_batch as engine
import vk_groups as multi
import vk_api_read as api


class CatalogError(RuntimeError):
    """Public source catalog unavailable or does not have valid provenance."""


def refresh_public_catalog(account_db: sqlite3.Connection, *,
                           source_db: Path = engine.DB_PATH,
                           target_db: Path, days: int = 7,
                           now: int | None = None) -> int:
    """Copy *only* validated, recent public API metadata into account #2 SQLite.

    This never copies posts.like_status, posts.repost_status, action_attempts,
    locks or browser scan state from the primary account. Existing account #2
    rows retain their current statuses, including verified and uncertain.
    The source database is opened read-only and never migrated or altered.
    """
    source_db, target_db = Path(source_db), Path(target_db)
    if source_db.resolve() == target_db.resolve():
        raise CatalogError('Общая API-база и SQLite второго аккаунта не должны совпадать')
    if not source_db.is_file():
        raise CatalogError('Общая SQLite VK API не найдена; обнови каталог под аккаунтом №1')
    if days < 1 or days > 30:
        raise ValueError('Окно публикаций должно быть от 1 до 30 дней')
    stamp = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
    uri = source_db.resolve().as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True)) as source:
            source.row_factory = sqlite3.Row
            # Do not use the first account's per-post state or browser-only rows.
            catalog = source.execute('''
              SELECT a.group_id,a.post_id,a.published_at,a.seen_at
              FROM api_seen_posts a
              JOIN post_publication d ON d.group_id=a.group_id AND d.post_id=a.post_id
              LEFT JOIN api_group_scan_progress sc ON sc.group_id=a.group_id
              WHERE a.valid=1 AND d.exact=1
                AND a.published_at=d.published_at
                AND a.published_at BETWEEN ? AND ?
                AND (sc.status IS NULL OR sc.status!='error')
              ORDER BY a.published_at DESC, a.group_id, a.post_id
            ''', (stamp - days * 86400, stamp + 3600)).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CatalogError('Невозможно прочитать точные публикации из общего VK API-каталога') from exc
    multi.init_dates(account_db)
    api.init_api(account_db)
    at = engine.moment()
    account_db.executemany('''INSERT OR IGNORE INTO posts(group_id,post_id,seen_at)
                                 VALUES(?,?,?)''',
                           ((r['group_id'], r['post_id'], at) for r in catalog))
    account_db.executemany('''INSERT INTO post_publication
      (group_id,post_id,published_at,source,exact,checked_at)
      VALUES(?,?,?,?,1,?) ON CONFLICT(group_id,post_id) DO UPDATE SET
        published_at=excluded.published_at,source=excluded.source,exact=1,
        checked_at=excluded.checked_at''',
      ((r['group_id'], r['post_id'], r['published_at'], api.API_SOURCE, at)
       for r in catalog))
    account_db.executemany('''INSERT INTO api_seen_posts
      (group_id,post_id,published_at,seen_at,valid) VALUES(?,?,?,?,1)
      ON CONFLICT(group_id,post_id) DO UPDATE SET
      published_at=excluded.published_at,seen_at=excluded.seen_at,valid=1''',
      ((r['group_id'], r['post_id'], r['published_at'], r['seen_at']) for r in catalog))
    account_db.commit()
    return len(catalog)
