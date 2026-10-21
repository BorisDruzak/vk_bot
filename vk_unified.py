#!/usr/bin/env python3
"""Single-account, multi-group VK orchestration.

Official VK API for public wall discovery (read-only), SQLite for resumable
progress, Playwright only for independently verified actions on posts selected by VK API. Does not
circumvent VK rate limits or try to imitate a human. In command-action mode
the operator explicitly specifies per-invocation maximum attempts; the
application still halts at VK-enforced limits, CAPTCHA and uncertain outcomes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import io
import hashlib
import json
import os
import re
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PWTimeout, sync_playwright

import vk_api_read as api
import vk_batch as engine
import vk_groups as multi

VERSION = '6.26'
LOCKFILE = Path(__file__).resolve().parent / '.vk-unified-running.lock'
DEFAULT_CAMPAIGN = 'test_60_likes'
DEFAULT_PAIR_CAMPAIGN = 'auto_30_pairs'
APPROVAL_DEFAULT = Path(__file__).resolve().parent / 'vk_approve_30.csv'
RESULT_DEFAULT = Path(__file__).resolve().parent / 'vk_pairs_results.csv'
ACTION_RESULTS_DEFAULT = Path(__file__).resolve().parent / 'vk_actions_results.csv'
RESULT_FIELDS = ('url','group_name','published_at_utc','like_status','repost_status',
                 'liked','reposted','new_like_in_campaign','new_repost_in_campaign',
                 'like_attempt_at_utc','repost_attempt_at_utc','access','updated_at_utc')
CSV_FIELDS = ('approve','url','group_id','post_id','published_at_utc','group_name','like_status','repost_status')


class RunAlreadyActive(RuntimeError):
    pass


@contextmanager
def exclusive_run(path: Path = LOCKFILE):
    """A conservative lock to prevent two unified executions on one profile."""
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RunAlreadyActive(f'Обнаружен {path.name}: одновременно запускать две копии нельзя. '
                               'Если предыдущий процесс завершился аварийно, проверь его отсутствие '
                               'перед ручным удалением файла.') from None
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write('VK Unified running; do not run another process on this profile.\n')
        yield
    finally:
        path.unlink(missing_ok=True)


def init_campaign(db) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS like_experiments (
       name TEXT PRIMARY KEY,
       target_likes INTEGER NOT NULL,
       created_at TEXT NOT NULL,
       completed_at TEXT,
       baseline_action_id INTEGER NOT NULL DEFAULT 0,
       group_ids_json TEXT NOT NULL
    )''')
    columns={row['name'] for row in db.execute('PRAGMA table_info(like_experiments)')}
    if 'baseline_action_id' not in columns:
        db.execute('ALTER TABLE like_experiments ADD COLUMN baseline_action_id INTEGER NOT NULL DEFAULT 0')
    db.execute("""CREATE TABLE IF NOT EXISTS vk_health_events (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       at TEXT NOT NULL,
       kind TEXT NOT NULL,
       detail TEXT NOT NULL
    )""")
    db.commit()


def record_health_event(db, kind: str, detail: str) -> None:
    init_campaign(db)
    # Only brief categories/errors, never URLs, cookies or authentication data.
    allowed={'api-rate-limit','browser-rate-limit','challenge'}
    if kind not in allowed:
        raise ValueError('Unsupported health event category')
    db.execute('INSERT INTO vk_health_events(at,kind,detail) VALUES(?,?,?)',
               (engine.moment(), kind, detail[:180]))
    db.commit()


def start_campaign(db, name: str, target: int, group_ids: list[int]):
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', name) or not 1 <= target <= 60:
        raise ValueError('Имя: латиница/цифры/_/- (3..40); цель 1..60 лайков')
    if not group_ids or len(set(group_ids)) != len(group_ids):
        raise ValueError('Список сообществ пуст или содержит дубли')
    init_campaign(db)
    saved = db.execute('SELECT * FROM like_experiments WHERE name=?', (name,)).fetchone()
    canon = json.dumps(sorted(group_ids), separators=(',', ':'))
    if saved:
        if saved['target_likes'] != target or saved['group_ids_json'] != canon:
            raise ValueError('Существующий тест имеет иную цель или список групп. '
                             'Для нового теста укажи новое --name.')
        return saved
    baseline=db.execute('SELECT COALESCE(MAX(id),0) FROM action_attempts').fetchone()[0]
    db.execute('INSERT INTO like_experiments(name,target_likes,created_at,baseline_action_id,group_ids_json) '
               'VALUES(?,?,?,?,?)', (name, target, engine.moment(), baseline, canon))
    db.commit()
    return db.execute('SELECT * FROM like_experiments WHERE name=?', (name,)).fetchone()


def get_campaign(db, name: str):
    init_campaign(db)
    return db.execute('SELECT * FROM like_experiments WHERE name=?', (name,)).fetchone()


def campaign_progress(db, campaign) -> int:
    """Count only distinct VK-verified new likes attempted AFTER experiment start.

    Older successful actions and posts that were already liked are not credited.
    """
    if campaign is None:
        return 0
    ids = json.loads(campaign['group_ids_json'])
    if not ids:
        return 0
    marks = ','.join('?' for _ in ids)
    return db.execute(f'''
      SELECT COUNT(*) FROM (
        SELECT a.group_id,a.post_id FROM action_attempts a
        JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
        WHERE a.action='like' AND a.id > ?
          AND p.like_status='verified' AND a.group_id IN ({marks})
        GROUP BY a.group_id,a.post_id
      )''', (campaign['baseline_action_id'], *ids)).fetchone()[0]


def finish_campaign_if_complete(db, campaign) -> bool:
    if campaign_progress(db,campaign) >= campaign['target_likes']:
        db.execute('UPDATE like_experiments SET completed_at=COALESCE(completed_at, ?) '
                   'WHERE name=?', (engine.moment(), campaign['name']))
        db.commit()
        return True
    return False


def pending_recent_likes(db, groups: list[dict], days: int, *, limit: int = 100) -> list:
    """Only exact, recent, unliked-by-program posts; never old/unresolved."""
    liked_groups = [g for g in groups if g.get('like_enabled')]
    if not liked_groups:
        return []
    # v6.18: provenance is *independent* of browser JSON/UI dates.  Only a
    # public wall.get result with an exact owner_id/post id/date may select a
    # post.  Browser-only legacy discoveries are never silently upgraded.
    api.init_api(db)
    cutoff = int(datetime.now(timezone.utc).timestamp()) - days*86400
    authoritative = {(r['group_id'],r['post_id']): r['published_at']
                     for r in db.execute('''SELECT a.group_id,a.post_id,a.published_at
                         FROM api_seen_posts a
                         LEFT JOIN api_group_scan_progress g ON g.group_id=a.group_id
                         WHERE a.valid=1 AND a.published_at>=?
                         AND (g.status IS NULL OR g.status!='error')''', (cutoff,))}
    rows = multi.candidate_rows(db, liked_groups, days, include_unknown=False,
                                include_older=False, limit=5000)
    return [r for r in rows if r['like_status']=='pending' and r['exact']==1
            and multi.date_allowed(r['published_at'],days,True)
            and authoritative.get((r['group_id'],r['post_id']))==r['published_at']][:limit]


def print_status(db, cfg: dict, groups: list[dict], name: str) -> None:
    multi.print_multigroup_report(db,groups,cfg['lookback_days'])
    campaign = get_campaign(db,name)
    if campaign is None:
        print('[GOAL] Тест ещё не начат: запуск с --run --confirm создаст новую точку отсчёта')
    else:
        done=campaign_progress(db,campaign)
        print(f'[GOAL] {campaign["name"]}: {done}/{campaign["target_likes"]} '
              f'новых лайков подтверждено VK с {campaign["created_at"]}')
        if campaign['completed_at']:
            print(f'[GOAL] Завершён: {campaign["completed_at"]}')
    events=db.execute('SELECT kind,at FROM vk_health_events ORDER BY id DESC LIMIT 1').fetchone()
    count_events=db.execute('SELECT COUNT(*) FROM vk_health_events').fetchone()[0]
    if count_events:
        print(f'[HEALTH] В истории тестов {count_events} предупреждений VK; последнее: '
              f'{events["kind"]} {events["at"]}')
    else:
        print('[HEALTH] Предупреждений VK в истории v6.16 пока нет; '
              'это не проверка общего состояния аккаунта.')
    api.api_scan_report(db,groups,min_interval_hours=cfg['scan_min_interval_hours'])
    fresh=pending_recent_likes(db,groups,cfg['lookback_days'],limit=1000)
    print(f'[QUEUE] Кандидатов из VK API с точной датой: {len(fresh)} '
          '(старые browser-only записи не выбираются)')
    print('[LIMITS] Лайки за запуск / 24 ч: '
          f'{cfg["limits"]["likes_per_run"]} / {cfg["limits"]["likes_per_24h"]}; '
          f'общих действий: {cfg["limits"]["actions_per_run"]} / {cfg["limits"]["actions_per_24h"]}')


def discover_api(db, cfg: dict, groups: list[dict], *, required: bool,
                 token: str | None, max_groups: int = 3) -> int:
    if not token:
        if required:
            raise api.VkApiAuthFailure('VK_API_TOKEN не задан. Токен выдаёт VK для вашего приложения. '
                                       'Не присылай токен в чат и не записывай в JSON.')
        print('[API] Токен не настроен: новое сканирование пропущено; используется кеш SQLite.')
        return 0
    engine.require_platform_unlocked(db)
    client=api.VKReadClient(db,token,pause_seconds=max(cfg['read_pause_seconds'],20),
                            per_run=3,per_24h=30)
    multi.init_scan_progress(db)
    due=api.choose_api_scan_groups(db,groups,min(max_groups,3),
                                   min_interval_hours=cfg['scan_min_interval_hours'])
    if not due:
        print('[API CACHE] Эти группы уже недавно прочитаны через API; новых запросов нет.')
        return 0
    print(f'[API] Чтение публичных стен через wall.get: до {len(due)} групп, '
          f'не более {client.remaining()} запросов за этот запуск.')
    total=0
    for g in due:
        if client.remaining()==0:
            print('[API CAP] Бюджет чтения исчерпан; продолжение только новым запуском.')
            break
        try:
            result=api.scan_one_group(db,client,g,days=cfg['lookback_days'],max_pages=3)
        except (api.VkApiFailure,api.VkApiAuthFailure,api.VkApiLimited) as exc:
            api.mark_api_group_error(db,g['id'],str(exc))
            print(f'[API STOP] Группа {g["id"]}: {exc}. Автоматических повторов нет.')
            raise
        total+=result['fetched']
        print(f'   [API FOUND] {g["id"]}: прочитано={result["fetched"]} за '
              f'{result.get("pages",1)} стр., за {cfg["lookback_days"]} дн.='
              f'{result["recent"]} (в текущем цикле), '
              f'полнота={result.get("status","partial")} '
              f'({result.get("reason","unknown")})')
    print(f'[API DONE] Получено записей в этом запуске: {total}; '
          'открывать стены в браузере для обнаружения не требуется.')
    api.api_scan_report(db,groups,min_interval_hours=cfg['scan_min_interval_hours'])
    return total


def run_likes(db, cfg: dict, groups: list[dict], campaign,
              *, max_posts: int, pause: float) -> int:
    if campaign['completed_at']:
        print('[GOAL] Этот тест уже завершён. Повторное выполнение не требуется.')
        return 0
    done=campaign_progress(db,campaign)
    if done>=campaign['target_likes']:
        finish_campaign_if_complete(db,campaign)
        print('[GOAL] Цель уже достигнута; кликов не будет.')
        return 0
    budget=multi.MultiBudget(db,cfg['limits'])
    remaining_budget=min(
        cfg['limits']['likes_per_run'],cfg['limits']['actions_per_run'],
        cfg['limits']['likes_per_24h']-budget.past_count('like'),
        cfg['limits']['actions_per_24h']-budget.past_count(),
        campaign['target_likes']-done)
    if remaining_budget<=0:
        print('[SAFETY PAUSE] На текущий момент лимит действий исчерпан. '
              'Новых кликов не будет. После восстановления суточного бюджета запусти вручную.')
        return 0
    candidates=pending_recent_likes(db,groups,cfg['lookback_days'],limit=max_posts)
    if not candidates:
        print('[QUEUE] Недостаточно проверенных свежих публикаций с точной датой. '
              'Выполни --discover (VK API) или ручной аудит, без лайков.')
        return 0
    print(f'[PLAN] Цель {campaign["target_likes"]}, выполнено {done}. '
          f'Текущий запуск: максимум {remaining_budget} новых лайков, '
          f'проверить не более {len(candidates)} постов; репосты отключены.')
    print('[PLAN] При предупреждении VK, капче, HTTP 429 или неопределённом '
          'результате выполнение остановится без автоматического продолжения.')
    engine.PROFILE.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as pw:
        context=pw.chromium.launch_persistent_context(
            user_data_dir=str(engine.PROFILE),headless=False,locale='ru-RU',
            viewport={'width':1320,'height':900})
        try:
            page=context.pages[0] if context.pages else context.new_page()
            engine.watch_rate_limit_responses(page)
            engine.configure_navigation_pacing(page,pause,db=db)
            completed=0
            for row in candidates:
                if campaign_progress(db,campaign)>=campaign['target_likes']:
                    break
                if not budget.available_actions({'like'}):
                    print('[SAFETY PAUSE] Лимиты лайков/действий исчерпаны, очередь сохранена.')
                    break
                gid,pid=row['group_id'],row['post_id']
                print(f'[NEXT] https://vk.com/wall-{gid}_{pid}')
                try:
                    engine.require_platform_unlocked(db)
                    engine.go_to_post(page,gid,pid)
                    date=multi.exact_post_date_info(page,gid,pid)
                    live=date.get('publishedAt')
                    if live is None or not multi.date_allowed(live,cfg['lookback_days'],True):
                        print('   [SKIP DATE] Не удалось подтвердить дату поста из текущей страницы.')
                        continue
                    # Multiple independent date sources should match within one
                    # second; a mismatch may mean stale cache/wrong content.
                    if abs(int(live)-int(row['published_at']))>1:
                        raise engine.StopRun(f'Дата поста {gid}_{pid} отличается между VK API/SQLite '
                                             'и страницей VK. Нажатия запрещены.')
                    budget.current_group_id=gid
                    before=campaign_progress(db,campaign)
                    engine.process_one(page,db,gid,row,cfg['action_pause_seconds'],
                                       safety=budget,actions={'like'},already_open=True)
                    after=campaign_progress(db,campaign)
                    if after>before:
                        completed+=after-before
                        print(f'   [GOAL] {after}/{campaign["target_likes"]} новых лайков подтверждено')
                except engine.PostUnavailable as exc:
                    engine.record_post_access(db,gid,pid,exc.kind,str(exc))
                    print(f'   [SKIP] {gid}_{pid}: {exc.kind}')
                    continue
                except engine.PostMarkupUnknown as exc:
                    engine.record_post_access(db,gid,pid,'unresolved',str(exc))
                    print(f'   [STOP] {gid}_{pid}: нестандартная разметка; никаких кликов.')
                    break
                except engine.PostTemporaryError as exc:
                    engine.record_post_access(db,gid,pid,'temporary',str(exc))
                    print(f'   [STOP] {gid}_{pid}: временная ошибка; не продолжаем серию.')
                    break
            finish_campaign_if_complete(db,campaign)
            print(f'[RESULT] Новых лайков в этой партии: {completed}; '
                  f'всего в тесте: {campaign_progress(db,campaign)}/{campaign["target_likes"]}')
            return completed
        finally:
            context.close()



def init_pair_campaign(db) -> None:
    """Additive schema: never alter legacy post/like/repost histories."""
    db.execute("""CREATE TABLE IF NOT EXISTS pair_experiments (
        name TEXT PRIMARY KEY,
        target_each INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        baseline_action_id INTEGER NOT NULL,
        approved_digest TEXT NOT NULL,
        completed_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS pair_experiment_posts (
        campaign TEXT NOT NULL,
        group_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        PRIMARY KEY(campaign,group_id,post_id),
        FOREIGN KEY(campaign) REFERENCES pair_experiments(name)
    )""")
    db.commit()


def start_auto_pair_campaign(db, name: str, target: int, group_ids: list[int], days: int):
    """Create a resumable campaign with a *dynamic* VK API queue, not an approval manifest.

    All selected posts must have a valid current API provenance and a published
    timestamp inside the configured lookback window. A group/target change
    requires a different campaign name, not implicit campaign migration.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', name) or not 1 <= target <= 30:
        raise ValueError('Имя кампании: 3..40 символов латиница/цифры/_/-; цель 1..30 пар')
    if not 1 <= days <= 30 or not group_ids or len(set(group_ids)) != len(group_ids):
        raise ValueError('Для кампании требуется непустой набор групп и окно 1..30 дней')
    init_pair_campaign(db)
    scope = json.dumps({'groups': sorted(group_ids), 'days': days},
                       sort_keys=True, separators=(',', ':'))
    digest = 'auto-api-v1:' + hashlib.sha256(scope.encode('utf-8')).hexdigest()
    saved = db.execute('SELECT * FROM pair_experiments WHERE name=?', (name,)).fetchone()
    if saved:
        if saved['target_each'] != target or saved['approved_digest'] != digest:
            raise ValueError('У этой кампании уже другие настройки или ручной список. '
                             'Для нового набора групп/цели укажи другое --pair-name.')
        return saved
    baseline = db.execute('SELECT COALESCE(MAX(id),0) FROM action_attempts').fetchone()[0]
    db.execute('INSERT INTO pair_experiments '
               '(name,target_each,created_at,baseline_action_id,approved_digest) '
               'VALUES(?,?,?,?,?)', (name,target,engine.moment(),baseline,digest))
    db.commit()
    return db.execute('SELECT * FROM pair_experiments WHERE name=?', (name,)).fetchone()


def auto_pair_candidates(db, groups: list[dict], days: int, campaign=None, *, limit: int = 5000) -> list:
    """Live campaign queue from API-confirmed wall.get IDs (SQLite only).

    Partially completed campaign posts take priority so likes without reposts
    can be finished first. Otherwise the newest posts are selected first.
    Unknown dates, mismatched API provenance, suspicious states and blocked
    posts are never selected automatically.
    """
    api.init_api(db)
    init_pair_campaign(db)
    enabled = sorted({g['id'] for g in groups if g.get('enabled', True)
                      and g.get('like_enabled') and g.get('repost_enabled')})
    if not enabled:
        return []
    cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
    now = int(datetime.now(timezone.utc).timestamp())
    placeholders = ','.join('?' for _ in enabled)
    rows = db.execute(f"""
        SELECT p.group_id,p.post_id,p.like_status,p.repost_status,
               d.published_at,d.exact,d.source,
               CASE WHEN cp.post_id IS NOT NULL THEN 1 ELSE 0 END AS in_campaign
          FROM api_seen_posts a
          JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
          JOIN post_publication d ON d.group_id=a.group_id AND d.post_id=a.post_id
          LEFT JOIN pair_experiment_posts cp
            ON cp.campaign=? AND cp.group_id=p.group_id AND cp.post_id=p.post_id
          LEFT JOIN post_access x ON x.group_id=p.group_id AND x.post_id=p.post_id
          LEFT JOIN api_group_scan_progress prog ON prog.group_id=p.group_id
         WHERE p.group_id IN ({placeholders}) AND a.valid=1
           AND a.published_at=d.published_at AND d.exact=1
           AND d.published_at BETWEEN ? AND ?
           AND x.post_id IS NULL
           AND (prog.status IS NULL OR prog.status!='error')
           AND (p.like_status='pending' OR p.repost_status='pending')
           AND p.like_status NOT IN ('uncertain','error')
           AND p.repost_status NOT IN ('uncertain','error')
    """, (campaign['name'] if campaign else '', *enabled, cutoff, now+3600)).fetchall()
    def priority(row):
        # Preserve a consistent pair if one part is already complete.
        half_done = bool(row['in_campaign']) and (
            (row['like_status'] in ('verified','already') and row['repost_status']=='pending')
            or (row['repost_status'] in ('verified','already') and row['like_status']=='pending'))
        return (-int(half_done), -row['published_at'], row['group_id'], -row['post_id'])
    return sorted(rows, key=priority)[:limit]


def export_auto_pair_results(db, campaign, groups: list[dict], dest: Path = RESULT_DEFAULT) -> int:
    """CSV is OUTPUT only; all values come from locally recorded VK confirmations.

    UTF-8 BOM + semicolon is Excel-friendly. Atomic replacement protects the
    previous report from partial writes or interrupted processes.
    """
    init_pair_campaign(db)
    names = {g['id']: g['name'] for g in groups}
    rows = db.execute("""SELECT x.group_id,x.post_id,p.like_status,p.repost_status,
          p.updated_at,d.published_at,access.state AS access_state,
          (SELECT MAX(a.attempted_at) FROM action_attempts a
           WHERE a.group_id=x.group_id AND a.post_id=x.post_id AND a.action='like'
             AND a.id > ?) AS like_attempt,
          (SELECT MAX(a.attempted_at) FROM action_attempts a
           WHERE a.group_id=x.group_id AND a.post_id=x.post_id AND a.action='repost'
             AND a.id > ?) AS repost_attempt
        FROM pair_experiment_posts x
        JOIN posts p ON x.group_id=p.group_id AND x.post_id=p.post_id
        LEFT JOIN post_publication d ON d.group_id=x.group_id AND d.post_id=x.post_id
        LEFT JOIN post_access access ON access.group_id=x.group_id AND access.post_id=x.post_id
        WHERE x.campaign=? ORDER BY d.published_at DESC,x.group_id,x.post_id""",
        (campaign['baseline_action_id'],campaign['baseline_action_id'],campaign['name'])).fetchall()
    dest = Path(dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8-sig', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=RESULT_FIELDS, delimiter=';',
                                    lineterminator='\n')
            writer.writeheader()
            for r in rows:
                like_done = r['like_status'] in ('verified','already')
                repost_done = r['repost_status'] in ('verified','already')
                writer.writerow({
                    'url': f'https://vk.com/wall-{r["group_id"]}_{r["post_id"]}',
                    'group_name': _csv_safe(names.get(r['group_id'], f'Сообщество {r["group_id"]}')),
                    'published_at_utc': (datetime.fromtimestamp(r['published_at'],timezone.utc).isoformat()
                                         if r['published_at'] is not None else ''),
                    'like_status': r['like_status'], 'repost_status': r['repost_status'],
                    'liked': 'да' if like_done else 'нет',
                    'reposted': 'да' if repost_done else 'нет',
                    'new_like_in_campaign': ('да' if r['like_attempt'] and r['like_status']=='verified' else 'нет'),
                    'new_repost_in_campaign': ('да' if r['repost_attempt'] and r['repost_status']=='verified' else 'нет'),
                    'like_attempt_at_utc': r['like_attempt'] or '',
                    'repost_attempt_at_utc': r['repost_attempt'] or '',
                    'access': r['access_state'] or 'нет сохранённой ошибки доступа',
                    'updated_at_utc': r['updated_at'] or ''})
        os.replace(temporary, dest)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'[CSV REPORT] {len(rows)} записей: {dest}')
    return len(rows)


