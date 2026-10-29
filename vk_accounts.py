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
import time
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
REGISTRY_PATH = ROOT / 'vk_account_registry.json'
PROFILE_LINK = 'a[data-testid="header-profile-menu-button"]'
VK_HOSTS = {'vk.com', 'www.vk.com', 'vk.ru', 'www.vk.ru', 'm.vk.com'}


class AccountError(RuntimeError):
    """A conservative account/profile validation failure."""


class IdentityPreparationDeadline(AccountError):
    """No bounded identity read can fit before the caller's intent deadline."""


def prepare_context_identity(context,*,primary_lock_held=False,deadline=None,before_request=None):
    """Explicit pre-intent preparation; ordinary identity inspection stays offline."""
    import vk_identity_alias
    return vk_identity_alias.prepare_context_identity(context,primary_lock_held=primary_lock_held,
        deadline=deadline,before_request=before_request)


def profile_for_account(account: int) -> Path:
    if type(account) is not int or account not in account_numbers():
        raise ValueError('Нужен зарегистрированный положительный номер аккаунта')
    directory=load_registry()['accounts'].get(str(account),{}).get('profile_dir')
    if directory is not None:
        if not isinstance(directory,str) or not re.fullmatch(r'\.vk-local-profile(?:-[1-9]\d*)?',directory):
            raise AccountError('invalid_profile_directory')
        return ROOT/directory
    return PROFILE_DIRS.get(account, ROOT / f'.vk-local-profile-{account}')


def uses_primary_profile_lock(account):
    return profile_for_account(account)==PROFILE_DIRS[1]


def database_for_account(account: int) -> Path:
    profile_for_account(account)
    return ROOT / ('vk_batch.sqlite3' if account == 1 else f'vk_batch_account_{account}.sqlite3')


def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {'version': 1, 'accounts': {}}
    raw = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    if (not isinstance(raw, dict) or raw.get('version') != 1
            or not isinstance(raw.get('accounts'), dict)):
        raise AccountError('Неверный реестр управления аккаунтами')
    for number, row in raw['accounts'].items():
        if (not isinstance(number,str) or not number.isascii() or not number.isdigit()
                or str(int(number)) != number or not 1 <= int(number) <= 1000
                or not isinstance(row, dict) or type(row.get('enabled')) is not bool):
            raise AccountError('Неверная запись аккаунта')
    return raw


def account_numbers() -> list[int]:
    return sorted({1, 2, *(int(k) for k in load_registry()['accounts'])})


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows readers can briefly deny replacement of JSON files.
                # Retry only this local replacement, never a VK operation.
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==5:raise
                time.sleep(.05*(attempt+1))
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def management_lock(path=None):
    path = Path(path) if path is not None else REGISTRY_PATH.with_suffix('.lock')
    try: fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise AccountError('Изменение аккаунтов уже выполняется') from None
    try:
        os.close(fd)
        yield
    finally: path.unlink(missing_ok=True)


def account_record(account: int) -> dict:
    profile_for_account(account)
    import vk_schedule_policy as schedule
    defaults = schedule.DEFAULT_CONFIG['accounts'].get(str(account),
        {'likes':0, 'personal_reposts':0, 'community_reposts':0, 'communities':[]})
    return {'account':account, 'name':f'Аккаунт {account}', 'enabled':account in (1,2),
            'policy':dict(defaults), **load_registry()['accounts'].get(str(account), {})}


def account_enabled(account: int) -> bool:
    row=account_record(account)
    return row['enabled'] and not row.get('archived',False) and not row.get('connection_pending',False)


def prepare_account_database(account: int) -> None:
    """Prepare the actor ledger before publishing a newly allocated number."""
    if type(account) is not int or not 1<=account<=1000:raise ValueError('Invalid account number')
    from contextlib import closing
    import vk_batch as engine
    path=ROOT/('vk_batch.sqlite3' if account==1 else f'vk_batch_account_{account}.sqlite3')
    with closing(engine.db_open(path)):pass


