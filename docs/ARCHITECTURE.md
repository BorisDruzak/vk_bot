# Архитектура VK Unified (v6.34)

30октября: SourceDateUnavailable после initial+3 bounded date-reading
windows получает отдельную exact receipt scheduler_date_skips: без pause,
без повторной отправки, со spentquota/intent/uncertain и actor/day exclusion.
State.pause хранит тип через vk_scheduler_blocks, manual сохраняется,
automatic истекает на более позднем сохранённом дне с будущимsavedslotgate.
Legacy безmetadata manual, actor safety/unknown результаты не очищаются.
Контракт и результаты: SOURCE_DATE_BLOCKS_2026_10_30.md.

`accounts.future_policy(registry)` объединяет настроенную policy из
vk_automation.json с реестром и используется в configure_account/connect.
Явный end_hour допускает22; DEFAULT_CONFIG19 и сохранённые dayplans прежние.
State.refresh_policy выбирает неизменную текущую сохранённую policy, будущие
09–22/320/80 действуют с29октября. Snapshot передаёт work_window и
pending_work_window; панель различает даты и не фиксирует19 в причинах остановки.
Установлено28октября17:50:57;FOUR_ACCOUNTS_WINDOW_2026_10_28.md.

`vk_batch_engagement.refresh` повторяет одну wall.get-страницу максимум дважды
после доказанного сетевого VkApiReadDeadline/VkApiTransientRead. Каждая попытка
идёт через прежний VKReadClient/общий ledger/fresh_after; ожидание5/10сек
не отменяет guards и cutoff. При нехватке времени/бюджета после сбоя исходная
ошибка сохраняет технический backoff. Скрытая защитная цепочка запрещает повтор.
Событие public_read_retry и свежий counter_refresh heartbeat показывают2/3,3/3
и ожидание; после успеха маркеры очищаются. Действия и аналитика08/20 эту новую
политику не получают. Установлено28октября15:08:44YEKT; см.PUBLIC_READ_RETRY_2026_10_28.md.

`State.selection_context` объединяет публичные строки/ownverified и пакетные
exclusionна один pick или один проход _eligible. Контекст не хранится в State
и не переносится через действие; каждый новый выбор перечитывает историю.
Pool ограничивает уже свежие строки, не разрешает partial/staleнаблюдения.
`batch_*` причины различают cutoff следующего слота, рабочее окно, квоту,
stop, disable, смену дня и spacingdeadline; старый смешанный статус остаётся
неуточнённым. HB содержит spacing_ready_at/batch_deadline_at, отсчёт в панели
не оживляет staleсигнал. См.BATCH_PERFORMANCE_2026_10_27.md.

Правка после ревью26октября (установленаVM22:10YEKT): VkApiProtection14/17
идёт через прежний durable VkApiLimited latch; новые actorБД готовятся до
atomicregistrypublication. Каталог возвращает requests=client.used, кеш не
сбрасывает read-streak; safe OSreason согласован с прежним recovery допуском.
Worker.pulse_action обновляет heartbeat фактическими callbacks и пропускает
незавершённую транзакцию. UIразделяет stale и uncertain. Аналитика сохраняет
nullable observed_from по всем страницам, включаяпустые; общий conservative
measurementbound использует сохранённые start/finish/page/post времена,
староеemptyбезevidenceUNKNOWN. ТолькоП3можетбытьsignedfinite приимпорте.
История/frozen/paused/guards/лимиты прежние. См.REVIEW_FIXES_2026_10_26.md
и DEPLOY_REVIEW_FIXES_2026_10_26.md; текущая защита631не снята.

26октября19:28: только первый chooser wait30сек может дать отдельный
CommunityPreparationTimeout после freshidentity/source guards. Ошибка идёт
без обобщающегоStopRun в существующий RetryLedger через конкретный код;
допуск exacttype/stage/kind и отсутствующие intent/attempt. Policy0/legacy
defer не принимают этоттип. Общие защиты/последующие selectors не расширены,
старые interruptedjobs не мигрируют. См.CHOOSER_TIMEOUT_RETRY_2026_10_26.md.

