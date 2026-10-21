#!/usr/bin/env python3
"""A one-post smoke test for separately authenticated VK account #2.

This is intentionally NOT a multi-account task runner. Public VK post IDs are
read from account #1's API catalog *read-only*; authenticated observations and
all write attempts are stored in a separate, identity-bound account #2 SQLite.
No credentials, cookies, private VK page HTML or token values are inspected.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PWTimeout, sync_playwright

import vk_accounts as accounts
import vk_batch as engine
import vk_groups as multi

ROOT = Path(__file__).resolve().parent
ACCOUNT2_DB = ROOT / 'vk_batch_account_2.sqlite3'
CATALOG_DB = engine.DB_PATH
POST_RX = re.compile(r'([1-9]\d{0,14})_([1-9]\d{0,14})\Z')
WALL_RX = re.compile(r'/wall-([1-9]\d{0,14})_([1-9]\d{0,14})\Z')


class SmokeError(RuntimeError):
    """Preflight failed. No action is authorized."""


def parse_post(value: str) -> tuple[int, int]:
    """Only group-owned wall posts; no arbitrary VK page or profile links."""
    raw = value.strip()
    match = POST_RX.fullmatch(raw)
    if match is None and raw.startswith('https://'):
        url = urlsplit(raw)
        if url.hostname not in {'vk.com', 'www.vk.com', 'vk.ru', 'www.vk.ru'} or url.query or url.fragment:
            raise SmokeError('Укажи прямой URL публичной публикации vk.com/wall-GROUP_POST')
        match = WALL_RX.fullmatch(url.path)
    if match is None:
        raise SmokeError('Формат публикации: GROUP_POST, например 188206921_13418')
    return int(match.group(1)), int(match.group(2))


def pinned_user_id(account: int = 2, *, binding_path: Path = accounts.BINDINGS_PATH) -> int:
    if account != 2:
        raise SmokeError('Тест действий v6.25 доступен только для аккаунта №2')
    bindings = accounts.load_bindings(binding_path)['accounts']
    record = bindings.get('2')
    identity = record.get('vk_id') if isinstance(record, dict) else None
    if type(identity) is not int or identity <= 0:
        raise SmokeError('Сначала выполни --account 2 --check-account для привязки VK ID')
    first = bindings.get('1')
    if isinstance(first, dict) and first.get('vk_id') == identity:
        raise SmokeError('Профили №1 и №2 имеют один VK ID; выполнение запрещено')
    return identity


def catalog_post(group_id: int, post_id: int, *, catalog_db: Path = CATALOG_DB,
                 days: int = 7, now: int | None = None) -> int:
    """Retrieve an exact, recent public post; never trust account #1 reactions."""
    if not catalog_db.exists():
        raise SmokeError('Общий каталог VK API отсутствует; сначала --discover под аккаунтом №1')
    cutoff = (int(datetime.now(timezone.utc).timestamp()) if now is None else now) - days * 86400
    path = catalog_db.resolve().as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(path, uri=True)) as db:
            row = db.execute('''SELECT a.published_at FROM api_seen_posts a
               JOIN post_publication d ON d.group_id=a.group_id AND d.post_id=a.post_id
               WHERE a.group_id=? AND a.post_id=? AND a.valid=1
                 AND d.exact=1 AND d.published_at=a.published_at
                 AND a.published_at BETWEEN ? AND ?''', (group_id, post_id, cutoff, cutoff+days*86400+3600)).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SmokeError('Общий API-каталог нельзя безопасно прочитать') from exc
    if row is None:
        raise SmokeError('Публикация отсутствует в точном VK API-каталоге за последние 7 дней')
    return int(row[0])


