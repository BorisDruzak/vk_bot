# VK Unified v6.30 — перенос и проверка

Дата: 23 октября 2026, Asia/Yekaterinburg. Канонический GitHub:
https://github.com/BorisDruzak/vk_bot, main. Проверенный remote main при переносе:
`d58069198aa69ff49558dae7bf9385bd66d45199`. Исходная папка без .git;
публикация v6.30 в GitHub не выполнялась.

## Размещение

- Windows VM `vk-bot@192.168.100.46`, проект `C:\Users\vk-bot\vk_bot`.
- HTTP панель **http://192.168.100.46:8787**, `--local-backend`, task
  `VKUnifiedWebPanel`; учётная запись admin, пароль задан пользователем отдельно.
- Единственный worker `VKUnifiedDailyWorker`, `.venv\Scripts\pythonw.exe`,
  `--scheduler-daemon --confirm`, InteractiveToken/Limited, logon и08:55 UTC+5.
- Python3.13.17, Chrome156.0.8078.48, Playwright1.64.1/Chromium156.0.8078.4.
  Установка из официальных подписанных дистрибутивов; pip check PASS.
- Часовой пояс VM исправлен с UTC+3 на Екатеринбург UTC+5.
- SSH key-only вход проверен обычной `ssh vk-bot@192.168.100.46`; пароль/закрытый
  ключ не хранятся в проекте. Known host проверяется.
- Прежние локальные задачи worker и панели Disabled. Временная локальная панель
 10.10.10.2 остановлена после проверки HTTP200 с VM.
- Firewall VM разрешает TCP8787 только10.10.10.0/24 и192.168.100.0/24.
  Исправлены: venv launcher отличается от реального pythonw; созданный Windows
  TCP deny нового pythonw перекрывал allow. Отключён только этот TCP deny;
  UDP/default inbound и остальные правила сохранены.

## Сохранность

Исходники/docs и SQLite snapshots сохранены вне проекта в отдельной защищённой
ACL резервной папке. Перед snapshot старый worker штатно получил stop_requested,
завершился сам, задача отключена. Не применялись kill/reset/retry.

- Три SQLite snapshots: integrity_check=ok на источнике и VM.
- Все49 таблиц аккаунтов логически совпали (количество и SHA256 строк).
- Истории попыток109/101, scheduler_jobs75, две точные постовые изоляции и
  сохранённые day plans совпали. scheduler_meta/diagnostics изменились только
  вследствие штатного старта/heartbeat новой VM. Новых попыток при переносе0.
- Полные профили скопированы с прямого разрешения, включая password stores:
  профиль1 —2902файла, профиль2 —2729. Исключались только Chromium Singleton
  locks. Исходные каталоги не заменены и не удалены.
- Headed нейтральный smoke Chrome и Chromium в пользовательской сессии2 PASS.
- Проверка точной header identity в перенесённых VK профилях: оба
  identity_detected=false/identity_matches=false. Это отсутствие подтверждения
  авторизации, а не доказанное несовпадение VK ID. Работоспособность сохранённых
  паролей на другом Windows-пользователе не доказана.
- Пользователь уведомлён о необходимости ручного входа. Оба аккаунта временно
  enabled=false; сохранённые VK ID/квоты/история прежние. Общая пауза не снималась,
  paused=null, active_uncertain=0, isolated_posts=2; два исхода остаются uncertain.

## Реализация

N профилей и БД, атомарный реестр и binding lock; новые аккаунты отключены и
с нулевой квотой. Политика максимум50/10/20, два community назначения поровну;
saved day plan неизменен, настройки на первый не спланированный день,
отключение проверяется сразу, перед attempt и final click. Сохранены09–19YEKT,
sleep20/API>=5с/5000за24ч/пачки<=4/интервал>=600с/no-catch-up.

API статистика nullable likes/comments/reposts/views, последние100, только
mode=ro кеш. Не выполняет дополнительных API-запросов и не меняет курсоры.
Просмотры старого кеша неизвестны до реального последующего обновления API.
Агрегаты групп NULL при неполном покрытии; отдельно известная часть и даты.
Источник полей — официальная схема VK:
https://github.com/VKCOM/vk-api-schema/blob/master/wall/objects.json.