26октября: bounded_transport сохраняет тотже20сек срок и stdout-контракт,
но HTTP-only helper передаёт ограниченный numeric/enum stderr-прогресс.
vk_read_trace наблюдает исходные DNS/TCP/TLS/send/header calls с восстановлением
вfinally; parent принимает только whitelist, пишет compact success/failure
послеHTTP и прикрепляет metadata к VkApiFailure. Внутренний2tupleрезультата
имеет атрибут telemetry, JSONVK/public/frozen данные не дополняются. Worker
передаёт callback через engagement.refresh в существующие scheduler_diagnostics.
Подробности:READ_NETWORK_DIAGNOSTICS_2026_10_26.md. Правила действий/времени
и отображение heartbeat этой правкой не менялись.

Установленная25октября поправка: независимые read
deadline(next-120) и intent window(next-60); before-click boundary(next-20).
State.fresh_time(_batch_clock) заново проверяет время послеDBчтений. Claim
проверяет planned..planned+120, owned claim может дождаться540сек доnext-60.
resume_not_before ограничивает последующее операторское возобновление строго
будущим savedslot. Communitychooser30сек/одинopener/duplicate-stop.
Описание: BATCH_WINDOWS_2026_10_25.md; установка/аудит: DEPLOY_RESUME_2026_10_25.md.

Deadline подготовки: реальный `Worker.refresh_batch` берёт границу из исходного
schedule_days (next slot либо конец окна), оставляет120сек и передаёт её в
engagement.refresh. Старый полный обход, обнаруживший new, возвращает mixed
pool без второго чтения. VKReadClient получает bounded_transport: отдельный
HTTP-only child без SQLite/профилей; stdin для ключа, общий20сек timeout,
kill/wait только child. Pacing/ledger/shared cache остаются в родительском
VKReadClient. Просроченная/частичная подготовка не даёт pool, обычный timeout
не маскирует technical failure. Worker учитывает общий attempted перед cutoff;
execute_job сохраняет pending перед немедленным повтором за границей.
См. RESERVE_DEADLINE_2026_10_24.md (уточняет прежний reserve fallback ниже).

`Worker.prepare_batch_pool` очищает прошлую выборку и применяет прежние
ошибки/guards ко всем refresh. Когда selected_band=new исчерпан, один
`refresh(reserve=True)` обновляет резерв с новым fresh_after и допускает
полные свежие наблюдения обоих слоёв. `pick` сохраняет возрастной приоритет,
квоты и учёт своих verified. Claim/попытки/sleep/click guards прежние.
`finish_batch(day=...)` закрывает именно исходный день при смене суток.
Partial reserve statuses отдельно отображаются в timeline/heartbeat.
См. RESERVE_FILL_2026_10_24.md.

## Встроенная очередь ЭЦП

vk_web.load_pos_reader загружает установленный POS ReadOnlyQueue из соседнего pos_automation. БД открывается mode=ro с одной read-транзакцией; каталог/схема не создаются. /pos/ и четыре ресурса имеют точный allowlist; /pos/api/session использует VK cookie, snapshot требует тот же session/CSRF. Только HTML /pos/ разрешён как SAMEORIGIN iframe; основной документ сохраняет DENY. POST-команд ЭЦП нет. Renderer и CSS остаются в POS-проекте, единый вход и очистку iframe при logout обеспечивает VK. Подробности: POS_QUEUE_TAB_2026_10_24.md.

23октября прямо согласованы60пачек≤4 для трёх полных квот50/10/20.
`vk_schedule_policy.MIN_BATCH_GAP_SECONDS=540` — общий минимум генерации,
claim и фактической резервации. Плотная генерация имеет bounded fallback с
уникальными balanced-gap парами; сохранённые dayplans не перезаписываются.
09–19YEKT/sleep20/API5сек/5000/no-catchup прежние.
См.THREE_ACCOUNT_SCHEDULE_2026_10_23.md.