def init_account2(db, vk_id: int) -> None:
    """First use accepts ONLY a fresh empty DB; never mistake copied #1 DB for #2."""
    db.execute('''CREATE TABLE IF NOT EXISTS local_account_identity (
       id INTEGER PRIMARY KEY CHECK (id=1), vk_id INTEGER NOT NULL,
       assigned_at TEXT NOT NULL)''')
    row = db.execute('SELECT vk_id FROM local_account_identity WHERE id=1').fetchone()
    if row:
        if row['vk_id'] != vk_id:
            raise SmokeError('SQLite второго аккаунта принадлежит другому VK ID; действия запрещены')
        return
    # A legacy DB without the identity binding must NEVER silently become #2.
    for table in ('posts', 'post_audit', 'action_attempts'):
        count = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        if count:
            raise SmokeError('В отдельной базе обнаружены старые статусы без привязки VK ID. '
                             'Проверь файл, перенос истории запрещён.')
    db.execute('INSERT INTO local_account_identity(id,vk_id,assigned_at) VALUES(1,?,?)',
               (vk_id, engine.moment()))
    db.commit()


class OnePostAttemptRecorder:
    """One like and one repost maximum; persist attempt before the real click."""
    def __init__(self, db, group_id: int, post_id: int):
        self.db, self.group_id, self.post_id = db, group_id, post_id
        self.reserved: set[str] = set()

    def reserve(self, post_id: int, action: str):
        if post_id != self.post_id or action not in {'like', 'repost'} or action in self.reserved:
            raise SmokeError('Попытка не соответствует выбранному посту либо уже выполнялась')
        engine.require_platform_unlocked(self.db)
        self.db.execute('''INSERT INTO action_attempts(group_id,post_id,action,attempted_at)
                           VALUES(?,?,?,?)''', (self.group_id, post_id, action, engine.moment()))
        self.db.commit()
        self.reserved.add(action)


def _live_identity(context, expected_id: int) -> None:
    observed = accounts.inspect_context(context, attempts=4)
    if observed != expected_id:
        raise SmokeError('VK ID в текущем Chromium не совпадает с привязанным к профилю №2. '
                         'Никаких нажатий.')


def _check_config_group(config_path: Path, group_id: int, actions: set[str]) -> int:
    cfg = multi.load_config(config_path)
    group = next((item for item in cfg['groups'] if item['id'] == group_id and item.get('enabled', True)), None)
    if group is None:
        raise SmokeError('Сообщество не входит в активный список групп')
    for action in actions:
        if not cfg['actions'].get(action, False) or not group.get(f'{action}_enabled'):
            raise SmokeError(f'Действие {action} для сообщества отключено в конфигурации')
    return cfg['lookback_days']