Панель использует фиксированный RPC, scrypt hash и закрытый ACL, HttpOnly/
SameSite cookie, CSRF/Origin/Host, ограничение JSON и throttling входа.
Нет команд engagement/retry/reset/unpause. HTTP для LAN выбран пользователем.
Добавление через форму создаёт один профиль и автоматически запускает вход;
ошибка окна не создаёт второй аккаунт. Окно авторизации находится на VM,
RDP download не содержит credentials. Перед входом аккаунт отключается.

## Проверка

TDD для новых аккаунтов, статистики, RPC, HTTP/CSRF/Origin, nullable счётчиков,
immutable day, immediate disable, binding concurrency, own-account probe.
Ревью выявило и исправило чтение account2 ledger при probe account3 и concurrent
перезапись bindings. Runtime QA выявило heartbeat object и early HTTP TCP reset;
исправлены с регрессиями. Полный suite перед последним исправлением окна:
Windows source309PASS; VM309PASS, skipped2 (Node frontend checks, Node не нужен
runtime панели). Точные результаты окончательной проверки записываются ниже.

Playwright MCP: реальный VM URL, вход admin → обзор → аккаунты/диалог → публикации
100строк → группы57. Desktop1440x1000 и mobile390x844; document width390,
диалог358, Escape закрывает без сохранения, overflow страницы0, свежий цикл
console/page errors0/warnings0. Скриншоты вне Git: C:/Temp/vk-v630-vm-desktop.png,
C:/Temp/vk-v630-vm-mobile.png. Генерированный концепт и desktop просмотрены
через view_image: белые поверхности/светлый фон, синяя навигация, табличная
плотность, блок расписания, семантические цвета и иерархия сохранены. Отличия
намеренные: реальные данные, причины/attempt, отдельный heartbeat/phase/mode,
отсутствуют фиктивные успешные строки концепта.

Ежедневный существующий монитор перенаправлен на VM; его09:00 расписание и
тихий notification intent сохранены. Старую локальную копию он не запускает.

## Границы

### Исправление кнопки входа после реального обращения пользователя

При первом запуске панели на самой VM кнопка входа не смогла перезаписать
старую SSH-created задачу. В реальном Limited InteractiveToken session2
подтверждены access_denied/CimException, HRESULT0x80131500. DACL старой задачи
разрешала обычному токену только чтение; fresh task с теми же параметрами
создавалась успешно. Защиты/ACL не расширялись.

Исправление: определить собственную desktop session через ProcessIdToSessionId;
в интерактивной панели запускать отдельный pythonw/login worker напрямую с тем
же пользователем. Только SSH/service session0 использует прежний Task Scheduler
fallback. Добавлены RED→GREEN регрессии обоих путей. Установлен код, перезапущена
только панель; worker8808 продолжал работу, действий/изменений истории0.
Кнопки обоих аккаунтов проверены через реальный VM HTTP сайт: два отдельных
login worker в session2, оба awaiting_login/detected=false. Ошибки регистрации
нет; текущий вход VK всё ещё требует участия пользователя. После исправления
на VM16 целевых core tests PASS; полные окончательные результаты ниже.

Полный день реальных действий на VM, авторизация обоих VK аккаунтов,
расшифровка сохранённых паролей и восстановление после reboot/logoff не приняты.
Автологин Windows не настраивался; браузеру требуется пользовательская сессия.
Не выдавать Running/live за доказательство действий или входа VK.

Окончательная локальная проверка после исправления входа:311 tests PASS,
67.958с, exit0. Свежая runtime проверка: оба аккаунта awaiting_login/detected=false,
worker waiting_window, active_uncertain0, isolated_posts2, jobs75, posts100/groups57.
Окончательный полный прогон установленного кода VM:311tests, OK(skipped=2),
99.630с, TEST_EXIT=0. Оба пропуска — Node frontend fixture tests; runtime панели
Node не использует. Контрольные SHA256 шести основных модулей source/VM совпали.

Обновление00:35:49YEKT: account1 awaiting_login/detected=false; account2
ready/detected=true после ручного входа. Финальная сверка прежнего VK ID через
«Завершить вход» ещё не выполнена; оба аккаунта пока enabled=false.
