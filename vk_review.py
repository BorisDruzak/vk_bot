#!/usr/bin/env python3
"""Offline, operator-reviewed acceptance plan for VK API-discovered posts.

This tool NEVER connects to VK, opens a browser, likes or reposts.
It reads the existing API provenance and action statuses from SQLite read-only
and exports a human-review CSV with at most 60 recent candidates.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_COLUMNS = (
    'group_id', 'group_name', 'post_id', 'published_utc', 'url',
    'like_status_db', 'repost_status_db',
    'manual_like', 'manual_repost', 'notes',
)
POSITIVE = {'да', 'yes', 'y', 'done', 'выполнено', '1'}
NEGATIVE = {'нет', 'no', 'n', '0', 'ошибка', 'error', 'не удалось'}


def parse_groups(path: Path, only: str | None = None) -> dict[int, str]:
    data = json.loads(path.read_text(encoding='utf-8'))
    raw = data.get('groups')
    if not isinstance(raw, list):
        raise ValueError('В конфигурации отсутствует список groups')
    selected = None
    if only is not None:
        try:
            selected = {int(x.strip()) for x in only.split(',') if x.strip()}
        except ValueError as exc:
            raise ValueError('Значение --only: числовые ID через запятую') from exc
        if not selected:
            raise ValueError('Пустой --only')
    result: dict[int, str] = {}
    known = set()
    for g in raw:
        if not isinstance(g, dict) or type(g.get('id')) is not int or g['id'] <= 0:
            raise ValueError('Некорректный ID сообщества в JSON')
        gid = g['id']
        if gid in known:
            raise ValueError(f'Дублирование ID сообщества {gid}')
        known.add(gid)
        if selected is not None and gid not in selected:
            continue
        if g.get('enabled') is False or not (g.get('like_enabled') and g.get('repost_enabled')):
            continue
        result[gid] = str(g.get('name') or f'Сообщество {gid}')
    if selected is not None and not selected.issubset(known):
        raise ValueError('Некоторые ID --only отсутствуют в vk_groups.json')
    if not result:
        raise ValueError('Нет сообществ, где разрешено просматривать оба действия')
    return result


def _exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (name,)).fetchone())


def read_candidates(db_path: Path, group_names: dict[int, str], days: int,
                    limit: int, now: datetime | None = None) -> list[dict[str, str]]:
    """Strict selection based on wall.get provenance; never guess from feed hints."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError('Now must be timezone-aware')
    from_ts = int((moment - timedelta(days=days)).timestamp())
    to_ts = int(moment.timestamp())
    uri = db_path.resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        required = ('api_seen_posts', 'posts')
        missing = [t for t in required if not _exists(db, t)]
        if missing:
            raise ValueError(f'Не обнаружены необходимые таблицы: {", ".join(missing)}. Сначала выполните --discover в v6.18.')
        exclude_access = _exists(db, 'post_access')
        access_sql = ('LEFT JOIN post_access x ON x.group_id=a.group_id AND x.post_id=a.post_id'
                      if exclude_access else '')
        access_condition = 'AND x.post_id IS NULL' if exclude_access else ''
        cursor = db.execute(f'''
          SELECT a.group_id, a.post_id, a.published_at, p.like_status, p.repost_status
          FROM api_seen_posts a
          JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
          {access_sql}
          WHERE a.valid=1 AND a.published_at BETWEEN ? AND ?
            AND p.like_status='pending' AND p.repost_status='pending'
            {access_condition}
          ORDER BY a.published_at DESC, a.group_id, a.post_id DESC
        ''', (from_ts, to_ts))
        seen = set()
        output = []
        for rec in cursor:
            group_id, post_id = rec['group_id'], rec['post_id']
            if group_id not in group_names or (group_id, post_id) in seen:
                continue
            seen.add((group_id, post_id))
            published = datetime.fromtimestamp(rec['published_at'], timezone.utc)
            name = group_names[group_id]
            # Avoid Excel interpreting untrusted community names as formulas.
            if name.lstrip().startswith(('=', '+', '-', '@')):
                name = "'" + name
            output.append({
                'group_id': str(group_id),
                'group_name': name,
                'post_id': str(post_id),
                'published_utc': published.strftime('%Y-%m-%d %H:%M:%S UTC'),
                'url': f'https://vk.com/wall-{group_id}_{post_id}',
                'like_status_db': rec['like_status'],
                'repost_status_db': rec['repost_status'],
                'manual_like': '',
                'manual_repost': '',
                'notes': '',
            })
            if len(output) >= limit:
                break
        return output