`vk_public_data.PublicPages` — общий append-only store wall.get-страниц и
числовых наблюдений. `VKReadClient` использует его по runtime opt-in и общий
OS lease. Источник один, аналитические снимки остаются историческими,
api_seen_posts/api_post_engagement — совместимая проекция для вебки и очереди.
TTL300сек не заменяет строгий fresh_after перед пачкой. Все штатные читатели
используют страницы100; byid отключает кеш. Before-request проверки выполняются
после pacing до расхода ledger. См. SHARED_PUBLIC_DATA_V6_34.md.

`vk_action_retry.RetryLedger`: явная policy1, scheduler_action_recovery связывает
точную идентичность job с двумя scheduler_action_passes. Фазы и attempt ID
сохраняются отдельно; pending/held освобождают только своё точное uncertainty,
running допускает только текущего владельца процесса, после аварии interrupted.
Отдельный action_failure_streak увеличивается один раз за логический неуспех,
verified/already сбрасывает; третий ставит paused=action_failures_repeated.
Все настоящие резервации учитываются actor ledger и четырьмя местами пачки;
незаписанный второй intent также сохраняет расход после аварии.

`execute_action(recovery_attempt_id=...,allow_retry_click=...)`: свежая identity/
source/date проверка; like требует согласованного отрицательного JSON/footer и
can_like перед единственным новым reserve. Репосты после intent только читают
подтверждение. Типизированный ActionTransportError сохраняет непосредственную
Playwright cause, guard failures не становятся transport recovery.
Pending-источники входят в свежий API refresh независимо от ordinary eligibility,
но не меняют обычный возрастной приоритет. Известные canonical aliases одного
назначения исключаются до выбора; финальный precheck сохранён.
Подробности — RESUMABLE_ACTIONS_V6_33.md и VALIDATION_V6_33.md.

`vk_connections`: opt-in `account_flow=add_any`, архив старых UI-карточек,
автоматическое сопоставление свежего VK ID с неизменным actor. Registry
`profile_dir` связывает actor с подключённым физическим профилем; SQLite,
bindings и история принадлежат actor. Новый VK ID не наследует чужую историю.
Подключение проверяет исходный путь, in-flight, lease целевого профиля и
вместимость расписания до публикации. Незавершённые/архивные подключения
не участвуют в новых планах, сохранённый day plan неизменен.

`vk_activity.shared_activity` использует тот же байт OS lease, что exclusive
worker batch. На Windows это LockFileEx shared/nonblocking; разные login-окна
совместимы друг с другом, пачка worker с ними не пересекается. Физический
profile lock остаётся exclusive и разрешается по `profile_dir`, включая actor1.
`vk_control.login_worker` ждёт занятую пачку (`waiting_browser`), открывает окно
в интерактивном сеансе VM и автоматически сохраняет подтверждённый собственный
VK ID. После успеха закрывается только созданный контекст с сохранением кеша.
В режиме add_any ручной finish не нужен; exact actor guard перед действием
и fresh proof перед включением очереди сохранены. См. `ACCOUNTS_V6_32.md`.

`vk_authentication`: отдельное account/profile/machine/time proof точного header
ID, срок300сек; saved binding/legacy ready не авторизация. `vk_statistics_refresh`
→ общий `VKReadClient`/ledger → nullable observations; `vk_statistics_job` —
ограниченный фоновый проход, без cursor/actor изменений. `vk_statistics` —
readonly последние100 на группу, known_sum и coverage. `.vk-activity.guard`
согласует целую пачку worker с login/statistics до прежних global/profile locks.
Подтверждение и границы — `AUTH_STATS_V6_31.md`.

## Отдельная аналитика госпабликов

VM-панель: GET/api/analytics → vk_analytics_web.read_analytics → только
analyticsSQLite mode=ro → web/analytics.js, общиеsession/CSRF/origin guards.
Нет API-загрузок/actorwrites из UI. Недельные результаты вычисляются на момент
выбранного замера по полным календарным/anchored7day окнам; отсутствие покрытия
не становится невыполнением. UIпоказывает K/N и отдельный provisionalER.