def add_account(name: str = '') -> dict:
    if not isinstance(name, str) or len(name.strip()) > 80 or any(ord(c)<32 for c in name):
        raise ValueError('Имя аккаунта должно быть не длиннее 80 символов')
    with management_lock():
        registry = load_registry()
        number = max(account_numbers()) + 1
        if number > 1000: raise AccountError('Достигнут предел локальных профилей')
        flexible=registry.get('account_flow')=='add_any'
        row = {'account':number, 'name':name.strip() or ('Новый аккаунт' if flexible else f'Аккаунт {number}'), 'enabled':False,
               'policy':{'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[]}}
        registry['accounts'][str(number)] = row
        prepare_account_database(number)
        atomic_json(REGISTRY_PATH, registry)
    return row


def configure_account(account: int, **updates) -> dict:
    import vk_schedule_policy as schedule
    allowed = {'enabled','likes','personal_reposts','community_reposts','communities','name'}
    if set(updates)-allowed: raise ValueError('Неизвестная настройка аккаунта')
    with management_lock():
        row = account_record(account)
        enabled = updates.get('enabled', row['enabled'])
        if type(enabled) is not bool: raise ValueError('enabled должен быть boolean')
        if enabled:
            if row.get('archived') or row.get('connection_pending'):raise AccountError('account_not_connected')
            login_file=ROOT/f'.vk-login-{account}.json'
            if login_file.exists() and json.loads(login_file.read_text(encoding='utf-8')).get('state') in ('opening','waiting_browser','awaiting_login','ready'):
                raise AccountError('Сначала завершите открытый сеанс входа')
            binding = load_bindings(BINDINGS_PATH)['accounts'].get(str(account), {})
            if type(binding.get('vk_id')) is not int: raise AccountError('Сначала подтвердите VK ID')
            import vk_authentication
            vk_authentication.require_matched(account)
        limits = {**row['policy'], **{k:v for k,v in updates.items() if k in row['policy']}}
        # Validate both this record and total schedule capacity before writing.
        if type(limits.get('community_reposts')) is int and limits['community_reposts']>0 and limits.get('communities')==[]:
            raise AccountError('community_destinations_required')
        schedule.validate_account_policy(limits)
        registry = load_registry()
        row = {**row, 'enabled':enabled, 'policy':limits}
        if 'name' in updates:
            name=updates['name']
            if not isinstance(name,str) or not name.strip() or len(name)>80 or any(ord(c)<32 for c in name):
                raise ValueError('Неверное имя аккаунта')
            row['name']=name.strip()
        registry['accounts'][str(account)] = row
        overrides = policy_overrides(registry)
        for value in overrides.values():schedule.validate_account_policy(value)
        try:
            cfg = future_policy(registry)
            schedule.build_schedule(datetime.now(timezone.utc), cfg)
        except ValueError as exc:
            raise AccountError('account_schedule_capacity') from exc
        atomic_json(REGISTRY_PATH, registry)
        return row


def policy_overrides(registry):
    return {k:({'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[]}
               if r.get('archived') or r.get('connection_pending') else r['policy'])
            for k,r in registry['accounts'].items()}


def future_policy(registry):
    """Validate pending quotas against the scheduler's configured work window.

    Existing saved days are immutable and are selected by State.refresh_policy.
    This policy is only for capacity checks and the next unplanned day.
    """
    import vk_schedule_policy as schedule
    path=ROOT/'vk_automation.json'
    base=json.loads(path.read_text(encoding='utf-8')).get('policy',{}) if path.exists() else {}
    base=schedule.validate_config(base)
    return schedule.validate_config({**base,'accounts':{**base['accounts'],**policy_overrides(registry)}})


def effective_policy(base: dict) -> dict:
    import vk_schedule_policy as schedule
    overrides = policy_overrides(load_registry())
    cfg = {**base, 'accounts':{**base['accounts'], **overrides}}
    return schedule.validate_config(cfg)


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
    with management_lock(Path(path).with_suffix('.binding.lock')):
        return _register_detected_id(path, account, observed_id)


def _register_detected_id(path: Path, account: int, observed_id: int) -> tuple[str, dict]:
    """Bind the numeric ID once; never silently replace an existing identity.

    Result: ('bound'|'matched', registry). Does not touch browser profiles.
    """
    profile_for_account(account)
    if type(observed_id) is not int or observed_id <= 0:
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
def exclusive_activity(root=None):
    """Coordinate a whole worker batch with login/statistics readers.

    An OS lease releases on crash. Its persistent file is never removed as a
    way to free a browser or a worker; nested global/profile locks still apply.
    """
    from vk_scheduler import singleton
    with singleton(Path(root) / '.vk-activity.guard' if root is not None else ROOT / '.vk-activity.guard'):
        yield


@contextmanager
def exclusive_profile(account: int):
    profile=profile_for_account(account)
    row=load_registry()['accounts'].get(str(account),{})
    physical=account
    if row.get('profile_dir'):
        suffix=profile.name.removeprefix('.vk-local-profile')
        physical=int(suffix[1:]) if suffix else 1
    lock = LOCK_DIRS.get(physical, ROOT / f'.vk-unified-running-{physical}.lock')
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
        href=candidates.first.get_attribute('href', timeout=2500)
        number=vk_id_from_profile_href(href)
        if number is not None:return number
        import vk_identity_alias
        return vk_identity_alias.prepared_header_identity(page,href)
    except (PWTimeout, PlaywrightError, AttributeError):
        return None


def inspect_context(context, attempts: int = 6) -> int | None:
    """Wait for the React header of an authenticated VK page; never guess IDs.

    Some VK login flows open extra tabs and redirect through VK ID; only a
    VK-hosted tab with the exact signed-in header control is accepted.
    """
    for i in range(attempts):
        import vk_identity_alias
        contextual,prepared=vk_identity_alias.prepared_context_identity(context)
        if contextual:
            if prepared is not None:return prepared
            if i<attempts-1:
                active=[p for p in context.pages if not p.is_closed()]
                if active:active[-1].wait_for_timeout(1000)
            continue
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
    if interactive and Path(binding_path).resolve()==Path(BINDINGS_PATH).resolve():
        import vk_connections
        if vk_connections.enabled():
            import vk_control
            vk_control.start_login(account)
            print('[LOGIN] Окно VM: войдите в выбранный VK. Профиль сохранится автоматически.')
            return 0
    profile = profile_for_account(account)
    authentication = None
    if Path(binding_path).resolve() == Path(BINDINGS_PATH).resolve():
        import vk_authentication as authentication
        authentication.invalidate(account)

    def inspect_and_observe(context):
        try:
            observed = inspect_context(context)
            if observed is None:
                observed=prepare_context_identity(context,primary_lock_held=uses_primary_profile_lock(account))
        except AccountError:
            if authentication is not None:
                authentication.observe(account, None)
            raise
        if authentication is not None:
            authentication.observe(account, observed)
        return observed

    # No copying of profile, cookies, localStorage or access tokens at any time.
    from vk_activity import shared_activity
    with shared_activity(ROOT), exclusive_profile(account), sync_playwright() as p:
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
                    identity = inspect_and_observe(context)
                    if identity is None:
                        print('[CHECK] Числовой VK ID из шапки страницы пока не найден. '
                              'Убедись, что открыт твой VK и вход завершён.')
                        continue
                    if cmd in {'СТАТУС', 'CHECK'}:
                        print(f'[CHECK] Найден VK ID: {identity}; нажми ГОТОВО для привязки.')
                        continue
                    break
            else:
                identity = inspect_and_observe(context)
                if identity is None:
                    print('[CHECK] Не удалось подтвердить авторизацию по ссылке профиля в шапке VK. '
                          'Ни один аккаунт не привязан и не изменён.')
                    return 2
            try:
                status, _ = register_detected_id(binding_path, account, identity)
                if status == 'bound' and authentication is not None:
                    authentication.observe(account, identity)
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
    for account in account_numbers():
        path = profile_for_account(account)
        row = bindings.get(str(account))
        if isinstance(row, dict) and isinstance(row.get('vk_id'), int):
            state = f'VK ID {row["vk_id"]}; проверен {row.get("verified_at_utc", "неизвестно")}'
        else:
            state = 'личность не привязана'
        record=account_record(account)
        print(f'   №{account}: {record["name"]}; {path.name}; каталог={"есть" if path.is_dir() else "нет"}; {state}; очередь={record["enabled"]}; лимиты={record["policy"]}')
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