def print_auto_pair_preview(db, groups: list[dict], days: int, campaign, limit: int):
    rows = auto_pair_candidates(db, groups, days, campaign, limit=limit)
    print(f'[AUTO QUEUE] {len(rows)} публикаций за последние {days} дней (VK API + SQLite)')
    for r in rows:
        at = datetime.fromtimestamp(r['published_at'],timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
        print(f'   {at} | https://vk.com/wall-{r["group_id"]}_{r["post_id"]} '
              f'| like={r["like_status"]} repost={r["repost_status"]}')
    return rows


def pair_review_candidates(db, groups: list[dict], days: int, count: int) -> list:
    """Only previously validated VK API IDs with exact recent dates and two pending actions."""
    enabled={g['id']:g for g in groups if g.get('like_enabled') and g.get('repost_enabled')}
    rows=pending_recent_likes(db, [g for g in groups if g['id'] in enabled], days,
                             limit=5000)
    return [r for r in rows if r['group_id'] in enabled
            and r['repost_status']=='pending'][:count]


def _csv_safe(value: str) -> str:
    # Prevent Office/Excel formula execution on free-text group names.
    if value and value.lstrip().startswith(('=','+','-','@','\t','\r')):
        return "'"+value
    return value


def export_pair_review(db, groups: list[dict], days: int, count: int, dest: Path):
    if not 1<=count<=90:
        raise ValueError('Для экспорта разрешено 1..90 публикаций')
    candidates=pair_review_candidates(db,groups,days,count)
    names={g['id']:g['name'] for g in groups}
    # No login data, tokens or post bodies are stored in the file.
    dest=dest.resolve()
    if dest.exists():
        raise ValueError(f'Файл уже существует: {dest}. Укажи новое --approved-file; '+
                         'существующее утверждение нельзя затирать.')
    with dest.open('w',encoding='utf-8-sig',newline='') as fh:
        writer=csv.DictWriter(fh,fieldnames=CSV_FIELDS,delimiter=';',lineterminator='\n')
        writer.writeheader()
        for r in candidates:
            writer.writerow(dict(approve='',url=f'https://vk.com/wall-{r["group_id"]}_{r["post_id"]}',
                 group_id=r['group_id'],post_id=r['post_id'],
                 published_at_utc=datetime.fromtimestamp(r['published_at'],timezone.utc).isoformat(),
                 group_name=_csv_safe(names[r['group_id']]),
                 like_status=r['like_status'],repost_status=r['repost_status']))
    print(f'[APPROVAL] Экспортировано {len(candidates)} публикаций: {dest}')
    print('[APPROVAL] Проверь материалы и впиши «да» в approve только для выбранных строк. '+
          'Никаких действий при экспорте не было.')
    return len(candidates)


def load_approved_pairs(db, groups: list[dict], days: int, path: Path, target: int, *, enforce_fresh: bool=True) -> list[tuple[int,int]]:
    """Validate human approval against authoritative API provenance, not untrusted CSV URLs."""
    if not path.is_file():
        raise ValueError(f'Не найден файл утверждений: {path}')
    if path.stat().st_size > 500_000:
        raise ValueError('Слишком большой CSV с утверждениями')
    api.init_api(db)
    groups_by_id={g['id']:g for g in groups if g.get('like_enabled') and g.get('repost_enabled')}
    if not groups_by_id:
        raise ValueError('Нет групп, где разрешены оба действия')
    approved=[]
    seen=set()
    cutoff=int(datetime.now(timezone.utc).timestamp())-days*86400
    raw=path.read_bytes()
    try:
        content=raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        # Legacy Russian Excel can save semicolon-separated CSV as Windows-1251.
        content=raw.decode('cp1251')
    with io.StringIO(content,newline='') as fh:
        reader=csv.DictReader(fh,delimiter=';')
        if reader.fieldnames is None or not set(CSV_FIELDS).issubset(reader.fieldnames):
            raise ValueError('Некорректный CSV: отсутствуют заголовки; разделитель должен быть «;»')
        for line,row in enumerate(reader,start=2):
            approve=(row.get('approve') or '').strip().casefold()
            if approve in ('','нет','no','0'):
                continue
            if approve not in ('да','yes','1'):
                raise ValueError(f'Неверное значение approve в строке {line}: нужна «да» или «нет»')
            try:
                gid,pid=int(row['group_id']),int(row['post_id'])
            except (ValueError,TypeError) as exc:
                raise ValueError(f'Неверный ID в строке {line}') from exc
            if (gid,pid) in seen:
                raise ValueError(f'Повторно утверждена запись {gid}_{pid} в строке {line}')
            seen.add((gid,pid))
            if gid not in groups_by_id or pid<1 or row.get('url') != f'https://vk.com/wall-{gid}_{pid}':
                raise ValueError(f'Пост {gid}_{pid}: URL/ID не соответствует разрешённой группе')
            r=db.execute("""SELECT a.published_at,p.like_status,p.repost_status,d.published_at AS local_date,
                           d.exact,x.state AS access_state
                FROM api_seen_posts a JOIN posts p ON a.group_id=p.group_id AND a.post_id=p.post_id
                JOIN post_publication d ON d.group_id=a.group_id AND d.post_id=a.post_id
                LEFT JOIN post_access x ON x.group_id=a.group_id AND x.post_id=a.post_id
                WHERE a.group_id=? AND a.post_id=? AND a.valid=1""",(gid,pid)).fetchone()
            if (r is None or (enforce_fresh and (r['published_at']<cutoff
                or r['published_at']>int(datetime.now(timezone.utc).timestamp())+3600
                or r['local_date']!=r['published_at'] or r['exact']!=1
                or r['access_state'] is not None))):
                raise ValueError(f'Пост {gid}_{pid}: нет актуальной подтверждённой даты VK API или недоступен')
            if r['like_status'] in ('uncertain','error') or r['repost_status'] in ('uncertain','error'):
                raise ValueError(f'Пост {gid}_{pid}: есть незавершённый/ошибочный статус, сначала сверка VK')
            # Human selection is independent of whether an action later becomes already.
            approved.append((gid,pid))
    if len(approved)<target or len(approved)>90:
        raise ValueError(f'Для цели {target}/{target} нужно утвердить от {target} до 90 публикаций; сейчас {len(approved)}')
    return sorted(approved)


def _approved_digest(approved: list[tuple[int,int]]) -> str:
    canonical='\n'.join(f'{gid}_{pid}' for gid,pid in sorted(approved)).encode('ascii')
    return hashlib.sha256(canonical).hexdigest()


def start_pair_campaign(db, name: str, target: int, approved: list[tuple[int,int]]):
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}',name) or not 1<=target<=30:
        raise ValueError('Имя кампании: 3..40 символов латиница/цифры/_/-; цель 1..30 пар')
    if len(approved)<target:
        raise ValueError('Утверждённых публикаций меньше цели')
    init_pair_campaign(db)
    digest=_approved_digest(approved)
    saved=db.execute('SELECT * FROM pair_experiments WHERE name=?',(name,)).fetchone()
    if saved is not None:
        if saved['target_each']!=target or saved['approved_digest']!=digest:
            raise ValueError('Для этой кампании изменились цель или утверждённый список. '+
                             'Это запрещено во время выполнения; создай новую кампанию.')
        return saved
    baseline=db.execute('SELECT COALESCE(MAX(id),0) FROM action_attempts').fetchone()[0]
    with db:
        db.execute('INSERT INTO pair_experiments(name,target_each,created_at,baseline_action_id,approved_digest) '
                   'VALUES(?,?,?,?,?)',(name,target,engine.moment(),baseline,digest))
        db.executemany('INSERT INTO pair_experiment_posts(campaign,group_id,post_id) VALUES(?,?,?)',
                       ((name,gid,pid) for gid,pid in approved))
    return db.execute('SELECT * FROM pair_experiments WHERE name=?',(name,)).fetchone()