def test_post(account: int, post_ref: str, *, which: str = 'both',
              confirm: bool = False, binding_path: Path = accounts.BINDINGS_PATH,
              catalog_db: Path = CATALOG_DB, account_db: Path = ACCOUNT2_DB,
              config_path: Path = multi.CONFIG_PATH,
              wait_seconds: float = 20.0) -> int:
    """Smoke test: one explicitly named API post, no background or batch queue."""
    if account != 2:
        raise SmokeError('Для v6.25 тест действий доступен только в профиле №2')
    if which not in ('like', 'repost', 'both'):
        raise SmokeError('Допускаются только действия like, repost или both')
    actions = {'like', 'repost'} if which == 'both' else {which}
    group_id, post_id = parse_post(post_ref)
    vk_id = pinned_user_id(account, binding_path=binding_path)
    days = _check_config_group(config_path, group_id, actions)
    timestamp = catalog_post(group_id, post_id, catalog_db=catalog_db, days=days)
    if not accounts.profile_for_account(2).is_dir():
        raise SmokeError('Отдельный профиль Chromium №2 отсутствует; сначала --check-account')
    if accounts.LOCK_DIRS[1].exists():
        raise SmokeError('Аккаунт №1 сейчас занят; параллельные запуски запрещены')
    print(f'[SMOKE] Аккаунт №2, VK ID {vk_id}, публикация https://vk.com/wall-{group_id}_{post_id}')
    print(f'[SMOKE] Режим: {"ДЕЙСТВИЯ разрешены, один пост" if confirm else "ТОЛЬКО ЧТЕНИЕ"}; '
          f'виды: {", ".join(sorted(actions))}')
    with accounts.exclusive_profile(2), closing(engine.db_open(account_db)) as db:
        init_account2(db, vk_id)
        engine.require_platform_unlocked(db)
        try:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(accounts.profile_for_account(2)),
                    headless=False, locale='ru-RU', viewport={'width': 1320, 'height': 900})
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    engine.watch_rate_limit_responses(page)
                    engine.configure_navigation_pacing(page, max(wait_seconds, 20.0), db=db)
                    engine.go_to_post(page, group_id, post_id)
                    _live_identity(context, vk_id)
                    live_date = multi.exact_post_date_info(page, group_id, post_id).get('publishedAt')
                    if type(live_date) is not int or abs(live_date-timestamp) > 1:
                        raise SmokeError('Дата поста на странице отличается от подтверждённой VK API; действий не было')
                    live = engine.refresh_state(page, group_id, post_id)
                    if live['liked'] is None or live['reposted'] is None:
                        raise SmokeError('Состояние лайка/репоста не подтверждено VK; действий не было')
                    footer = engine.wait_post_footer_info(page, group_id, post_id, 'like')
                    if not footer['ok'] or footer['liked'] != live['liked']:
                        raise SmokeError('Основной лайк поста и данные VK не согласованы; действий не было')
                    print(f'[SMOKE] Текущее состояние у №2: like={live["liked"]}, repost={live["reposted"]}')
                    if not confirm:
                        print('[SMOKE] Никаких кликов. Для разового теста добавь --confirm.')
                        return 0
                    engine.add_posts(db, group_id, [post_id])
                    row = engine.target_row(db, group_id, post_id)
                    for action in actions:
                        if row[f'{action}_status'] in ('uncertain', 'error', 'unavailable'):
                            raise SmokeError(f'{action}: в SQLite №2 статус {row[f"{action}_status"]}; '
                                             'повторное действие без разбирательства запрещено')
                        if row[f'{action}_status'] in ('verified', 'already') and not live[
                                'liked' if action == 'like' else 'reposted']:
                            raise SmokeError(f'{action}: SQLite №2 говорит, что действие было, '
                                             'но VK его не подтверждает. Никаких кликов.')
                    recorder = OnePostAttemptRecorder(db, group_id, post_id)
                    for action in ('like','repost'):
                        if action not in actions:
                            continue
                        _live_identity(context, vk_id)
                        row = engine.target_row(db, group_id, post_id)
                        engine.process_one(page, db, group_id, row, pause=1.0,
                                           safety=recorder, actions={action}, already_open=True)
                    print('[SMOKE] Проверка второго профиля закончена. Статусы записаны только в SQLite №2.')
                    return 0
                finally:
                    context.close()
        except engine.PlatformRateLimited as exc:
            engine.lock_on_platform_warning(db, 0, str(exc))
            print('[RATE LIMIT] VK ограничил запросы; профиль №2 заблокирован в своей SQLite.')
            return 2
        except (engine.StopRun, SmokeError, accounts.AccountError) as exc:
            message = str(exc)
            if 'CAPTCHA' in message or 'VK сообщил об ограничении' in message:
                engine.lock_on_platform_warning(db,0,message)
            print('[STOP]', exc)
            return 2
        except (PWTimeout, PlaywrightError):
            print('[STOP] Ошибка Chromium; состояние после возможного клика проверяй вручную.')
            return 2


def print_test_status(account: int = 2, *, binding_path: Path = accounts.BINDINGS_PATH,
                      db_path: Path = ACCOUNT2_DB) -> int:
    vk_id = pinned_user_id(account, binding_path=binding_path)
    if not db_path.exists():
        print('[SMOKE STATUS] Отдельной базы №2 ещё нет; история №1 не используется.')
        return 0
    with closing(engine.db_open(db_path)) as db:
        init_account2(db, vk_id)
        print(f'[SMOKE STATUS] Аккаунт №2, VK ID {vk_id}; база {db_path.name}')
        engine.safety_report(db, 0)
        for row in db.execute('''SELECT group_id,post_id,like_status,repost_status
            FROM posts ORDER BY seen_at DESC LIMIT 12'''):
            print(f'   https://vk.com/wall-{row["group_id"]}_{row["post_id"]} '
                  f'| like={row["like_status"]} repost={row["repost_status"]}')
        attempts = dict(db.execute('SELECT action,COUNT(*) FROM action_attempts GROUP BY action').fetchall())
        print('[SMOKE STATUS] Попыток №2:', attempts)
    return 0