`vk_analytics_cli` → `vk_analytics_import`/`vk_analytics_collect` → отдельный
`vk_analytics_store` (analytics SQLite) → `vk_analytics` (формулы, недели, сверка)
→ frozen JSON/HTML/CSV. LiveReader проверяет VM runtime binding, использует
прежний VKReadClient и общий ledger/guards, не browser и не actor history.
Никакой зависимости исполнения worker от новой аналитики не добавлено.
v1.1 замораживает analytics_catalog_captures/references из публичного каталога
через read-only SQLite. Исчезнувшие исторические ID дают отдельный NULL-снимок
и partial, конфликт дат тоже partial; прежние frozen отчёты не обновляются.
`vk_analytics_scheduled` — отдельный native Task Scheduler entry08/20YEKT:
фиксированный roster, deterministic slot/run_id, crash checkpoint, frozen
reports/rawCSV. Обычный рабочий период пропускается до API. Optional
LiveReader.before_request проверяет deadline послеpacing; прежние вызовы
без callback сохраняют поведение. Store.new_run принимает optional run_id
и повторно использует его только при совпадении frozen периода/состава.
Исторический кешпоследних100 не заменяет append-only snapshots. Подробнее:
[OMSU_ANALYTICS.md](OMSU_ANALYTICS.md).

## Исполнение и панель

Windows VM HTTP (`vk_web.py --local-backend`, `web/`) → фиксированный
`vk_control` RPC → read-only snapshots/управление реестром. Дополнительно доступен
режим отдельной панели → pinned-host SSH/key → `vk_control.py --rpc` на VM.
Playwright и единственный scheduler на VM рядом с SQLite и API key.
Панель не копирует базы, не читает cookies, не отправляет engagement-команды.

`vk_account_registry.json`: имена/enabled/желаемая политика;
`vk_account_bindings.json`: неизменные точные VK ID. Атомарная запись и отдельный
binding lock предотвращают потерю concurrent read-modify-write. Профили и БД
дополнительных аккаунтов имеют N, чужая история не переносится.
`State.refresh_policy` сохраняет старый day plan, обновляет новые пути;
enabled повторно проверяется перед попыткой/кликом. Расход остаётся в истории
отключённых аккаунтов. Вместимость расписания учитывает заданные квоты.

Вход требует disabled и отсутствия in-flight job; profile lock удерживается до
завершения. Панель в InteractiveToken-сеансе запускает отдельный pythonw/browser
напрямую тем же пользователем; SSH session0 использует Task Scheduler fallback.
Это избегает перезаписи административной задачи из Limited-токена панели. Точный
header VK ID в старом режиме должен совпасть с привязкой; в add_any он выбирает
неизменного actor автоматически. Закрывается только созданный контекст.

`api_post_engagement.view_count` — additive nullable, только реальное views.count.
`vk_statistics` читает mode=ro без API/миграций/смены курсоров. Агрегат группы NULL
при неполном покрытии; известная часть и границы времени указаны отдельно.

Ниже сохранена архитектурная история предыдущих версий.

## Общий бюджет API — v6.29.8

По прямому запросу локальный scan.daily_requests=5000 за скользящие24ч. Предпачечный refresh и фоновый каталог используют общий прежний api_read_attempts; новая настройка не обнуляет расход. Scheduler и каталог принимают максимум5000, согласованный с VKReadClient. Официальные ограничения VK/429/защиты и лимиты действий не отменяются. См. [API_BUDGET_V6_29_8.md](API_BUDGET_V6_29_8.md).

## Техническое восстановление — v6.29.7