def paired_progress(db, campaign) -> dict[str,int]:
    if campaign is None:
        return {'like':0,'repost':0,'complete':0,'approved':0}
    selected=db.execute('SELECT COUNT(*) FROM pair_experiment_posts WHERE campaign=?',
                        (campaign['name'],)).fetchone()[0]
    result={'approved':selected}
    for action in ('like','repost'):
        verified=db.execute(f"""SELECT COUNT(*) FROM (
          SELECT a.group_id,a.post_id FROM action_attempts a
          JOIN pair_experiment_posts x ON x.campaign=? AND x.group_id=a.group_id AND x.post_id=a.post_id
          JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
          WHERE a.action=? AND a.id>? AND p.{action}_status='verified'
          GROUP BY a.group_id,a.post_id)""",
          (campaign['name'],action,campaign['baseline_action_id'])).fetchone()[0]
        result[action]=verified
    result['complete']=db.execute("""SELECT COUNT(*) FROM pair_experiment_posts x
       JOIN posts p ON p.group_id=x.group_id AND p.post_id=x.post_id
       WHERE x.campaign=? AND p.like_status IN ('verified','already')
         AND p.repost_status IN ('verified','already')""",(campaign['name'],)).fetchone()[0]
    return result


def finish_pair_campaign_if_complete(db, campaign) -> bool:
    done=paired_progress(db,campaign)
    if done['like']>=campaign['target_each'] and done['repost']>=campaign['target_each']:
        db.execute('UPDATE pair_experiments SET completed_at=COALESCE(completed_at,?) WHERE name=?',
                   (engine.moment(),campaign['name']))
        db.commit()
        return True
    return False


