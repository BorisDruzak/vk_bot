#!/usr/bin/env python3
"""Local, read-only management of two separately authenticated VK browser profiles.

Account 1 uses the existing Chromium user data dir unchanged; account 2 gets
its own independent directory. This module performs no engagement actions,
does not retrieve or export cookies, and fails closed on unknown account ID.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PWTimeout, sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE_DIRS = {
    1: ROOT / '.vk-local-profile',  # DO NOT MOVE: existing v6.23 session.
    2: ROOT / '.vk-local-profile-2',
}
LOCK_DIRS = {
    1: ROOT / '.vk-unified-running.lock',  # The v6.23 lock for account 1.
    2: ROOT / '.vk-unified-running-2.lock',
}
BINDINGS_PATH = ROOT / 'vk_account_bindings.json'
PROFILE_LINK = 'a[data-testid="header-profile-menu-button"]'
VK_HOSTS = {'vk.com', 'www.vk.com', 'vk.ru', 'www.vk.ru', 'm.vk.com'}


class AccountError(RuntimeError):
    """A conservative account/profile validation failure."""


def profile_for_account(account: int) -> Path:
    if account not in PROFILE_DIRS:
        raise ValueError('Поддерживаются номера аккаунтов 1 и 2')
    return PROFILE_DIRS[account]


def vk_id_from_profile_href(href: str | None) -> int | None:
    """Only header's OWN numeric /idNNN link identifies the logged-in user.

    Do not parse arbitrary post/comment links; they are not the logged-in ID.
    Do not use page cookies or VK's browser-side access tokens.
    """
    if not isinstance(href, str) or not href.strip():
        return None
    url = urlsplit(urljoin('https://vk.com/', href.strip()))
    if url.scheme != 'https' or (url.hostname or '').lower() not in VK_HOSTS:
        return None
    found = re.fullmatch(r'/id([1-9]\d{0,17})/?', url.path)
    return int(found.group(1)) if found else None


def load_bindings(path: Path = BINDINGS_PATH) -> dict:
    if not path.exists():
        return {'version': 1, 'accounts': {}}
    raw = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(raw, dict) or raw.get('version') != 1
            or not isinstance(raw.get('accounts'), dict)):
        raise AccountError('Неверный формат реестра аккаунтов. Файл не изменён.')
    return raw


def register_detected_id(path: Path, account: int, observed_id: int) -> tuple[str, dict]:
    """Bind the numeric ID once; never silently replace an existing identity.

    Result: ('bound'|'matched', registry). Does not touch browser profiles.
    """
    profile_for_account(account)
    if not isinstance(observed_id, int) or observed_id <= 0:
        raise AccountError('VK ID не подтверждён: профиль не привязан')
    registry = load_bindings(path)
    accounts = registry['accounts']
    key = str(account)
    for other, record in accounts.items():
        if other != key and isinstance(record, dict) and record.get('vk_id') == observed_id:
            raise AccountError('Один и тот же VK ID обнаружен у двух профилей. '
                               'Проверь вход во второй аккаунт; привязка запрещена.')
    current = accounts.get(key)
    if current and (not isinstance(current, dict) or current.get('vk_id') != observed_id):
        raise AccountError('VK ID отличается от ранее закреплённого за этим профилем. '
                           'Ничего не меняю; проверь переключение аккаунта в браузере.')
    status = 'matched' if current else 'bound'
    accounts[key] = {'vk_id': observed_id, 'verified_at_utc': datetime.now(timezone.utc).isoformat(),
                     'profile': profile_for_account(account).name}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp',
                                         delete=False) as output:
            tmp_name = output.name
            json.dump(registry, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)
    return status, registry


@contextmanager
def exclusive_profile(account: int):
    profile_for_account(account)
    lock = LOCK_DIRS[account]
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise AccountError(f'Профиль №{account} уже занят или остался lock-файл. '
                           'Проверь процессы Chromium перед удалением lock вручную.') from None
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(f'VK account {account}: read-only login / identity inspection\n')
        yield
    finally:
        lock.unlink(missing_ok=True)


def inspect_header_identity(page) -> int | None:
    """Do not inspect general links: only the signed-in header profile button."""
    try:
        if page.is_closed() or (urlsplit(page.url).hostname or '').lower() not in VK_HOSTS:
            return None
        candidates = page.locator(PROFILE_LINK)
        if candidates.count() != 1:
            return None
        return vk_id_from_profile_href(candidates.first.get_attribute('href', timeout=2500))
    except (PWTimeout, PlaywrightError, AttributeError):
        return None


def inspect_context(context, attempts: int = 6) -> int | None:
    """Wait for the React header of an authenticated VK page; never guess IDs.

    Some VK login flows open extra tabs and redirect through VK ID; only a
    VK-hosted tab with the exact signed-in header control is accepted.
    """
    for i in range(attempts):
        found = set()
        for page in list(context.pages):
            number = inspect_header_identity(page)
            if number:
                found.add(number)
        if len(found) == 1:
            return next(iter(found))
        if len(found) > 1:
            raise AccountError('В разных вкладках найдены разные VK ID — '
                               'для безопасности аккаунт не подтверждён')
        if i < attempts - 1:
            active = [p for p in context.pages if not p.is_closed()]
            if active:
                active[-1].wait_for_timeout(1000)
    return None


def browser_inspect(account: int, interactive: bool, *, binding_path: Path = BINDINGS_PATH) -> int:
    profile = profile_for_account(account)
    # No copying of profile, cookies, localStorage or access tokens at any time.
    with exclusive_profile(account), sync_playwright() as p:
        print(f'[PROFILE] Аккаунт №{account}; Chromium: {profile}')
        print('[PROFILE] Действия с публикациями отключены. Данные входа остаются в профиле Chromium.')
        profile.mkdir(parents=True, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, locale='ru-RU',
            viewport={'width': 1320, 'height': 900})
        try:
            page = context.new_page()
            try:
                page.goto('https://vk.com/feed', wait_until='domcontentloaded', timeout=35000)
            except (PWTimeout, PlaywrightError) as exc:
                print(f'[LOGIN] VK пока не загрузился ({type(exc).__name__}). '
                      'Если окно открыто — продолжай вход вручную.')
            if interactive:
                print('[LOGIN] Войди в аккаунт самостоятельно; пройди VK ID / 2FA при необходимости.')
                print('[LOGIN] Когда видна лента VK, вернись в CMD.')
                while True:
                    try:
                        cmd = input('[LOGIN] Enter/ГОТОВО — проверить и закрыть; '
                                    'СТАТУС — проверить без закрытия; ВЫХОД — отмена: ').strip().upper()
                    except EOFError:
                        print('[STOP] Нет интерактивного ввода; привязка не выполнена.')
                        return 2
                    if cmd in {'ВЫХОД', 'EXIT', 'Q'}:
                        print('[LOGIN] Отменено. Профиль сохранён, данные не удалены.')
                        return 1
                    if cmd not in {'', 'ГОТОВО', 'СТАТУС', 'CHECK'}:
                        print('[LOGIN] Неизвестная команда.')
                        continue
                    identity = inspect_context(context)
                    if identity is None:
                        print('[CHECK] Числовой VK ID из шапки страницы пока не найден. '
                              'Убедись, что открыт твой VK и вход завершён.')
                        continue
                    if cmd in {'СТАТУС', 'CHECK'}:
                        print(f'[CHECK] Найден VK ID: {identity}; нажми ГОТОВО для привязки.')
                        continue
                    break
            else:
                identity = inspect_context(context)
                if identity is None:
                    print('[CHECK] Не удалось подтвердить авторизацию по ссылке профиля в шапке VK. '
                          'Ни один аккаунт не привязан и не изменён.')
                    return 2
            try:
                status, _ = register_detected_id(binding_path, account, identity)
            except AccountError as exc:
                print('[MISMATCH]', exc)
                return 3
            print(f'[CHECK] Аккаунт №{account}: VK ID {identity} — '
                  f'{"впервые привязан" if status == "bound" else "совпадает с сохранённым"}.')
            print('[CHECK] Cookies не экспортируются. Повторная проверка выполняется без ввода пароля, '
                  'пока VK сохраняет авторизацию.')
            return 0
        finally:
            context.close()


def accounts_report(binding_path: Path = BINDINGS_PATH) -> int:
    bindings = load_bindings(binding_path)['accounts']
    print('[ACCOUNTS] Локальные профили, без обращения к VK:')
    for account in (1, 2):
        path = profile_for_account(account)
        row = bindings.get(str(account))
        if isinstance(row, dict) and isinstance(row.get('vk_id'), int):
            state = f'VK ID {row["vk_id"]}; проверен {row.get("verified_at_utc", "неизвестно")}'
        else:
            state = 'личность не привязана'
        print(f'   №{account}: {path.name}; каталог={"есть" if path.is_dir() else "нет"}; {state}')
    print('[ACCOUNTS] Аккаунт №2: отдельная SQLite и ручные --test-post/--run-actions; '
          'синхронные задания двух аккаунтов не поддерживаются.')
    return 0


def run_account_mode(account: int, mode: str) -> int:
    try:
        if mode == 'accounts-report':
            return accounts_report()
        return browser_inspect(account, interactive=(mode == 'login-only'))
    except (AccountError, OSError, ValueError, json.JSONDecodeError) as exc:
        print('[STOP]',exc)
        return 2
    except (PWTimeout, PlaywrightError) as exc:
        # No URLs, cookies, request headers or query strings in diagnostics.
        print(f'[BROWSER STOP] {type(exc).__name__}: не удалось открыть/проверить Chromium-профиль.')
        return 2
    except KeyboardInterrupt:
        print('[STOP] Прервано; данные профиля не удаляются.')
        return 130


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--login-only', action='store_true')
    modes.add_argument('--check-account', action='store_true')
    modes.add_argument('--accounts-report', action='store_true')
    parser.add_argument('--account', type=int, choices=(1, 2), default=1)
    opts = parser.parse_args()
    mode = ('login-only' if opts.login_only else 'check-account' if opts.check_account
            else 'accounts-report')
    raise SystemExit(run_account_mode(opts.account, mode))