API-чтение раз в5сек по прямому разрешению. Refresh прекращает лишнюю пагинацию только по доказанной возрастной границе. `vk_scheduler_recovery` требует точного доказательства отсутствия отправки; failedjob остаётся failed, batchpool освобождается, read/actionstreak разделены, backoff120/240сек и третий сбой блокирует. Intent/uncertain/guards/429/contextclose не отменяются. Две точные пользовательские изоляции47/75 сохраняют uncertain и бюджет. См. [THROUGHPUT_RECOVERY_V6_29_7.md](THROUGHPUT_RECOVERY_V6_29_7.md).

## Диагностические события — v6.29.6

`vk_diagnostics.py` задаёт allowlist событий/этапов/чисел и цепочки ошибок без сообщения/DOM/секретов. `vk_scheduled_action` испускает этапы и read-only сетевые метаданные; `State.log_event` выводит JSON и отдельно сохраняет scheduler_diagnostics/last_diagnostic_error. Ошибки telemetry не меняют основной исход, чужие транзакции не коммитятся. Финальный deadline проверяется после записи telemetry. См. [DIAGNOSTICS_V6_29_6.md](DIAGNOSTICS_V6_29_6.md).

## Свежий приоритет перед пачкой — v6.29

Перед каждой рабочей пачкой `vk_batch_engagement.refresh` обновляет через `wall.get` счётчики всех известных подходящих постов старшего доступного слоя: сначала сегодняшний день YEKT, затем резерв предыдущих семи дней. В пачку попадают только точные публикации, действительно возвращённые этим обновлением с тремя известными счётчиками. Приоритет — минимальная сумма **лайков + репостов + комментариев**; неизвестное значение не ноль. При равенстве сохраняются баланс лимитов/аккаунтов и дата публикации. Комментарии только считаются, бот их не пишет.

Перед каждым выбором внутри пачки перечитываются подтверждённые собственные действия обоих аккаунтов. Эффективный счётчик равен `max(API + наши verified после наблюдения, число наших различных verified)`: прежние действия не прибавляются повторно поверх API, а задержка API не уменьшает подтверждённый минимум. `uncertain` не становится подтверждённой активностью, но продолжает расходовать квоту и останавливать worker. Истории и запреты повторных отправок сохранены.

Чтение разделяет прежний бюджет API 2000 запросов/24 ч, паузу минимум 20 с и предел 10 страниц на группу. Курсоры фонового каталога сохраняются; code 7 откладывает только обновление счётчиков недоступной группы на час. При остановке, исчерпанном бюджете или отсутствии свежих полных данных действия по старому кешу не запускаются. Новые посты в прочитанных группах включаются, но минимум гарантируется только среди доступных обновлённых кандидатов, а не всех неизвестных публикаций VK. Долгое чтение может привести к пропуску плановых пачек; пропуски не догоняются.

Окно 09–19, дневные максимумы 50/10/20 на аккаунт, разделение community 10+10, пачка до четырёх, один action за раз и `sleep(20)` после попытки не меняются. Дополнительно сохранён `last_action_batch`: десять минут между началами фактических попыток разных пачек проверяются после обновления и при резервации, включая перезапуск. Диагностика: `refreshing_counters`, `last_counter_refresh`; обоснование выбора — отдельная таблица `scheduler_priority_evidence` с наблюдёнными и эффективными числами.

Проверка и границы приёмки — [VALIDATION_V6_29.md](VALIDATION_V6_29.md). Текущая v6.29 локальная; публикация в GitHub в этом изменении не выполнялась.

## Историческая основа v6.26

Описана реализация v6.26, прошедшая локальный реальный тест второго аккаунта 30/30. После отдельной публикации документации (`ee47478bce9341af97cd846d482bc2363df5c89b`) публикуется тот же исполняемый код, тесты и профильный CI. Контрольные суммы и результаты сверки — `VALIDATION_V6_26.md`.

## Поток чтения

`VK_API_TOKEN` (локальная переменная окружения) → `vk_api_read.py` (`wall.get`, `owner_id`, `id`, `date`, `offset`) → `api_seen_posts` и `api_group_scan_progress` в SQLite → отбор публикаций `lookback_days=7` → CLI и CSV отчёты.