def print_pair_status(db, name: str) -> None:
    init_pair_campaign(db)
    camp=db.execute('SELECT * FROM pair_experiments WHERE name=?',(name,)).fetchone()
    if camp is None:
        print(f'[PAIRS] Кампания {name} ещё не начата. --run-pairs создаст её из API-очереди.')
        return
    done=paired_progress(db,camp)
    target=camp['target_each']
    mode='автоматическая API-очередь' if camp['approved_digest'].startswith('auto-api-v1:') else 'ручная кампания'
    print(f'[PAIRS] Кампания {name} ({mode}); учтено {done["approved"]} публикаций')
    print(f'[PAIRS] Новые лайки, подтверждённые VK: {done["like"]}/{target}')
    print(f'[PAIRS] Новые репосты, подтверждённые VK: {done["repost"]}/{target}')
    print(f'[PAIRS] Обе реакции на одном посте (включая уже существовавшие): {done["complete"]}')
    print(f'[PAIRS] Завершение: {camp["completed_at"] or "ещё не завершена"}')


def pair_pending_rows(db, groups: list[dict], days: int, campaign) -> list:
    """Validate every approved row against fresh API provenance and current DB state."""
    allowed={g['id'] for g in groups if g.get('like_enabled') and g.get('repost_enabled')}
    cutoff=int(datetime.now(timezone.utc).timestamp())-days*86400
    rows=db.execute("""SELECT p.group_id,p.post_id,p.like_status,p.repost_status,d.published_at,d.exact
        FROM pair_experiment_posts x
        JOIN posts p ON p.group_id=x.group_id AND p.post_id=x.post_id
        JOIN post_publication d ON d.group_id=p.group_id AND d.post_id=p.post_id
        JOIN api_seen_posts a ON a.group_id=p.group_id AND a.post_id=p.post_id
        LEFT JOIN api_group_scan_progress g ON g.group_id=p.group_id
        LEFT JOIN post_access bad ON bad.group_id=p.group_id AND bad.post_id=p.post_id
        WHERE x.campaign=? AND a.valid=1 AND a.published_at=d.published_at
          AND (g.status IS NULL OR g.status!='error')
          AND d.exact=1 AND d.published_at>=? AND bad.post_id IS NULL
          AND (p.like_status='pending' OR p.repost_status='pending')
          AND p.like_status NOT IN ('uncertain','error')
          AND p.repost_status NOT IN ('uncertain','error')
        ORDER BY d.published_at DESC,p.group_id,p.post_id""", (campaign['name'],cutoff)).fetchall()
    return [r for r in rows if r['group_id'] in allowed]