def export_rows(rows: list[dict[str, str]], csv_path: Path, *, overwrite: bool) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    mode = 'w' if overwrite else 'x'
    with csv_path.open(mode, newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, delimiter=';', lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(rows)


def read_report(csv_path: Path) -> dict[str, int]:
    with csv_path.open(newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file, delimiter=';')
        if not reader.fieldnames or not set(CSV_COLUMNS).issubset(reader.fieldnames):
            raise ValueError('Это не файл VK Review: структура столбцов не совпадает')
        counts = {'posts': 0, 'likes_marked': 0, 'reposts_marked': 0, 'both_marked': 0,
                  'needs_review': 0, 'issues': 0}
        for row in reader:
            counts['posts'] += 1
            like = str(row.get('manual_like') or '').strip().casefold()
            repost = str(row.get('manual_repost') or '').strip().casefold()
            if like not in POSITIVE | NEGATIVE | {''} or repost not in POSITIVE | NEGATIVE | {''}:
                raise ValueError(f'Строка {counts["posts"]}: статус должен быть да, нет или пустой')
            if like in POSITIVE:
                counts['likes_marked'] += 1
            if repost in POSITIVE:
                counts['reposts_marked'] += 1
            if like in POSITIVE and repost in POSITIVE:
                counts['both_marked'] += 1
            if not like or not repost:
                counts['needs_review'] += 1
            if like in NEGATIVE or repost in NEGATIVE:
                counts['issues'] += 1
        return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='VK Review — локальный план приёмочного теста без автоматических реакций')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--export', action='store_true', help='Создать CSV для ручной проверки')
    mode.add_argument('--report', action='store_true', help='Посчитать ручные отметки в CSV')
    parser.add_argument('--db', type=Path, default=HERE / 'vk_batch.sqlite3')
    parser.add_argument('--config', type=Path, default=HERE / 'vk_groups.json')
    parser.add_argument('--output', type=Path, default=HERE / 'vk_review_60.csv')
    parser.add_argument('--count', type=int, default=60)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--only', default=None, help='Один или несколько ID групп через запятую')
    parser.add_argument('--overwrite', action='store_true', help='Перезаписать CSV (стирает ручные отметки)')
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 60 or not 1 <= args.days <= 30:
        parser.error('--count 1..60, --days 1..30')
    if args.overwrite and args.report:
        parser.error('--overwrite применяется только с --export')
    try:
        if args.report:
            counts = read_report(args.output)
            print(f'[REVIEW] Записей: {counts["posts"]}; вручную отмечено лайков: '
                  f'{counts["likes_marked"]}; репостов: {counts["reposts_marked"]}; '
                  f'обе отметки: {counts["both_marked"]}')
            print(f'[REVIEW] Без полной отметки: {counts["needs_review"]}; с замечаниями: {counts["issues"]}')
            print('[NOTE] Это ручные отметки из CSV, а НЕ подтверждение VK.')
        else:
            groups = parse_groups(args.config, args.only)
            rows = read_candidates(args.db, groups, args.days, args.count)
            export_rows(rows, args.output, overwrite=args.overwrite)
            print(f'[EXPORT] {len(rows)}/{args.count} публикаций из VK API за последние {args.days} дней')
            print(f'[FILE] {args.output.resolve()}')
            print('[NOTE] Файл предназначен для ручного просмотра и отметок. '
                  'Запросов к VK, лайков и репостов не выполнялось.')
            if len(rows) < args.count:
                print('[NOTE] Подходящих записей меньше целевого количества. '
                      'Проверь полноту API через vk_unified.py --api-report и --discover.')
    except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