Каждая публикация идентифицируется ключом `(group_id, post_id)`; URL может быть восстановлен из пары `owner_id=-group_id`, `post_id`: `https://vk.com/wall-<group_id>_<post_id>`. Пример: `wall-188206921_13413`. Не нужно читать DOM стены для получения прямой ссылки.

Источники дат не эквивалентны:
- `vk-post-json` и `wall.get.date` — точное время конкретного поста, если совпадают IDs;
- `post_date_block_preview` вроде `1 ч назад` и оценка `feed-approx` — приблизительная дата;
- tooltip `20 окт в 20:284 просмотра` может содержать склеенный текст даты и счётчика просмотров; при наличии JSON использовать серверную дату.

Действия допускаются только с совпадением точных ID/дат полученных через VK API; записи, найденные только браузерным сканированием, не становятся автоматически API-подтверждёнными.

## Поток Playwright (аккаунт №1)

Очередь → открытие конкретного URL в `.vk-local-profile` → проверка точного корневого `[data-testid="post"][data-post-id]` → проверка состояния (`data-user-likes`, сведения VK) → нужное действие → повторная проверка VK → запись исхода `already`/`verified`/`pending`/`uncertain` в SQLite.

Нельзя путать кнопку основной публикации с `data-testid="comment-like"` комментария или вложенного репоста `data-post-nesting-lvl=1`. Футер иногда вне viewport и содержит `aria-hidden="true"`, что **само по себе не означает скрытый пост**. Перед кликом используется прокрутка к точно выбранной кнопке.

Диалог репоста: ожидать загрузки UI, проверять назначение «на личную стену», не отправлять при неоднозначности. Сбой до нажатия финальной отправки не должен превращать `pending` в `uncertain`; после попытки отправки при непроверенном ответе остаётся `uncertain`, повтор запрещён до ручной диагностики.

## Аккаунты и границы данных

| Объект | Аккаунт №1 | Аккаунт №2 |
| --- | --- | --- |
| Chromium | `.vk-local-profile` | `.vk-local-profile-2` |
| Привязка ID | локальный `vk_account_bindings.json` | тот же реестр, отдельная запись |
| Вход и проверка | работают | работают, проверены на живом VK |
| История действий | `vk_batch.sqlite3` | `vk_batch_account_2.sqlite3`, независимая история и новый числовой CLI |
| Действия | прежний движок, реальный тест 30/30 пройден | Разовый `--test-post` и отдельный `--run-actions`: реальный тест 30/30 пройден 2026-10-21 |

`vk_account_smoke.py` читает подтверждённые `(group_id, post_id, published_at)` из основной SQLite **в режиме read-only**, а статусы и попытки №2 — только из другой SQLite. Новая база №2 при первом создании закрепляется за VK ID. Старую непомеченную базу с уже существующими постами, аудитами или действиями невозможно привязать к №2 (защита от случайной копии БД №1). Перед каждым кликом ID проверяется по заголовку VK в отдельном Chromium-профиле. Начиная с v6.26, дополнительный `vk_account_batch.py` читает каталог публикаций №1 **строго read-only** и переносит в локальный кеш базы №2 только `group_id`, `post_id`, `published_at`, `seen_at` для валидных точных API-записей. Ни реакции, ни истории попыток, ни предупреждения №1 не переносятся. Старые `verified/uncertain` второго профиля не меняются при обновлении каталога. `--account 2 --preview` и `--account 2 --run-actions` используют очередь **только его собственной базы**. Перед открытием действий проверяется числовой VK ID №2, а перед каждым финальным кликом — повторно. Оба профиля последовательно используют общий lock-файл и собственные locks, чтобы не запускать две операции одновременно. Нет миграции всех состояний в общую таблицу `(vk_user_id,group_id,post_id)` — Issue #1 остается открытым для общего решения.

Новая привязка не должна принимать VK ID, уже привязанный к другому номеру профиля, или изменение ранее закреплённого VK ID без явного восстановления. VK ID читается из авторизованной шапки сайта, не из постов или комментариев. Cookies/пароли не извлекать.