def run_approved_pairs(db,cfg:dict,groups:list[dict],campaign,*,max_posts:int,pause:float,
                       candidates=None, auto_mode:bool=False) -> int:
    """One operator-initiated bounded session. Never automatically reschedules itself."""
    if campaign['completed_at']:
        print('[PAIRS] Цель ранее достигнута; новых действий не будет.')
        return 0
    done=paired_progress(db,campaign)
    target=campaign['target_each']
    if done['like']>=target and done['repost']>=target:
        finish_pair_campaign_if_complete(db,campaign)
        print('[PAIRS] Цель достигнута; новых действий не будет.')
        return 0
    budget=multi.MultiBudget(db,cfg['limits'])
    candidates=(auto_pair_candidates(db,groups,cfg['lookback_days'],campaign)
                if auto_mode and candidates is None else
                pair_pending_rows(db,groups,cfg['lookback_days'],campaign)
                if candidates is None else candidates)
    if not candidates:
        print('[QUEUE] Нет подходящих публикаций VK API за 7 дней или нет действий pending.')
        return 0
    usable=[]
    for row in candidates:
        remaining=paired_progress(db,campaign)
        desired={action for action in ('like','repost')
                 if remaining[action]<target and row[f'{action}_status']=='pending'}
        if desired:
            usable.append(row)
    if not usable:
        print('[QUEUE] Не осталось публикаций с незавершёнными действиями.')
        return 0
    print(f'[PLAN] Цель: {target} новых лайков + {target} новых репостов; '+
          f'сейчас {done["like"]}/{target} и {done["repost"]}/{target}. '+
          f'До {min(max_posts,len(usable))} постов за запуск; лимиты JSON сохранены.')
    print('[PLAN] Автовыбор из подтверждённых VK API ID за 7 дней. '+
          'При предупреждении, CAPTCHA или неопределённом результате — остановка.'
          if auto_mode else
          '[PLAN] Действия только с ранее утверждёнными ссылками VK API. '+
          'При предупреждении, CAPTCHA или неопределённом результате — остановка.')
    visited=0
    engine.PROFILE.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as pw:
        context=pw.chromium.launch_persistent_context(
            user_data_dir=str(engine.PROFILE),headless=False,locale='ru-RU',
            viewport={'width':1320,'height':900})
        try:
            page=context.pages[0] if context.pages else context.new_page()
            engine.watch_rate_limit_responses(page)
            engine.configure_navigation_pacing(page,pause,db=db)
            for row in usable:
                if visited>=max_posts:
                    break
                done=paired_progress(db,campaign)
                if done['like']>=target and done['repost']>=target:
                    break
                desired={action for action in ('like','repost')
                         if done[action]<target and row[f'{action}_status']=='pending'}
                allowed=budget.available_actions(desired)
                slots=min(cfg['limits']['actions_per_run']-budget.used['total'],
                          cfg['limits']['actions_per_24h']-budget.past_count())
                # Do not start two actions if the combined budget only has one slot.
                if len(allowed)>slots:
                    if slots<=0:
                        allowed=set()
                    elif slots==1:
                        preferred='repost' if done['repost']<done['like'] else 'like'
                        if preferred not in allowed:
                            preferred='repost' if 'repost' in allowed else 'like'
                        allowed={preferred}
                if not allowed:
                    # No visit needed when this post cannot receive any new action.
                    if not budget.available_actions({'like','repost'}):
                        print('[SAFETY PAUSE] Лимиты действий исчерпаны; прогресс сохранён.')
                        break
                    continue
                gid,pid=row['group_id'],row['post_id']
                if auto_mode:
                    # Record the chosen post *before* opening VK: interruptions
                    # still appear in the CSV report, without repeating clicks.
                    db.execute('INSERT OR IGNORE INTO pair_experiment_posts'
                               '(campaign,group_id,post_id) VALUES(?,?,?)',
                               (campaign['name'],gid,pid))
                    db.commit()
                visited+=1  # Count attempted openings, including a hidden/error page.
                print(f'[NEXT] https://vk.com/wall-{gid}_{pid} | действия: {", ".join(sorted(allowed))}')
                try:
                    engine.require_platform_unlocked(db)
                    engine.go_to_post(page,gid,pid)
                    live=multi.exact_post_date_info(page,gid,pid).get('publishedAt')
                    if (live is None or not multi.date_allowed(live,cfg['lookback_days'],True)
                        or abs(int(live)-int(row['published_at']))>1):
                        raise engine.StopRun(f'Дата {gid}_{pid} не подтверждена или отличается от VK API; '+
                                             'нажатия запрещены')
                    budget.current_group_id=gid
                    # Re-read statuses after any previous iteration/DB commit.
                    fresh=db.execute('SELECT * FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()
                    engine.process_one(page,db,gid,fresh,cfg['action_pause_seconds'],
                                       safety=budget,actions=allowed,already_open=True)
                    engine.clear_post_access(db,gid,pid)
                    finished=paired_progress(db,campaign)
                    print(f'   [PAIRS] Лайки {finished["like"]}/{target}; репосты {finished["repost"]}/{target}')
                except engine.PostUnavailable as exc:
                    engine.record_post_access(db,gid,pid,exc.kind,str(exc))
                    print(f'   [SKIP] {gid}_{pid}: {exc.kind}')
                except engine.PostMarkupUnknown as exc:
                    engine.record_post_access(db,gid,pid,'unresolved',str(exc))
                    print(f'   [STOP] {gid}_{pid}: неоднозначная разметка, никаких дальнейших кликов.')
                    break
                except engine.PostTemporaryError as exc:
                    engine.record_post_access(db,gid,pid,'temporary',str(exc))
                    print(f'   [STOP] {gid}_{pid}: временная ошибка; серию прерываем.')
                    break
            finish_pair_campaign_if_complete(db,campaign)
            print_pair_status(db,campaign['name'])
            return visited
        finally:
            context.close()

# v6.23: explicit per-command action counts, independent of historic JSON quotas.
# This is NOT a VK rate-limit bypass: the persistent platform warning lock,
# HTTP 429/CAPTCHA detectors and post-state validations are unchanged.

def init_command_actions(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS command_action_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        requested_like INTEGER NOT NULL CHECK(requested_like>=0),
        requested_repost INTEGER NOT NULL CHECK(requested_repost>=0),
        max_posts INTEGER NOT NULL CHECK(max_posts>0),
        result TEXT NOT NULL DEFAULT 'running'
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS command_action_posts (
        run_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        PRIMARY KEY (run_id,group_id,post_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS command_action_attempts (
        run_id INTEGER NOT NULL,
        attempt_id INTEGER NOT NULL UNIQUE,
        PRIMARY KEY (run_id,attempt_id)
    )""")
    db.commit()


class CommandActionBudget:
    """Only the operator-provided counts constrain this single invocation.

    This budget still persists attempted clicks BEFORE the browser submits them.
    It does not read the legacy per-day/per-run JSON quotas, and cannot clear
    a persisted VK platform warning. No spoofing or automated retry is used.
    """
    def __init__(self, db, run_id: int, likes: int, reposts: int):
        if min(likes, reposts) < 0 or (likes + reposts) <= 0:
            raise ValueError('Нужно указать положительное число лайков или репостов')
        engine.require_platform_unlocked(db)
        self.db = db
        self.run_id = run_id
        self.requested = {'like': likes, 'repost': reposts}
        self.used = Counter()
        self.current_group_id = None
        self.identity_guard = None  # optional verified account binding, before every click

    def available_actions(self, actions: set[str]) -> set[str]:
        engine.require_platform_unlocked(self.db)
        return {a for a in actions if a in self.requested and self.used[a] < self.requested[a]}

    def reserve(self, post_id: int, action: str) -> None:
        if self.identity_guard is not None:
            self.identity_guard()
        if action not in self.available_actions({action}):
            raise engine.SafetyLimitReached(f'Командное число действий {action} уже достигнуто')
        if self.current_group_id is None:
            raise ValueError('Не задана группа для действия')
        cursor = self.db.execute(
            'INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
            (self.current_group_id, post_id, action, engine.moment()))
        self.db.execute('INSERT INTO command_action_attempts(run_id,attempt_id) VALUES(?,?)',
                        (self.run_id, cursor.lastrowid))
        self.db.commit()  # durability before every outward-facing click
        self.used['total'] += 1
        self.used[action] += 1


def begin_command_action_run(db, likes: int, reposts: int, max_posts: int) -> int:
    init_command_actions(db)
    cursor = db.execute('''INSERT INTO command_action_runs
        (started_at,requested_like,requested_repost,max_posts)
        VALUES (?,?,?,?)''', (engine.moment(), likes, reposts, max_posts))
    db.commit()
    return int(cursor.lastrowid)


def command_action_progress(db, run_id: int) -> dict:
    """Attempt ledger + verified state, never a guess from post existence."""
    init_command_actions(db)
    progress = {'like_attempts': 0, 'repost_attempts': 0,
                'like_verified': 0, 'repost_verified': 0}
    for action in ('like', 'repost'):
        progress[f'{action}_attempts'] = db.execute('''
          SELECT COUNT(*) FROM command_action_attempts c
          JOIN action_attempts a ON a.id=c.attempt_id
          WHERE c.run_id=? AND a.action=?''', (run_id, action)).fetchone()[0]
        progress[f'{action}_verified'] = db.execute(f'''
          SELECT COUNT(*) FROM (
            SELECT a.group_id,a.post_id FROM command_action_attempts c
            JOIN action_attempts a ON a.id=c.attempt_id
            JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
            WHERE c.run_id=? AND a.action=? AND p.{action}_status='verified'
            GROUP BY a.group_id,a.post_id)''', (run_id, action)).fetchone()[0]
    progress['posts'] = db.execute(
        'SELECT COUNT(*) FROM command_action_posts WHERE run_id=?', (run_id,)).fetchone()[0]
    return progress


def command_action_candidates(db, groups: list[dict], days: int,
                              likes: int, reposts: int) -> list:
    """Only previously saved, exact public VK API wall.get post IDs."""
    api.init_api(db)
    allowed = sorted({g['id'] for g in groups if g.get('enabled', True) and (
        (likes > 0 and g.get('like_enabled')) or
        (reposts > 0 and g.get('repost_enabled'))
    )})
    if not allowed:
        return []
    cutoff = int(datetime.now(timezone.utc).timestamp()) - days*86400
    now = int(datetime.now(timezone.utc).timestamp())
    marks = ','.join('?' for _ in allowed)
    rows = db.execute(f'''
       SELECT p.group_id,p.post_id,p.like_status,p.repost_status,d.published_at
       FROM api_seen_posts a
       JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
       JOIN post_publication d ON d.group_id=p.group_id AND d.post_id=p.post_id
       LEFT JOIN post_access x ON x.group_id=p.group_id AND x.post_id=p.post_id
       LEFT JOIN api_group_scan_progress sc ON sc.group_id=p.group_id
       WHERE p.group_id IN ({marks}) AND a.valid=1
         AND a.published_at=d.published_at AND d.exact=1
         AND d.published_at BETWEEN ? AND ?
         AND x.post_id IS NULL
         AND (sc.status IS NULL OR sc.status!='error')
         AND p.like_status NOT IN ('uncertain','error')
         AND p.repost_status NOT IN ('uncertain','error')
         AND ((? > 0 AND p.like_status='pending')
           OR (? > 0 AND p.repost_status='pending'))
       ''', (*allowed, cutoff, now+3600, likes, reposts)).fetchall()
    def priority(r):
        # Previously completed half-pairs first; then newest publications.
        half = (r['like_status'] in ('verified','already')) != (
                r['repost_status'] in ('verified','already'))
        return (-int(half), -r['published_at'], r['group_id'], -r['post_id'])
    return sorted(rows, key=priority)


def run_command_actions(db, cfg: dict, groups: list[dict], *,
                        likes: int, reposts: int, max_posts: int,
                        pause: float, profile: Path | None = None,
                        identity_guard=None) -> int:
    """One bounded manual invocation; no automatic repeat/scheduling.

    The requested numbers are MAXIMUM ATTEMPTS in THIS invocation, not a
    guarantee of VK-verified effects or a platform-approved quota.
    """
    engine.require_platform_unlocked(db)
    candidates = command_action_candidates(db, groups, cfg['lookback_days'], likes, reposts)
    if not candidates:
        print('[QUEUE] Нет новых подходящих API-публикаций или действий pending.')
        return 0
    run_id = begin_command_action_run(db, likes, reposts, max_posts)
    budget = CommandActionBudget(db, run_id, likes, reposts)
    enabled = {g['id']: g for g in groups if g.get('enabled', True)}
    inspected = 0
    result = 'partial'
    print(f'[COMMAND] Запуск #{run_id}: до {likes} попыток лайка, '
          f'до {reposts} попыток репоста, не более {max_posts} публикаций.')
    print('[COMMAND] Исторические ограничения JSON не применяются к этому режиму. '
          'Остановка при ограничении VK, CAPTCHA и неизвестном результате сохранена.')
    active_profile = engine.PROFILE if profile is None else Path(profile)
    active_profile.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(active_profile), headless=False, locale='ru-RU',
                viewport={'width': 1320, 'height': 900})
            try:
                page = context.pages[0] if context.pages else context.new_page()
                engine.watch_rate_limit_responses(page)
                if identity_guard is not None:
                    budget.identity_guard = lambda: identity_guard(context)
                engine.configure_navigation_pacing(page, pause, db=db)
                for row in candidates:
                    if inspected >= max_posts:
                        break
                    desired = {a for a in ('like', 'repost')
                               if budget.requested[a] > 0 and row[f'{a}_status']=='pending'
                               and enabled[row['group_id']].get(f'{a}_enabled')}
                    allowed = budget.available_actions(desired)
                    if not allowed:
                        if not budget.available_actions({'like','repost'}):
                            break
                        continue
                    gid,pid = row['group_id'], row['post_id']
                    db.execute('INSERT OR IGNORE INTO command_action_posts(run_id,group_id,post_id) '
                               'VALUES (?,?,?)', (run_id, gid, pid))
                    db.commit()
                    inspected += 1
                    print(f'[NEXT] https://vk.com/wall-{gid}_{pid} | '
                          f'действия: {", ".join(sorted(allowed))}')
                    try:
                        engine.require_platform_unlocked(db)
                        engine.go_to_post(page, gid, pid)
                        if identity_guard is not None:
                            identity_guard(context)
                        current_date = multi.exact_post_date_info(page,gid,pid).get('publishedAt')
                        if (current_date is None
                            or not multi.date_allowed(current_date,cfg['lookback_days'],True)
                            or abs(int(current_date)-int(row['published_at'])) > 1):
                            raise engine.StopRun(f'Дата {gid}_{pid} отличается от VK API; '
                                                 'действия не выполнялись')
                        budget.current_group_id = gid
                        fresh = db.execute('SELECT * FROM posts WHERE group_id=? AND post_id=?',
                                           (gid,pid)).fetchone()
                        engine.process_one(page,db,gid,fresh,cfg['action_pause_seconds'],
                                           safety=budget,actions=allowed,already_open=True)
                        engine.clear_post_access(db,gid,pid)
                        current = command_action_progress(db, run_id)
                        print(f'   [COMMAND] Подтверждено: лайков {current["like_verified"]}/{likes}, '
                              f'репостов {current["repost_verified"]}/{reposts}; '
                              f'попыток {current["like_attempts"]}+{current["repost_attempts"]}')
                    except engine.PostUnavailable as exc:
                        engine.record_post_access(db,gid,pid,exc.kind,str(exc))
                        print(f'   [SKIP] {gid}_{pid}: {exc.kind}')
                    except engine.PostMarkupUnknown as exc:
                        engine.record_post_access(db,gid,pid,'unresolved',str(exc))
                        print(f'   [STOP] {gid}_{pid}: неизвестная разметка VK')
                        result = 'stopped'
                        break
                    except engine.PostTemporaryError as exc:
                        engine.record_post_access(db,gid,pid,'temporary',str(exc))
                        print(f'   [STOP] {gid}_{pid}: временная ошибка VK')
                        result = 'stopped'
                        break
                if result != 'stopped':
                    prog = command_action_progress(db,run_id)
                    result = ('complete' if prog['like_attempts'] >= likes
                              and prog['repost_attempts'] >= reposts else 'partial')
            finally:
                context.close()
    finally:
        # A crash after click may leave uncertain; do NOT retry automatically.
        db.execute('UPDATE command_action_runs SET finished_at=?,result=? WHERE id=?',
                   (engine.moment(), result, run_id))
        db.commit()
        prog = command_action_progress(db,run_id)
        print(f'[COMMAND RESULT] Запуск #{run_id}: попыток лайка '
              f'{prog["like_attempts"]}/{likes}, репоста {prog["repost_attempts"]}/{reposts}; '
              f'подтверждено VK: {prog["like_verified"]} лайков, '
              f'{prog["repost_verified"]} репостов; постов {prog["posts"]}/{max_posts}')
    return run_id


def print_command_action_status(db) -> None:
    init_command_actions(db)
    latest = db.execute('SELECT * FROM command_action_runs ORDER BY id DESC LIMIT 1').fetchone()
    if latest is None:
        print('[COMMAND] Запусков с независимыми числовыми заданиями пока нет.')
        return
    p = command_action_progress(db, latest['id'])
    print(f'[COMMAND] Последний запуск #{latest["id"]}: {latest["result"]}, '
          f'{latest["started_at"]} → {latest["finished_at"] or "выполняется"}')
    print(f'[COMMAND] Лайки: попыток {p["like_attempts"]}/{latest["requested_like"]}, '
          f'подтверждено VK {p["like_verified"]}; репосты: '
          f'попыток {p["repost_attempts"]}/{latest["requested_repost"]}, '
          f'подтверждено VK {p["repost_verified"]}')
    sums = db.execute('''SELECT action,COUNT(*) FROM command_action_attempts c
       JOIN action_attempts a ON a.id=c.attempt_id GROUP BY action''').fetchall()
    print('[COMMAND] Все запускá: попыток по видам —',
          dict((r[0],r[1]) for r in sums))


def export_command_action_results(db, groups: list[dict], dest: Path = ACTION_RESULTS_DEFAULT) -> int:
    """Output-only report of current VK-confirmed states of posts tried in command mode."""
    init_command_actions(db)
    names = {g['id']: g['name'] for g in groups}
    rows = db.execute('''
        SELECT t.group_id,t.post_id,t.last_run,p.like_status,p.repost_status,
               p.updated_at,d.published_at,x.state AS access,
          (SELECT MAX(a.attempted_at) FROM command_action_attempts ca
           JOIN action_attempts a ON a.id=ca.attempt_id
           WHERE a.group_id=t.group_id AND a.post_id=t.post_id AND a.action='like') AS like_at,
          (SELECT MAX(a.attempted_at) FROM command_action_attempts ca
           JOIN action_attempts a ON a.id=ca.attempt_id
           WHERE a.group_id=t.group_id AND a.post_id=t.post_id AND a.action='repost') AS repost_at
        FROM (SELECT group_id,post_id,MAX(run_id) last_run
              FROM command_action_posts GROUP BY group_id,post_id) t
        JOIN posts p ON p.group_id=t.group_id AND p.post_id=t.post_id
        LEFT JOIN post_publication d ON d.group_id=t.group_id AND d.post_id=t.post_id
        LEFT JOIN post_access x ON x.group_id=t.group_id AND x.post_id=t.post_id
        ORDER BY d.published_at DESC,t.group_id,t.post_id''').fetchall()
    dest=Path(dest).resolve()
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_name(dest.name + '.tmp')
    try:
        with temp.open('w',encoding='utf-8-sig',newline='') as out:
            writer=csv.DictWriter(out,fieldnames=RESULT_FIELDS,delimiter=';',lineterminator='\n')
            writer.writeheader()
            for r in rows:
                writer.writerow({
                    'url':f'https://vk.com/wall-{r["group_id"]}_{r["post_id"]}',
                    'group_name':_csv_safe(names.get(r['group_id'],f'Сообщество {r["group_id"]}')),
                    'published_at_utc':(datetime.fromtimestamp(r['published_at'],timezone.utc).isoformat()
                                        if r['published_at'] is not None else ''),
                    'like_status':r['like_status'],'repost_status':r['repost_status'],
                    'liked':'да' if r['like_status'] in ('already','verified') else 'нет',
                    'reposted':'да' if r['repost_status'] in ('already','verified') else 'нет',
                    'new_like_in_campaign':'да' if r['like_at'] and r['like_status']=='verified' else 'нет',
                    'new_repost_in_campaign':'да' if r['repost_at'] and r['repost_status']=='verified' else 'нет',
                    'like_attempt_at_utc':r['like_at'] or '',
                    'repost_attempt_at_utc':r['repost_at'] or '',
                    'access':r['access'] or 'нет сохранённой ошибки доступа',
                    'updated_at_utc':r['updated_at'] or ''})
        os.replace(temp,dest)
    finally:
        temp.unlink(missing_ok=True)
    print(f'[CSV REPORT] {len(rows)} записей: {dest}')
    return len(rows)


def run_account2_mode(args, parser) -> int:
    """Account #2: independent action ledger, shared public API metadata only.

    Profile #1 and its SQLite are never written. The shared catalog is read-only;
    there is no auto-discovery, scheduling or coordinated multi-account batch.
    """
    import vk_accounts as accounts
    import vk_account_smoke as smoke
    import vk_account_batch as account_catalog

    mode = 'run' if args.run_actions else 'status' if args.action_status else 'preview'
    if args.test_actions is not None or args.run_pairs or args.run or args.discover:
        parser.error('Для №2 доступны только --preview, --action-status, --run-actions и разовый --test-post')
    if mode != 'run' and (args.confirm or args.likes is not None or args.reposts is not None):
        parser.error('--likes/--reposts/--confirm допускаются только с --run-actions')
    likes, reposts = (args.likes or 0), (args.reposts or 0)
    if mode == 'run' and (likes < 0 or reposts < 0 or likes + reposts == 0 or not args.confirm):
        parser.error('Для действий №2 необходимы --likes N / --reposts M и --confirm')
    if args.read_pause is not None and not 5 <= args.read_pause <= 300:
        parser.error('--read-pause должен быть 5..300 секунд')
    if args.max_posts is None:
        args.max_posts = max(likes, reposts)*2 if mode == 'run' else 12
    if args.max_posts < 1:
        parser.error('--max-posts должен быть >=1')
    cfg = multi.load_config(args.config)
    selected = multi.selected_groups(cfg,args.only,0)
    if not selected:
        parser.error('Нет активных групп для второго аккаунта')
    if args.read_pause is not None:
        cfg['read_pause_seconds'] = max(cfg['read_pause_seconds'], args.read_pause)
    cfg['read_pause_seconds'] = max(cfg['read_pause_seconds'], 20.)
    if likes and not cfg['actions']['like'] or reposts and not cfg['actions']['repost']:
        parser.error('Запрошенное действие глобально выключено в vk_groups.json')
    vk_id = smoke.pinned_user_id(2)
    if not accounts.profile_for_account(2).is_dir():
        raise smoke.SmokeError('Профиль Chromium №2 отсутствует; сначала --account 2 --check-account')
    report = args.report_file or (Path(__file__).resolve().parent / 'vk_actions_results_account_2.csv')
    print(f'VK UNIFIED v{VERSION} | аккаунт=2, VK ID {vk_id}; источник: общий VK API (только чтение)')
    # Serialize both accounts' action runs to prevent overlapping VK sessions.
    with exclusive_run(), accounts.exclusive_profile(2), closing(engine.db_open(smoke.ACCOUNT2_DB)) as db:
        smoke.init_account2(db, vk_id)
        if mode == 'run':
            engine.require_platform_unlocked(db)
        if mode != 'status':
            total = account_catalog.refresh_public_catalog(db, source_db=engine.DB_PATH,
                                     target_db=smoke.ACCOUNT2_DB, days=cfg['lookback_days'])
            print(f'[API CATALOG] {total} публичных ID с точными датами. История №1 не переносилась.')
        if mode == 'preview':
            found = command_action_candidates(db,selected,cfg['lookback_days'],1,1)
            print(f'[ACCOUNT 2 PREVIEW] До {args.max_posts} постов из {len(found)} кандидатов; без действий')
            for row in found[:args.max_posts]:
                print(f'   https://vk.com/wall-{row["group_id"]}_{row["post_id"]} | '
                      f'like={row["like_status"]} repost={row["repost_status"]}')
            return 0
        if mode == 'status':
            print_command_action_status(db)
            engine.safety_report(db,0)
            try:
                export_command_action_results(db,selected,report)
            except OSError as exc:
                print('[CSV WARNING] Не удалось обновить CSV, закрой Excel:',type(exc).__name__)
            return 0
        try:
            run_command_actions(db,cfg,selected,likes=likes,reposts=reposts,
                        max_posts=args.max_posts,pause=cfg['read_pause_seconds'],
                        profile=accounts.profile_for_account(2),
                        identity_guard=lambda context: smoke._live_identity(context,vk_id))
        finally:
            try:
                export_command_action_results(db,selected,report)
            except OSError as exc:
                print('[CSV WARNING] Не удалось обновить CSV, закрой Excel:',type(exc).__name__)
    return 0

def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(
        description='VK Unified: VK API (поиск), SQLite (очередь), Playwright (лайки/репосты), CSV (отчёт)')
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--login-only',action='store_true',help='Ручной первый вход без действий, отдельный Chromium-профиль')
    modes.add_argument('--check-account',action='store_true',help='Проверить VK ID и сохранённую сессию, без действий')
    modes.add_argument('--accounts-report',action='store_true',help='Локальный список привязок аккаунтов, без сети')
    modes.add_argument('--test-post',type=str,metavar='GROUP_POST',help='Разовая проверка или тест действия аккаунта №2 на одном API-посте')
    modes.add_argument('--test-status',action='store_true',help='Локальные статусы разового теста аккаунта №2')
    modes.add_argument('--status',action='store_true',help='Только локальный отчёт')
    modes.add_argument('--preview',action='store_true',help='Только локальная очередь')
    modes.add_argument('--discover',action='store_true',help='Пакетное чтение публичных стен VK API с пагинацией')
    modes.add_argument('--api-report',action='store_true',help='Локальный отчёт по статусу API для всех выбранных групп')
    modes.add_argument('--run',action='store_true',help='Продолжить ограниченный тест лайков из API-очереди')
    modes.add_argument('--preview-pairs',action='store_true',help='Показать автоматическую очередь пары лайк+репост; без сети')
    modes.add_argument('--run-pairs',action='store_true',help='Лайки+репосты из VK API; с --likes/--reposts — числовой режим без квот JSON')
    modes.add_argument('--run-actions',action='store_true',help='Лайки и репосты из API, число каждого действия задаётся командой; без лимитов JSON')
    modes.add_argument('--action-status',action='store_true',help='Отчёт числовых запусков из SQLite; без сети')
    modes.add_argument('--pair-status',action='store_true',help='Отчёт кампании лайков и репостов; без сети')
    parser.add_argument('--goal-likes',type=int,default=60,help='Цель теста в новых подтверждённых лайках: 1..60')
    parser.add_argument('--name',default=DEFAULT_CAMPAIGN,help='ID прежнего теста лайков, сохраняется в SQLite')
    parser.add_argument('--pair-name',default=DEFAULT_PAIR_CAMPAIGN,help='ID автоматической кампании 30/30')
    parser.add_argument('--goal-pairs',type=int,default=30,help='Цель новых лайков и репостов каждого типа: 1..30')
    parser.add_argument('--likes',type=int,default=None,help='Число ПОПЫТОК новых лайков в этом запуске, от 0')
    parser.add_argument('--reposts',type=int,default=None,help='Число ПОПЫТОК новых репостов в этом запуске, от 0')
    parser.add_argument('--report-file',type=Path,default=None,help='Путь итогового CSV, генерируется автоматически')
    parser.add_argument('--only',help='Ограничить сообщества ID через запятую')
    parser.add_argument('--max-posts',type=int,default=None,help='Сколько разных постов проверить (для --run-actions по умолчанию удвоен больший счётчик)')
    parser.add_argument('--read-pause',type=float,default=None,help='Пауза между запросами в секундах, не ниже конфигурации')
    parser.add_argument('--confirm',action='store_true',help='Явное разрешение на действия этого запуска')
    parser.add_argument('--config',type=Path,default=multi.CONFIG_PATH)
    parser.add_argument('--account',type=int,choices=(1,2),default=1,
                        help='Локальный номер Chromium-профиля (№2: отдельные статусы и ручной запуск)')
    parser.add_argument('--test-actions',choices=('like','repost','both'),default=None,
                        help='Для --test-post: только лайк, только репост или оба действия')
    args=parser.parse_args(argv)
    if args.login_only or args.check_account or args.accounts_report:
        if args.confirm or args.likes is not None or args.reposts is not None or args.only:
            parser.error('Режим проверки профиля не принимает параметры выполнения действий и выбора постов')
        import vk_accounts
        return vk_accounts.run_account_mode(args.account,
                        'login-only' if args.login_only else
                        'check-account' if args.check_account else 'accounts-report')
    if args.test_post is not None or args.test_status:
        if args.account != 2:
            parser.error('Для теста второго профиля укажи --account 2')
        if args.likes is not None or args.reposts is not None or args.only or args.report_file or args.max_posts is not None:
            parser.error('Тест аккаунта №2 не принимает параметры массовой очереди')
        import vk_account_smoke as smoke
        try:
            if args.test_status:
                if args.confirm or args.test_actions is not None:
                    parser.error('--test-status только читает локальное состояние')
                return smoke.print_test_status(2)
            return smoke.test_post(2,args.test_post,which=args.test_actions or 'both',
                                   confirm=args.confirm,config_path=args.config,
                                   wait_seconds=args.read_pause or 20.0)
        except (smoke.SmokeError, ValueError, OSError, json.JSONDecodeError) as exc:
            print('[SMOKE STOP]',exc)
            return 2
    if args.test_actions is not None:
        parser.error('--test-actions допускается только с --test-post')
    if args.account == 2 and (args.preview or args.run_actions or args.action_status):
        try:
            return run_account2_mode(args, parser)
        except (ValueError, RunAlreadyActive, OSError, json.JSONDecodeError) as exc:
            print('[ACCOUNT 2 STOP]',exc)
            return 2
        except engine.PlatformRateLimited as exc:
            import vk_account_smoke as smoke
            with closing(engine.db_open(smoke.ACCOUNT2_DB)) as db:
                engine.lock_on_platform_warning(db,0,str(exc))
            print('[RATE LIMIT] VK ограничил запросы; дальнейшие действия второго аккаунта запрещены.')
            return 2
        except engine.StopRun as exc:
            import vk_account_smoke as smoke
            message=str(exc)
            if any(word in message.lower() for word in ('captcha','капча','ограничени','подозрительная активность','робот')):
                with closing(engine.db_open(smoke.ACCOUNT2_DB)) as db:
                    engine.lock_on_platform_warning(db,0,message)
            print('[ACCOUNT 2 STOP]',exc)
            return 2
        except (PWTimeout, PlaywrightError) as exc:
            print('[ACCOUNT 2 STOP] Chromium:',type(exc).__name__)
            return 2
        except RuntimeError as exc:
            # Covers missing/mismatched account binding, invalid shared catalog
            # and preserved locks without exposing credentials or DOM.
            print('[ACCOUNT 2 STOP]',exc)
            return 2
        except KeyboardInterrupt:
            print('[ACCOUNT 2 STOP] Прервано; история сохранена в SQLite №2')
            return 130
    if args.account != 1:
        parser.error('Для аккаунта №2 доступны --test-post, --preview, --run-actions и --action-status; '
                     'остальные команды запускай с --account 1')
    command_mode = args.run_actions or (args.run_pairs and
                        (args.likes is not None or args.reposts is not None))
    command_likes = 0 if args.likes is None else args.likes
    command_reposts = 0 if args.reposts is None else args.reposts
    if (args.likes is not None or args.reposts is not None) and not (args.run_actions or args.run_pairs):
        parser.error('--likes/--reposts только с --run-actions или --run-pairs')
    if command_mode and (command_likes < 0 or command_reposts < 0
                         or command_likes + command_reposts == 0):
        parser.error('Для числового запуска укажи --likes N и/или --reposts N; N>=0, хотя бы одно >0')
    if args.report_file is None:
        args.report_file = ACTION_RESULTS_DEFAULT if command_mode or args.action_status else RESULT_DEFAULT
    if args.max_posts is None:
        args.max_posts = (max(command_likes,command_reposts)*2 if command_mode
                          else 5 if args.run_pairs else 12)
    if not 1<=args.goal_likes<=60 or not 1<=args.goal_pairs<=30 or args.max_posts < 1:
        parser.error('--goal-likes 1..60, --goal-pairs 1..30, --max-posts >=1')
    if not command_mode and args.max_posts>90:
        parser.error('В прежних режимах --max-posts не более 90')
    if args.run_pairs and not command_mode and args.max_posts>10:
        parser.error('Для парной кампании не более 10 публикаций за запуск')
    if args.run and args.max_posts>30:
        parser.error('Для прежнего режима лайков не более 30 публикаций за запуск')
    if args.read_pause is not None and not 5<=args.read_pause<=300:
        parser.error('--read-pause должен быть 5..300 секунд')
    if (args.run or args.run_pairs or args.run_actions) and not args.confirm:
        parser.error('Для реальных действий обязательно --confirm')
    if args.confirm and not (args.run or args.run_pairs or args.run_actions):
        parser.error('--confirm используется только с --run, --run-pairs или --run-actions')
    try:
        cfg=multi.load_config(args.config)
        selected=multi.selected_groups(cfg,args.only,0)
        if not selected:
            raise ValueError('Не найдено активных сообществ')
    except (ValueError,OSError,json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if args.read_pause is not None:
        cfg['read_pause_seconds']=max(cfg['read_pause_seconds'],args.read_pause)
    cfg['read_pause_seconds']=max(cfg['read_pause_seconds'],20.0)
    print(f'VK UNIFIED v{VERSION} | аккаунт=1, групп={len(selected)}, окно={cfg["lookback_days"]} дней')
    print('[SAFETY] Цель теста — максимум; гарантий достижения нет. При ограничении VK — остановка.')
    try:
        if args.status or args.preview or args.api_report or args.preview_pairs or args.pair_status or args.action_status:
            with closing(engine.db_open(engine.DB_PATH)) as db:
                multi.init_dates(db)
                init_campaign(db)
                api.init_api(db)
                if args.action_status:
                    print_command_action_status(db)
                    try:
                        export_command_action_results(db, selected, args.report_file)
                    except OSError as exc:
                        print('[CSV WARNING] Закрой отчёт в Excel:',exc)
                elif args.preview_pairs:
                    init_pair_campaign(db)
                    current=db.execute('SELECT * FROM pair_experiments WHERE name=?',
                                       (args.pair_name,)).fetchone()
                    print_auto_pair_preview(db,selected,cfg['lookback_days'],current,args.max_posts)
                elif args.pair_status:
                    print_pair_status(db,args.pair_name)
                    current=db.execute('SELECT * FROM pair_experiments WHERE name=?',
                                       (args.pair_name,)).fetchone()
                    if current:
                        try:
                            export_auto_pair_results(db,current,selected,args.report_file)
                        except OSError as exc:
                            print('[CSV WARNING] Закрой CSV в Excel и повтори --pair-status:',exc)
                elif args.status:
                    print_status(db,cfg,selected,args.name)
                    print_pair_status(db,args.pair_name)
                elif args.api_report:
                    api.api_scan_report(db,selected,
                        min_interval_hours=cfg['scan_min_interval_hours'],details=True)
                else:
                    rows=pending_recent_likes(db,selected,cfg['lookback_days'],limit=args.max_posts)
                    print(f'[PREVIEW] Кандидатов из VK API за {cfg["lookback_days"]} дней: {len(rows)}')
                    for row in rows:
                        at=datetime.fromtimestamp(row['published_at'],timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
                        print(f'   {at} | https://vk.com/wall-{row["group_id"]}_{row["post_id"]} | api-verified')
            return 0
        with exclusive_run():
            with closing(engine.db_open(engine.DB_PATH)) as db:
                multi.init_dates(db)
                multi.init_scan_progress(db)
                init_campaign(db)
                engine.require_platform_unlocked(db)
                if args.discover:
                    discover_api(db,cfg,selected,required=True,token=api.configured_token())
                    return 0
                if command_mode:
                    if command_likes and not cfg['actions']['like']:
                        raise ValueError('Глобально выключено actions.like в vk_groups.json')
                    if command_reposts and not cfg['actions']['repost']:
                        raise ValueError('Глобально выключено actions.repost в vk_groups.json')
                    print('[API QUEUE] Только ID и точные даты из wall.get; '
                          'никакого браузерного сканирования.')
                    print('[LIMITS] Число действий задано командой; суточные квоты JSON '
                          'в этом режиме не действуют.')
                    try:
                        run_command_actions(db,cfg,selected,likes=command_likes,
                                            reposts=command_reposts,max_posts=args.max_posts,
                                            pause=cfg['read_pause_seconds'])
                    finally:
                        try:
                            export_command_action_results(db,selected,args.report_file)
                        except (OSError,ValueError) as exc:
                            print('[CSV WARNING] Не удалось записать отчёт:',exc)
                    return 0
                if args.run_pairs:
                    if not (cfg['actions']['like'] and cfg['actions']['repost']):
                        raise ValueError('В vk_groups.json должны быть включены actions.like и actions.repost')
                    enabled=[g['id'] for g in selected
                             if g.get('like_enabled') and g.get('repost_enabled')]
                    campaign=start_auto_pair_campaign(db,args.pair_name,args.goal_pairs,
                                                     enabled,cfg['lookback_days'])
                    print('[API QUEUE] Автовыбор свежих публикаций по wall.get ID/дате из SQLite; '
                          'CSV используется только для отчёта.')
                    try:
                        run_approved_pairs(db,cfg,selected,campaign,
                                           max_posts=args.max_posts,
                                           pause=cfg['read_pause_seconds'],auto_mode=True)
                    finally:
                        try:
                            export_auto_pair_results(db,campaign,selected,args.report_file)
                        except (OSError,ValueError) as exc:
                            print(f'[CSV WARNING] Не удалось сохранить CSV: {exc}')
                    return 0
                # Explicit operator action. API discovery is opportunistic,
                # never replaced with extra Playwright scanning if no token.
                campaign=start_campaign(db,args.name,args.goal_likes,[g['id'] for g in selected])
                if campaign['completed_at']:
                    print('[GOAL] Кампания завершена; никаких новых действий.')
                    return 0
                print('[API QUEUE] Выбор публикаций только по ранее полученным '
                      'wall.get ID и точным датам в SQLite. --run не запускает сканирование.')
                run_likes(db,cfg,selected,campaign,max_posts=args.max_posts,
                          pause=cfg['read_pause_seconds'])
                return 0
    except api.VkApiLimited as exc:
        with closing(engine.db_open(engine.DB_PATH)) as db:
            record_health_event(db,'api-rate-limit',str(exc))
        print('[RATE LIMIT]',exc)
        print('[SAFETY] Защитная блокировка сохранена; не повторяй запросы и действия до восстановления VK.')
        return 2
    except api.VkApiFailure as exc:
        print('[API STOP]',exc)
        return 2
    except engine.PlatformRateLimited as exc:
        with closing(engine.db_open(engine.DB_PATH)) as db:
            engine.lock_on_platform_warning(db,0,str(exc))
            record_health_event(db,'browser-rate-limit',str(exc))
        print('[RATE LIMIT]',exc)
        return 2
    except engine.SafetyLimitReached as exc:
        print('[SAFETY PAUSE]',exc)
        return 0
    except engine.StopRun as exc:
        msg=str(exc)
        if any(s in msg.lower() for s in (
            'vk запросил вход','vk сообщил об ограничении','появилась captcha',
            'подтвердите, что вы не робот','подозрительная активность')):
            with closing(engine.db_open(engine.DB_PATH)) as db:
                engine.lock_on_platform_warning(db,0,msg)
                record_health_event(db,'challenge',msg)
        print('[STOP]',exc)
        return 2
    except (PWTimeout,PlaywrightError) as exc:
        print('[ERROR] Browser:',type(exc).__name__,str(exc)[:180])
        return 2
    except (ValueError,RunAlreadyActive) as exc:
        print('[STOP]',exc)
        return 2
    except KeyboardInterrupt:
        print('[STOP] Прервано Ctrl+C. Текущий прогресс в SQLite.')
        return 130


if __name__=='__main__':
    sys.exit(main())