**Нельзя** просто использовать общую таблицу `posts.like_status` для разных аккаунтов. API-каталог публикаций может быть общий; состояния реакций, попытки, блокировки, статусы сеансов и отчёты должны быть разделены по числовому VK ID. Разовый smoke и многопостовый режим №2 физически изолированы отдельным файлом SQLite. Общая миграция в account-keyed таблицы ещё не сделана; успешный тест отдельной базы не закрывает эту архитектурную задачу.

## Подтверждённая приёмка аккаунта №2

2026-10-21 команда `python vk_unified.py --account 2 --run-actions --likes 30 --reposts 30 --max-posts 60 --confirm` завершилась с кодом 0 и результатом `complete`. Проверено 35 публикаций, записано ровно 30 попыток каждого действия и подтверждено VK по 30 новых лайков/репостов. Результат независимо сверен с `--account 2 --action-status`, журналами SQLite и CSV на 35 строк. Ранее существовавшие реакции пропущены; история попыток №1 не изменилась (71 запись до и после). Сохранённых предупреждений VK и оставшихся run-lock файлов на момент завершения не было. Полный контекст и временные отметки — в `DEVELOPMENT_CONTEXT.md`.

## Контроль ошибок/безопасности

- HTTP 429, текст `Too many requests per second`, ошибки API частоты запросов, CAPTCHA или предупреждение аккаунта → прекращение чтения и действий, сохранение общей блокировки;
- `--ack-warning` только после ручной проверки VK, не как автоматическое продолжение;
- временно отсутствующий контейнер поста при верном URL → одно безопасное повторное открытие для аудита, далее `unresolved`;
- `hidden`/`deleted` не означают `unresolved`; не считать ошибку загрузки постом удалённым;
- неверное целевое действие или непроверенный результат → не повторять клик вслепую;
- лимиты `--likes`/`--reposts` числового запуска — числа попыток, а не гарантия отсутствия платформенных ограничений;
- стандартные ограничения JSON и суточный учёт истории сохранены для старых режимов; в числовом режиме v6.23 внутренние JSON-квоты не применяются, **но** блокировки VK не обходятся;
- локальный запуск должен быть эксклюзивным (lock), чтобы два процесса не работали одновременно с одной сессией.

## Хранение

`vk_batch.sqlite3` (№1) и `vk_batch_account_2.sqlite3` (№2) — рабочие SQLite, развивается последовательными миграциями по требованию. Примеры таблиц: `posts`, `action_attempts`, `api_seen_posts`, `api_group_scan_progress`, `group_scan_progress`, `vk_health_events`, `like_experiments`, кампании и журналы числовых заданий. **Схему проверять в текущем коде**; перечень не заменяет миграционный файл.

CSV-отчёты (`vk_actions_results.csv`, `vk_actions_results_account_2.csv`, `vk_pairs_results.csv`) формируются автоматически и должны содержать ссылки, даты, статусы и подтверждения. Отчёты не используются как входной CSV для лайков в актуальном режиме.

## Репозиторий и поставка

Канонический репозиторий: [BorisDruzak/vk_bot](https://github.com/BorisDruzak/vk_bot), основная ветка `main`; правила — в `../AGENTS.md`. Перед публикацией сверять актуальный SHA с локальными файлами. Не смешивать отдельные старые zip-версии: v6.24 накладывается на код v6.23 и содержит `vk_batch.py` с исправлением репост-диалога v6.22. `requirements.txt` содержит Playwright. Автоматические тесты офлайн; реальные проверки выполняются отдельно, действующие VK-сессии в CI использовать нельзя.

## Явное назначение сообщества (локальная v6.27)

`vk_unified.py` направляет `--repost-community ID` в `vk_community.py` до прежних обработчиков аккаунтов. Без флага прежний путь не меняется. `vk_community_ui.py` обслуживает выбор и доказательство назначения; `--dry-run` вызывает только подготовку.

В каждом аккаунтном SQLite добавляются `community_account_identity`, `community_reposts` с ключом `(community_id,group_id,post_id)`, `community_runs`, `community_run_posts`, `community_run_attempts`. Фактические попытки попадают в общий аккаунтный `action_attempts`; в личный `command_action_attempts` они не добавляются. Лайки используют существующий аккаунтный `posts.like_status`, а репосты в сообщество — отдельное состояние. CSV также имеет account/community в имени.

Источник очереди — текущий API-каталог базы №1, прочитанный read-only; во вторую базу переносятся только публичные ID/даты. Диалог выбирается по точному ID опции, проверяются её value/label, единственное назначение club и итоговое значение combobox. Перед единственным финальным кликом требуется одноразовое доказательство подготовки и сохранённая попытка `uncertain`. Успех проверяется по точному владельцу целевой стены, вложенному исходному посту и свежему timestamp.

Просмотр целевой стены ограничен отрисованными постами; отсутствие совпадения не доказывает отсутствие старых дублей. 2026-10-21 проверены четыре реальные публикации (по две с аккаунта), все подтверждены. После каждой попытки в коде выполняется `time.sleep(20)` до чтения результата и следующего репоста, в том числе при ошибке после резервации. Команды и подробные ограничения — `COMMUNITY_REPOST_CLI.md`.

## Ежедневный worker v6.28

`vk_catalog_scan.py` выполняет полный поочерёдный каталог с durable cursors; `vk_api_read.py` хранит публичные counts отдельно в api_post_engagement. `vk_schedule_policy.py` содержит календарь/квоты/случайные slots/приоритет. `vk_scheduler_state.py` хранит day plans, batches и предварительные intents; usage импортируется из реальных аккаунтных action_attempts с community links. `vk_scheduled_action.py` выполняет ровно одну выбранную операцию через прежний UI и callbacks ledger/deadline. `vk_scheduler_probe.py` проверяет те же элементы read-only с жёсткими блокировками финальных кликов.

`vk_scheduler.py` связывает каталог/очередь/Windows worker. В текущем dry-run ветка возвращается до claim_batch/исполнения и может только читатьAPI/показывать очередь. Ночью ждёт. Task Scheduler стартует pythonw в интерактивной сессии; OS-held singleton освобождается при падении. Неопределённые intents после сбоя блокируют новые отправки. Детали — AUTOMATION_V6_28.md.

## Уточнение v6.28: подтверждение community после отправки

`vk_scheduled_action` после единственной сохранённой попытки и обязательного sleep20 вызывает `vk_community_ui.confirm_destination`. Одна навигация, затем ограниченные9сек DOM-чтения без reload/scroll/submit; точные owner/source/date обязательны. Таймаут и неоднозначность сохраняют uncertain. Фиксированные безопасные коды в scheduler обозначают этап сбоя; сырые сообщения/DOM не пишутся. Вручную подтверждённый по запросу пользователя старый uncertain согласуется с ledger без новой попытки, с локальным аудитом и сохранением расхода квоты. Такой разбор не является автоматическим снятием будущих ограничений.
# Диагностика времени — issue #4 (29 октября 2026)

`vk_scheduler_timing` вычисляет фиксированные effective windows и read-only
отчёт по сохранённым слотам/диагностике. Worker наблюдает claim/refresh/spacing
и durable reservations; batch_timing сохраняется после завершения пачки.
`vk_action_timing` буферизует monotonic spans семи этапов до cleanup;
`vk_diagnostics` пропускает только типизированные события и поля.
`vk_control.snapshot` отдаёт scheduler.timing в раскрываемый отчёт web/app.js.
`vk_schedule_simulation` использует реальные Worker/State/RetryLedger в
временных базах с FakeClock и синтетическими внешними адаптерами.
Диагностика не предоставляет разрешения на действие и не меняет guards/plans.
См. SCHEDULER_TIMING_DIAGNOSTICS_2026_10_29.md.
