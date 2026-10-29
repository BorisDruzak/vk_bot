# ПОЛНЫЙ КОНТЕКСТ РАЗРАБОТКИ — VK Unified / VK Bot

29октября15:54:23YEKT: после прямого разрешения и свежего15:52exactactor6/date
proof снята только source_date_unavailable; сохранённый futuregate15:57:59,
job1152failed/attemptNULL/pass1intent0interrupted неизменён. Worker5340не
перезапускался. Первая естественная пачкаslot43:15:58:02–16:04:35,4попытки/
4verified (3community+1like, jobs1153–1156),54spans/все7типов, cooldown20001мс,
24freshAPIreads,unknown0,pausedNULL/stopFalse/active_uncertain0. Read-onlyproof:
старыеjobs/passes≤1152,80slots,изоляции ипрежниеattempts6actorDB сохранены,
catchup28–42нет. См.DEPLOY_TIMING_DIAGNOSTICS_2026_10_29.md;локальноеevidence
outputs/issue4-vm-deploy-2026-10-29/live-acceptance.json.

29октября14:59:30YEKT: issue#4 установлен из bf37c10 после зелёногоCI и
985VM/OK/20Node-skip/442.353с в source-only копии.43файла, existingVM
vk_web.py POS-overlay сохранён. Backup10DB/integrityok+25JSON;144table/schema
swapchecks, послеrestart10DB/133таблицы/все схемы и80savedslots сохранены.
Worker5340/live/attention,stopFalse; прежняя13:29пауза source_date_unavailable
сохранена. Read-onlyreport80slots/HTTPassetsSHA PASS;15:01 exactactor6/date
proofPASS и реальные читающиеspans1.109/4.739с, действий0/unpause0.
Новые actionspans ждут отдельного разрешения на возобновление.
История106reservations/105verified; старый job-basedreport105 — приближение
на retry. Расчётбеззадержекне гарантирован: уже был carryover8.753с;
slot3окно197.591с,4communitymedian-сценарий~265.5с до четвёртогоintent.
Подробности: DEPLOY_TIMING_DIAGNOSTICS_2026_10_29.md.

29 октября: issue#4 — подготовлена офлайн диагностика effective_window,
буферизованные7stage spans с median/p95, read-only80-slot отчёт панели и
FakeClock-симулятор на реальном Worker/State.09:29 воспроизведён3/4; default
80-slot модель310попыток,70полных/10неполных. Это модель, не живое выполнение.
Baseline cb8ec8f; установки на VM нет, worker и сохранённые планы не менялись.
Состав/команды/ограничения: SCHEDULER_TIMING_DIAGNOSTICS_2026_10_29.md.
Финальный clean-clone discovery985/OK/0skip/265.334с, независимое ревью без
блокирующих замечаний, desktop/mobile PlaywrightMCP80-slot UI PASS.
Реализация: c1df439db68d88422b2424597458f89648b8ea93.

29 октября: текущие исходники v6.34, панель, аналитика и окно09–22 для четырёх
аккаунтов опубликованы в main;944офлайн-тестаPASS. Основной коммит8f608c0.
Состав и границы: PUBLICATION_MAIN_2026_10_29.md.

28октября17:57:03YEKT: по прямому «проверь и включи» actor6«Богдан»
проверен штатным check_only в интерактивном сеансеvk-bot: exactheaderID matched,
freshproof, затем configure_account(enabled=True). Другие акторы и квоты50/10/20
не изменены. Saveddayplans совпали с backup,worker5844/waiting_batch,pausedNULL,
stopFalse. Первый день действийactor6 —29октября; ручных лайков/репостов0.
Одноразовая проверочная задача завершена0 и удалена. Evidence:
outputs/four-account-window-2026-10-28/enable-bogdan/result.json.

## Четыре равные квоты и09–22 —28октября

По прямому выбору расширено время вместо уменьшения квот: каждый из4аккаунтов
50/10/20, futurecapacity320/80пачек. Установлено17:50:57YEKT; действует
с29октября. Старый dayplan28октября09–19/60slots не изменён. Actor6 успешно
подключён17:13:40, policy сохранена с двумя назначениями пользователя,
enabledFalse. Аккаунты1/2/5 и их квоты/назначения сохранены.
Сохранение/подключение проверяют accounts.future_policy(vk_automation.json);
DEFAULT_CONFIG19 остаётся историческим, явно разрешён end_hour<=22.
Панель показывает work_window/pending_work_window и понятное ожидание пачки.
728local/728VM(skip19Node),9новыхрегрессий, UI desktop/mobile, reviewPASS.
Установщик исправлен после двух безопасных отказов: stop только при quiescence
под activitylease с durableintent;7Python+4PowerShell guards local/VM PASS.
Backup10DB/integrityok+25JSON;132table/schema swapchecks,121table proofafterrestart,
61fileSHA+1unreadableuntouched,3TaskXML/enabled прежние. Worker5844/live/waiting_batch,
pausedNULL/uncertain0,gate18:00:29.1023прежнихjobs/631/actorhistory/план сохранены.
API5000/24ч и все guards/паузы прежние;320квота не гарантирует фактический объём.
Документ:FOUR_ACCOUNTS_WINDOW_2026_10_28.md. GitHub не публиковался.

## Повтор сетевого чтения — 28 октября2026

После четырёх20секTCPтайм-аутов13:02–13:41(без actionjobs/attempts, не остановка
процесса) пользователь прямо разрешил несколько подключений. Установлено
15:08:44YEKT: до3попыток одной страницы счётчиков, ожидание5/10сек, прежний
VKReadClient/ledger/pacing/cutoff/guards, без resend действий и без catchup.
Сетевые phases/type/context проверяются консервативно; при отсутствии запаса
сохраняется исходный technical failure. Панель показывает retry2/3,3/3 и ожидание.
719local/OK,719VM/OK(skip18Node),28новыхтестов, независимое ревью;5файлов.
Backup9DB/integrityok,23JSON,3TaskXML; схемы, история631, план и изоляции прежние.
Worker3152/live/waiting_batch,pausedNULL,gate15:10:51;115таблиц и62доступных
savedfileSHA совпали послеrestart, секрет панели недоступен и не менялся.
См.INCIDENT_2026_10_28.md и PUBLIC_READ_RETRY_2026_10_28.md. На момент установки
живой повтор сети ещё не наблюдался; регрессии используют fake transport.
Первая пачкаslot37(15:10:51–15:18:23)дала4попытки/4verified,cooldown>=20,
spacing549.264>=540; старыеjobs/actorattempts/план/изоляции сохранены.
Сетевых ошибок в ней не было; worker3152/live/monitoring,pausedNULL.

## Производительность пачек — 27 октября 2026

Старая пауза631 снята по отдельному прямому разрешению в09:57YEKT;631остаётся
failed/interrupted безattempt, старые пачки не переоткрывались. В10:59:37YEKT
установлена оптимизация SQLite: один свежий selection_context на pick/_eligible,
bulkисключения, без кеша между действиями. Точный порядок/evidence сохранён.
Локально691/OK, VM691/OK(skip17Node), независимое ревью. Backup9DB/integrityok:
C:/Users/vk-bot/vk_backups/batch-performance-20261027T054000Z. Worker9712/live/
waiting_batch, pausedNULL, gate11:10:11YEKT.115таблиц послеrestart,59доступных
fileSHA и3TaskXML совпали; secretпанели не читался/не менялся.15файлов заменены,
схемы/лимиты/история/план не менялись. См.BATCH_PERFORMANCE_2026_10_27.md и
DEPLOY_BATCH_PERFORMANCE_2026_10_27.md. Первая штатная пачкаslot13:11:10:13–11:14:17,4новые попытки/4verified,
все3аккаунта; ещё1alreadyбезattempt. VMвыбор0.246–0.354сек вместо12.2–12.5,
cooldown>=20сек,actualspacing957.572>=540,631/план/стараяистория сохранены.

## Установлены семь исправлений ревью 26 октября22:10YEKT

По прямому «разворачивай на вм»: runtimebaselineVMсовпал, source-only677tests
OK/217.062сек(skip14Node), Python3.13.17/Playwright1.64.1.23файла установлены,
observed_fromдобавлен безbackfill. Backup9SQLite/integrityok и исходники:
C:/Users/vk-bot/vk_backups/review-fixes-20261026T165653Z.
109таблиц/52читаемыхfileSHA/прочиеruntime29 неизменны; секретпанели
maintenance-readerнедоступен и не читался/не менялся. TaskXML3совпали.
Новыйworker8556/live/attention, pausedaction_stopped_community_prepare,
631failed/noattempt/interrupted сохранены, unpause0/реальныхVKзапросов0/actions0.
ПанельRunning/HTTP200; analyticsReady08/20, readonly64groups/partial.
Report:DEPLOY_REVIEW_FIXES_2026_10_26.md; evidence outputs/review-fixes-vm-2026-10-26.

## Семь дефектов исправлены локально 26 октября — без установки на VM

R1–R7 из CODE_REVIEW_2026_10_26 закрыты кодом и23новыми регрессиями:
API14/17durableguard; savedanalyticscoverage, включая empty/outofperiodcache;
actorSQLitebeforepublish; canonicalOSErrorreason; HTTP-onlyreadstreakreset;
eventheartbeat безintentcommit и отдельныйstaleUI; signedfiniteП3import.
677tests/127.378сек/OK, compileall, синтетический Playwright desktop/mobile,
независимое ревью безсущественных открытых замечаний. Добавлена только nullable
analytics_run_groups.observed_from, без backfill/frozen/historyrewrite; старые
пустые замеры безevidence остаютсяUNKNOWN. Source23файла перенесены сSHAпроверкой;
backup/diff/logs в outputs/review-fixes-2026-10-26. RuntimeVM/paused631/история
не менялись, unpause/реальныхдействий0. Подробности:REVIEW_FIXES_2026_10_26.md.

## Ревью 26 октября — без изменения runtime и VM

Подтверждены семь дефектов: API CAPTCHA14/validation17 не ставят общий lock;
старый незавершённый analytics run становится полным по текущим часам;
добавление disabled actor до созданияSQLite и подклассOSError могут вызвать
лишнюю глобальную паузу; кеш сбрасывает read-streak безHTTP; длинная исправная
пачка отображается uncertain; импорт отвергает допустимый отрицательныйП3.
Полный изолированный local unittest654/OK за137.960сек, отдельные синтетические
воспроизведения всех7 перепроверены. Исправлений/установки/stop/unpause не было;
GitHub и VM в этом ревью не сверялись. Приоритеты, границы, stop-матрица и
артефакты: [CODE_REVIEW_2026_10_26.md](CODE_REVIEW_2026_10_26.md).

## Установлен typed chooser timeout26октября19:28YEKT

По прямому «делай» начальный30сек chooser timeout выделен в
CommunityPreparationTimeout/кодcommunity_prepare_timeout, с повторной
identity/source проверкой. Только точныйтип/community/preintent/noattempt
и policy1 допускают оставшийся один повтор; pass.intent тоже проверяется.
ОбычныеStopRun/cleanup/защиты остаются немедленными; policy0 не получает
legacytechnical recovery. Три неуспешных logicalaction ставятattention,
успех сбрасываетstreak; cutoff оставляетpending прежнегоsame-dayправила.
Local654OK/VM654OK(skip13Node), независимое ревью безблокеров; установлены4
модуля,194protectedсверки неизменны,TaskXMLпрежний. Новыйworker5444/live/
attention: pausedaction_stopped_community_prepare/631failedintent0interrupted
сохранены, unpause/миграцииистории не было. Syntheticinstalledclassification
PASS безVK/actorwrites. Отчёт:CHOOSER_TIMEOUT_RETRY_2026_10_26.md.

## Проверка631 и повторы26октября17:21YEKT

Прямое «сделай проверку»: guardedreadonlyUI631/account5 PASS, option1441ms,
точныеsource/date/account/destination и finalbutton подтверждены;blocked_clicks0,
198protectedсверок неизменны. Основнойworker/paused не менялись, временная
интерактивная задача удалена. Policy1 включена, но community_prepare и её
обычный innerStopRun не retryable; interrupt+pause идёт до счётчика3,streak0.
Правило23октября —1повтор одного действия плюс3подряд неуспешных действий,
а не3отправки одного репоста. ПробелtypedchooserTimeout описан в
INCIDENT_2026_10_26.md; policy/paused/история не изменены.

## Установлена диагностика чтения26октября13:51YEKT

По прямому запросу добавлены безопасные public_read_finished/failed: фазы
helper/imports/DNS/TCP/TLS/send/headers/body/JSON,read_id,ms,errno/winerror/
TLSverify/HTTP/VKкоды. Частичный stderr при20сек timeout даёт последний этап;
metadata сохраняется и после ошибокVK5/7/6. URI/body/тексты исключений/ключи
не логируются. Callback только послеHTTP; исходные network/TLS/proxy вызовы,
20сек deadline,pacing/ledger/guards/retry не менялись. Изменены4модуля и новый
vk_read_trace.py. Local639OK; VM639OK(skip13Node), независимое ревью закрыто.
Backup и193protectedсверки:READ_NETWORK_DIAGNOSTICS_2026_10_26.md/rollout.json.
TaskXML неизменен, worker5704/live/attention; новая самостоятельная пауза
action_stopped_community_prepare с13:36:58 сохранена, unpause не выполняли.
Установленный DNSproof11001 синтетический, безVK/actorwrites. Недостаток
времени и heartbeat/UI только обсуждались, их алгоритмы не менялись.

## Диагностика26октября12:08YEKT — без изменения worker

Дополнительная проверка по «Установи причины сбоя»13:08:95verified/95attempts,
23полных из25пачек;6284/live/monitoring/pausednull, каталог64heads/62complete/
2partial/0error. Ошибок после09:31:12нет. Подтверждены wrapper20сек и отдельный
дефект heartbeat/timeline180сек; Node-воспроизведение232сек→uncertain,
fresh→running,finished→verified. Точная причина исторической задержкиHTTP
не установлена; одинаковые внутренний/внешний20сек ограничивают диагностику.
Шестьruntime/configSHAсовпали. Только чтение, без установки/stop/unpause.
Дополнение и evidence —INCIDENT_2026_10_26.md.

По «опять сбой, проверь»: сейчас6284/live/monitoring/pausednull, locks0,
71verified/71реальнаяпопытка. 09:30slot3:20сек transport helper timeout,
безjobs/attempts, backoff завершён09:33, с09:40пачки снова полные.
Оранжевая12:00 на скриншоте12:05:36 — stale heartbeat>180сек во время
живых действий; slot18/jobs592–595 завершились4verified в12:06:30.
Дефект timelineState воспроизведён офлайн, код/защиты не менялись.
Отдельно09:00job526 безattempt: поздняя подготовка пересекла intentcutoff,
пачка3verified. Подробности/границы: INCIDENT_2026_10_26.md (только локально).

## Согласование64источников —25октября

Пользователь после сверки57/64 прямо сказал «делай исправляй». В vk_groups.json
добавлены семь конкретных frozenID; прежние57объектов/остальные JSON-настройки
не изменены. Будущая аналитика автоматически не разрешает новые источники.
ERстраницы не становится критерием; приоритет постов/лимиты/guards прежние.
Локально140testsOK и офлайн loader/Worker/candidates9категорий/catalog64PASS;
VM source-only140OK+тотже smoke64PASS,13runtimehashes совпали. Независимое
ревью без важных замечаний. Установлено19:01:55YEKT после штатного завершения
slot59/окна; backup source-groups64-20261025T135553Z,196protectedпроверок
совпали. Прежняя задача/XML/учётная запись сохранены. В19:02:32 worker6284
live/waiting_window/pausednull, next26октября09:00; новых действий при установке0.
Живые результаты семи новых групп ещё не приняты. Подробности:
SOURCE_GROUPS_64_2026_10_25.md. Старое57ниже — исторические результаты.

## Установлено25октября после повторного разрешения доступа

Пользователь вновь разрешил VM:620локальных тестовOK, VM620/OK(skip13Node).
Установлены4runtimeмодуля15:47:56YEKT,67protectedhashes неизменны; backup
batch-window-20261025T104756Z. Свежий guarded450proof15:49:23 с blocked_clicks0,
история неизменна. Снята только action_stopped_community_prepare15:51:25,
job450failed/pass1intent0/interrupted сохранён. Gate на savedslot16:00;
пропущенные пачки не догоняются. Worker6964 live/waiting_batch.
Дополнительно QEMU-GA Running/Auto безreboot, ProxmoxVM125ping/osinfo/exec/IP
проверены. Независимый key-only SSH alias vk-bot-vm, техник vk-maint;
политика Windows lockout10/10/10 сохранена. Причина4740:10неудачных локальных
Windows-входов chrome.exe, не SSH; внутренняя причина Chromium UNKNOWN.
Подробности и приёмка штатной пачки: DEPLOY_RESUME_2026_10_25.md.
Приёмка16:06YEKT:slot42/batch_complete16:00:03–16:05:30, jobs451–454
4verified/4attempts, все3аккаунта; cooldown>=20.006сек/APIgap>=6сек,
rolling24h1819/5000. Все прежниеjobs/batches/plans и actorattempt-prefix
неизменны; пропущенные19–41 не догонялись. pausednull/uncertain0/locks0,
последняя ошибка историческая450. Следующий savedslot16:10.

## Локальное исправление25октября — доступ кVM приостановлен пользователем

Прямой запрос: исправить early cutoff/spacing/UIprepare, проверить/установить
и безопасно снять только связанную паузу450. Затем пользователь сказал
«пока не подключайся к вм»: после этого удалённых операций нет, установка/
unpause/resume ещё не выполнены. Доступ ожидает прямого разрешения.
Локально: readcutoff next-120 отдельно от intentcutoff next-60; финальныйклик
ограничен next-20 дляcooldown. Вовремя ownedclaim ждёт540сек в своём окне
доnext-60, но поздний claim запрещён. Freshclock послеDBI/O защищает claim/
intent/click. Chooser30сек с однимopener, дублиstop. Resume_not_before gate
подготовлен для строго будущего штатного slot. ИсторияVM/paused не менялись.
Сводка проверок/ревью и условия будущего unpause: BATCH_WINDOWS_2026_10_25.md.
Полный локальный source-only unittest:620тестов/113.690сек/OK; наVM этот
снимок ещё не проверен и не установлен.

## Проверка сбоев25октября —14:46YEKT

Worker10480 с12:04:16 на paused=action_stopped_community_prepare, job450failed
безintent/attempt: после opener за9сек не появилась опция сообщества. До паузы
58verified+4already,6partial по cutoff и2spacing skip;16последующих слотов
не claimed. Вчерашний120сек запас чрезмерно применяется и к готовым действиям,
а смешанная длительность refresh конфликтует с540сек фактического интервала
и120сек допуска claim. Это две проблемы времени, не массовые ошибки VK.
API budget2296/5000 за24ч, safety_lock пусты. Job391 ранее разрешён второй
проверкой подтверждения без нового intent. Read-only job450probe в14:45 успешен:
identity/source/date/destination, club option2267мс; все action/history/plan hashes
неизменны, отправок0. Историческая причина UI-сбоя не доказана. Паузу не снимали,
job450 не переоткрывали, код/config не меняли. См.INCIDENT_2026_10_25.md.

## Ограничение подготовки временем следующей пачки —24октября

Разобран18:19:45пропуск: slot55/18:10 завершился18:22:56 из-за второго полного
reserve refresh после3свежихлайков. Теперь полный исходный old-обход сохраняет
свежие оба слоя с исходным fresh_after; new priority прежний, второго прохода нет.
Если резерв ещё не обновлялся, дополнительно читается только до cutoff
следующего сохранённого slot/конца окна минус120сек. Подготовка исходного слоя
тоже ограничена. Перед HTTP/pacing проверяется20сек запас. HTTP-only helper
имеет общий20сек timeout с завершением/ожиданием только своего процесса;
VKReadClient/PublicPages по-прежнему владеют ledger/pacing/guards/проекциями.
Timeout до реального cutoff сохраняет technical read backoff/third-failure stop.
Повтор после cutoff pending,4/4 остаётся complete. План/лимиты/паузы прежние.
Проверки и установка: RESERVE_DEADLINE_2026_10_24.md.

## Заполнение остатка пачки резервом —24октября

По прямому запросу после queue_empty свежего слоя Worker делает один reserve
refresh и продолжает тот же slot до общего лимита4попытки, включая retry.
Новый fresh_after, pool очищается до чтения; только полные свежие наблюдения.
Сегодняшние обнаруженные посты первыми, затем предыдущие7дней. Ошибка/бюджет/
stop/window/guard закрывают остаток без старых данных. Смена дня закрывает
исходный claim, partial reserve исходы явно показаны панелью. План/квоты/
540сек/sleep20/API5000/5сек/09–19 сохраняются. Состояние установки и проверки:
RESERVE_FILL_2026_10_24.md.

## Аналитика отдельной вкладкой —24октября

Установлена наVM /#analytics с обычной сессией панели: новый read-only
vk_analytics_web.py, GET/api/analytics иrendererweb/analytics.js. Все64паблика,
выборзамера/недель/репостов, previewER/П3.3+K/N, недельныйминимум, ссылки.
НиAPIсбора, ниactorопераций приоткрытии нет; nativeсбор08/20 прежний.
VM112testsOK(skip5Node), localreader/HTTP11PASS, browserdesktop/mobile иreview
закрыты. Перезапущена толькоwebпанель, worker2092/5648 не менялись.
В publicVK найден новыйpost216229467_2152 послеутреннегосбора: за19–25живых>=3,
в утреннемfrozen2. Не переписыватьfrozen илиподменять данные свежимнаблюдением.
АвторизованныйUIпроверенfixture; nativeпанельоткрыта наобычномвходе.
Backupanalytics-tab-backup-20261024T100433Z, GitHubнет.
Подробности ANALYTICS_TAB_2026_10_24.md.

## Ожидание интервала пачки —24октября12:27YEKT

Исправлен преждевременный batch_spacing_wait: до intent короткое ожидание
внутри исходного120сек допуска, с шагами<=5сек и повторными guards.
Закрытые slots1/6/11 не переоткрывались; план/540сек/4/sleep20/09–19/API5000
сохранены. Timeline различает пропуски/будущее/текущую/досрочную пачку,
проверяет active_slot и обновляет cached freshness без API/сброса форм.
568localtestsPASS; VM source-only532OK/13Node skip; review45PASS;
установленныеassets проверены1440/390 на syntheticAPI, console0/overflow0.
Backup spacing-wait-20261024T072705Z,51protected hashes unchanged,
штатное обновление между slot20/21; одинworker5648(+2092), pausednull.
Первая штатная slot21/jobs236–239 завершилась12:35:01:4verified на всех трёх,
по1attempt, всеsleep>=20, новыеguards/uncertain/дубли0; прежниеjobs/попытки/план
сохранены. Ветка короткого ожидания доказана офлайн; полный240не принят.
Детали BATCH_SPACING_WAIT_2026_10_24.md.

## Подготовка оценки ответа — 24 октября

В отдельном POS-проекте добавлены pos_feedback.py/run-feedback.ps1: одна явно
выбранная ЭЦП, одна браузерная сессия/один вход; просмотр «Обращение рассмотрено»
и сверка исходного номера по verified отправкам этого actor всех дат, mode=ro.
Верхний ID карточки/номер PDF не подменяют регистрационный номер ПОС.
--scan только ищет; --open-rating-window открывает первое своё окно и прекращает
клики. Выбор/отправка оценки не реализованы; HTTP mutation barrier включается
перед входом в форму. Пауза1.5сек перед всеми кликами, включая NativeOK.
55POS tests PASS локально/в source-only копии VM; offline browser:1login/1page,
foreign skipped, radio untouched/submit0. Живой тест actor1 на VM прошёл ESIA,
автоматическое OK, roles (сокращённое имя), проверку полного ФИО в профиле,
главную и «Все уведомления». Две рассмотренные карточки не совпали с нашими
verified; при открытии третьей портал вернул POST429, штатный стоп без retry.
Окно оценки не открыто, оценок0; охват частичный, RCA429 не установлена.
Исправлены ожидание рендера/role-origin/карточка/landing; установленный CLI
сохранил БД. Разовые тестовые задачи после завершения удалены, постоянных новых
расписаний оценки нет; POS/VK worker/web не перезапускались. Документация в pos_automation:
docs/FEEDBACK_PREPARATION.md. GitHub не публиковался, VK UI/код не менялись.

## Очередь ЭЦП вкладкой — 24 октября

Вместо внешней ссылки добавлена вкладка «Очередь ЭЦП» на8787. Экран /pos/ использует существующий вход VK; данные читает ReadOnlyQueue mode=ro, отдельные действия не запускаются. Обновлены8исполняемых/статических файлов, сделан backup, перезапущена только VKUnifiedWebPanel. VK worker PID5840/7536, POSWebPanel PID4624 и POS DB SHA сохранены; реальный reader подтвердил15/15,[5,5,5]. Локально27web/retry/tab+39POS tests PASS; VM source-only27(skip8Node)+39PASS. Браузер1440/390: общий вход, раскрытие/переключение/возврат/logout, empty/stale/recovery PASS, без overflow. Production-пароль не извлекался: UI проверен на fixture, установленный reader — на живой БД. GitHub не публиковался. Детали: POS_QUEUE_TAB_2026_10_24.md.

## Отдельная очередь ПОС/ЭЦП — 24 октября

По прямому поручению установлена самостоятельная CLI на основной VM:
`C:\Users\vk-bot\pos_automation`, панель `http://192.168.100.46:8788/`.
В VK `web/index.html` добавлена только ссылка «Очередь ЭЦП», исходник сохранён.
VK worker, его квоты/SQLite/профили и Windows-задачи не менялись и не останавливались.
ПОС использует существующий scrypt auth-файл панели только для чтения,
с отдельными cookie и сессиями; ни пароль, ни хеш не копировались в документацию.
ПОС: 3 ЭЦП×5 начатых попыток/сутки YEKT, sleep10; 09:00 новые15 текстов,
11:00 native POSDailyWorker, 12:00 Codex audit; uncertain повторы сохраняют расход.
37 изолированных POS tests PASS локально/VM, web fixture browser PASS.
Реальная приёмка завершена:13новых3+5+5 и2прежних, всего15/15verified,
по5каждойЭЦП.13native OK автоматические,13уникальных новых номеров и13артефактов,
minпауза10.720598сек, exit0, ошибок/uncertain0. Отдельный аудит12/12PASS.
Временный SSH-сбой устранён ручным restart службы пользователем; его причина
не доказана. Сам worker закончил02:28:45YEKT ещё до восстановления SSH.
Подробный итоговый отчёт в отдельном проекте
`C:\Users\admin-2\Desktop\pos_automation\docs\DAILY_QUEUE_VALIDATION_2026_10_24.md`.
GitHub не публиковался, локальная папка VK не содержит `.git`.

## Третий аккаунт — четыре действия23октября18:08YEKT

По отдельному прямому поручению выполнены1like/1personal/1community в каждую
из двух настроенных групп третьей карточки (actor5). Jobs149–152, attempts1–4:
все verified, все четыре actor runs complete, ровно4попытки, паузы≥20сек.
Новые uncertain/locks/дубли/unmapped0. Резервная копия и читающий аудит наVM,
прежние jobs и все schedule_days неизменны; завтра остаётся60пачек.
Native worker не останавливался, после освобождения activity lease monitoring.
Это отдельная явная ручная приёмка, не разрешение повторять её в мониторинге.
Подробности: docs/THIRD_ACCOUNT_FOUR_ACTIONS_2026_10_23.md.

## Три аккаунта — 23октября17:10YEKT

Прямой выбор пользователя:60пачек по4, минимум9мин между фактическими
началами вместо10;09–19/sleep20/API5сек/5000/no-catchup сохраняются.
Общий540constant в генераторе/claim/reserve, bounded balanced-gap fallback
устраняет случайный отказ плотной генерации.550localtestsOK/514VMtestsOK
(skip11),1000seed и независимое ревью,17400дополнительных fallbackпланов.
Backup three-schedule-20261023T120620Z,16protectedtablehash неизменны;
штатный update/restart одногоworker, pausednull/activeuncertain0.
Третийactor5 повторно exactmatched17:09:15, enabledtrue/50/10/20,
две прежние пользовательские группы; ручных действий0/attempts0.
План24октября сохранён60слотов09:00–18:50, интервалы571–629сек;
3прежних dayplans неизменны, новые объёмы с24октября. Полный240день не принят.
См.THREE_ACCOUNT_SCHEDULE_2026_10_23.md; GitHub не публиковался.

## Настройки дополнительного аккаунта — 23 октября16:49YEKT

Исправлен отказ HTTP400: configure_account теперь принимает проверенное name,
которое отправляет add_any форма. Явные ошибки для отсутствующих двух назначений
и переполнения расписания сохраняют прежние настройки/введённый черновик.
Три полных набора80 не помещаются в прежние уникальные интервалы09–19;
квоты/паузы/dayplan не ослаблены.546изолированных localtestsOK,44VMtestsOK
(5Node skip, локально выполнены), браузерная форма проверена на tempregistry.
Установлены4source+2tests после backup; перезапущена только панель,
workerPID6600/реестр/конфигурация/dayplans сохранены. Actor5 подключён,disabled0.
Первоначальный сетевой Failed to fetch UNKNOWN и со слов пользователя прошёл;
не путать с доказанным отдельным HTTP400. GitHub не публиковался.
Подробности ACCOUNT_SETTINGS_2026_10_23.md.

## v6.34 — общий сбор публичных данных, 23 октября

Единый PublicPages/VKReadClient для analytics/каталога/пачки/статистики;
append-only vk_public_data.sqlite3, совместимые публичные проекции общей БД,
исходные времена и неизменные frozen snapshots. ОбычныйTTL300сек; перед пачкой
fresh_after точного старта с микросекундами. Плановая аналитика08/20 теперь
count100, как остальные читатели. Deadline/window после pacing до ledgerreserve.
543 localtestsOK,543 isolatedVMtestsOK(skip10Node,локальноPASS), финальное ревью.
Backup всехDB integrityOK,16sourcehashсовпали, markerвключён, PID6600live/panelRunning.
APIприёмка16:41:100постов, HTTP1/0/0 для analytics/stats/queue, ledgerdelta1,
исходныйobserved_atсовпадаетсвебкой; позднийfresh_afterотвергаеткеш. Digest19
историческихтаблицнеизменён. Профиль5 штатно matched/connected,disabled,квоты0.
Guardнеобходился. Slot31 16:47:01–16:55:10batch_complete,4verified(2likes/community/personal),обаactor.
Refresh49groups/49HTTP/87pool,unknown0/missing0;всесчётчикипослестартаrefresh.
У3из4storewitness,одинfreshrawfallbackбезкеширования(причинаUNKNOWN),неутверждать
универсальноекешированиекаждойстраницы. PID6600live/monitoring,pausednull,streak0.
Подробности SHARED_PUBLIC_DATA_V6_34.md и VALIDATION_V6_34.md. GitHubнепубликовался.


## Накопление для следующей выгрузки ЦУР — 23 октября

Обновление16:08YEKT по прямому запросу: исполняет задачаVM `VKOMSUAnalytics`,
S4U/Limited,08:00/20:00YEKT. Codex heartbeat14 PAUSED. В рабочее окно09–19
новый entry пропускает запуск безAPI; до каждого HTTP проверяет deadline
послеpacing. Guard/ledger прежние, worker не менялся.78VMтестовPASS,
S4U key/TLSpreflight0API и дневнойskip выполнены самойWindows-задачей,
LastTaskResult0, next23октября20:00. Полный плановый сбор ещё ожидается.
Состояние/CSV/JSON/HTML сохраняются наVM в observation.../scheduled/slot,
run_idслота идемпотентен, ошибкиAPIпослеаварии не повторяются.
Protectedaudit: толькоheartbeat. Код/установщик локально и наVM, GitHubнет.

По прямому запросу период наблюдения начинается14.10.2026, не8октября.
Состав64зафиксирован в analytics_local/observation-from-2026-10-14.json.
Первоначальный heartbeat id14 заменён указанной вышеVM-задачей.
Первый run1ef5f5db14114fceab065e055f357c3f за[14.10;24.10), текущий
день открыт. Новые run/snapshots/frozenотчёты, без перезаписи старых.
Счётчики до23октября восстановить нельзя. Новый период не сравнивать
с прежними921до следующей официальной выгрузки и уточнения её границ.
Инструкция и итог первого замера: [OMSU_OBSERVATION_FROM_14.md](OMSU_OBSERVATION_FROM_14.md).

## v6.33 — один повтор, 23 октября15:34YEKT

Прямое согласование: у логического действия максимум один повтор, три неуспеха
подряд ставят worker в attention, verified/already сбрасывает streak. Durable
две фазы/attempt journal, pending до следующей штатной пачки, held без подмены
uncertain успехом; restart running fail closed. Лайк только по точнымfalse/false/
canLiketrue, репост после intent только перечитывается без новой отправки.
Guard/429/CAPTCHA/identity/date/cleanup остаются немедленным стопом. Расход,
4/пачку,20сек/10мин,09–19,5000API и старые изоляции сохранены.

475 локальных testsOK,475 isolated VM testsOK(skip10Node,ониPASSлокально),
независимое ревью без важных замечаний. Backup integrityOK, штатная установка
наVM. Policy1 включена; PID9492 live/monitoring, pausednull/activeuncertain0,
streak0/held0/pending0; следующаяпачка15:45. Живой новый retry-сбой не создавался.

Job120 согласован с прежним явно разрешённым manual124 после4новыхpositive
чтений/2открытий безклика. Текущийpost/jobverified, pointer124; первая123 сохранена
uncertain в recovery journal. Всеattemptrows, другиеjobs/dayplan/2iso неизменны;
снята лишь like_confirmation_timeout. Прежнее описание паузы ниже историческое.
Панель показывает обе фазы и0/3. GitHub не публиковался. См.VALIDATION_V6_33.md.

**15:45–15:47:** ordinary waiting_profile_lock до claim из-за отдельного
vk_analytics_cli.py(старт15:43:36), общий exclusive activity lease. Grace120сек
истёк, пачка пропущена, следующая15:59:47; pausednull/streak0, новыхattempts0.
Ни worker, ни сборщик не останавливались для проверки; lock/catch-up не трогать.
Реальная новая пачка ещё не принята, ждать штатного доступа/интервала.

**16:05:04YEKT — новая пачка принята:** slot28 15:59:47–16:05:04,
jobs121–124 четыреlikeverified: actor1/130–132,actor2/125. Первые фазыcomplete,
новыхuncertain0,pausednull/streak0; прежниеизолированные47/75 сохранены.
Прежнее ожидание первой пачки закрыто. Ошибку для live retry не создавали.

## Явный single-like retry — 23 октября14:46YEKT

По прямому «попробуй запусти лайк со второго аккаунта для этой записи» выполнена
одна новая попытка124 actor2/source104486183_14106. Beforefalse/canLike true,
один click, sleep20.001, после новой навигации JSON/footer обаtrue: новый лайк
verified. Приватный backup/audit и отдельный diagnostic сохранены; like usage14.
Старый job120/attempt123/postuncertain не переписаны, day plan прежний,
paused=like_confirmation_timeout/active_uncertain1; общей отмены паузы/изоляции
пользователь не разрешал. Новый manual last_action_batch сохраняет10мин до
будущей пачки. Видеопост допускает успешный лайк; причина первого сбоя UNKNOWN.
Разрешение строго на один состоявшийся повтор, не будущие автоматические retries.
См. `LIKE_UNCERTAIN_2026_10_23.md`. Следующие false-наблюдения исторические.

## Уточнение аналитики v1.1 — 23 октября, 15:20 YEKT

Расследование921/693: по8сообществам проверены ID двух страниц owner/all,
доступные посты не теряются между ответом и сохранением. Найден старый
API-ID219875672_1650 от14октября, сейчас VK явно пишет «Пост удалён».
Исправлен учёт старого публичного каталога; он читается mode=ro и копируются
только публичные ID/даты, без истории действий. Полнота старого отчёта
complete отозвана, frozen payload сохранён. Новый run195d3d93ead44834a4da6a0164374a1a:
693доступных+1недоступная, минимум694, partial/ER UNKNOWN. Само вхождение
удалённого ID в921ЦУР не доказано, полной причины228пока нет.63изолированных
VMтеста PASS, ревью закрыто; worker не останавливался, protecteddiff только
heartbeat. Доказательства: outputs/omsu-2026-10-23/counts-audit/.

## Аналитика госпабликов v1 — 23октября2026

По прямому заданию реализованы `vk_analytics*` (store/calculation/import/collect/CLI),
отдельная analytics SQLite, append-only снимки, frozen состав/отчёт, сравнениеЦУР.
НаVM прочитаны64 официальных группы:693поста за предполагаемые8–14октября,
L/K/R/V5117/110/259/314998, ER1.741598359%,П3.3=48.377732204%.
Excel16октября:921пост, ER2.773021112%,П3.3=77.03%; период/времяЦУР неизвестны.
80диагностических вариантов не воспроизвели выборку. Не подменять отношение
сумм среднимER: последнее лишь диагностическое, сK/N. ByID batch2 вернул1051.
56изолированныхVMтестов PASS, независимое ревью и исправления4ошибок.
Старый набор CLI имеет12ошибок совместимости, весьsuite не заявлен зелёным.
Прежний worker не останавливался/не перезапускался. Установлены только новые
аналитические модули; GitHub не опубликован, .git здесь отсутствует.
Полные детали: [OMSU_ANALYTICS.md](OMSU_ANALYTICS.md).

## Новый uncertain — 23 октября около14:25YEKT

Job120/actor2/like/attempt123, источник104486183_14106: like_confirmation_timeout,
uncertain и paused после14:06. Два свежих открытия/четыре наблюдения точного
actor/date: liked=false/footer=false. Причина неприменения UNKNOWN; успеха,
retry/reset или снятия паузы нет. Worker живой, новая активная uncertain1,
старые изоляции2. На копии БД проверен вариант точной третьей изоляции с
сохранением job/actor/квоты; рабочего разрешения ещё нет. См.
`LIKE_UNCERTAIN_2026_10_23.md`. Нижележащая успешная пачка историческая.

Гипотеза видео проверена около14:33: attachment video подтверждён; actor2
can_like=1, точная стандартная кнопка вне плеера доступна и не перекрыта.
У actor1 лайк на этом же видео уже установлен (job119 already), у actor2 false.
Структура/обработчики совпадают с успешными фото-постами. Новых кликов0;
связь сбоя с видео не доказана, причина остаётся UNKNOWN.

## v6.32.1 — personal original, 23 октября 2026

По прямому «делаем» исправлена причина job112: canonical key=-VK owner_id
сохраняет personal/community знак в парсере, истории, target scan/confirmation.
386 VM tests OK (7 Node пропусков отдельно PASS локально), 74 профильных PASS,
независимое ревью без важных замечаний. Исправленный установленный код трижды
подтвердил именно failed source с верным actor/date/personal original без кликов.
Backup/integrity и сравнение всей action/community истории и day plan пройдены.
Job112 failed без attempt сохранён, снята только связанная пауза и записан аудит.
В 13:43 YEKT один worker PID6056/live, paused=null, active_uncertain=0,
две старые изоляции, attempts126/119. Ближайшая пачка13:44:04; новый реальный
personal-original репост ещё не принят. См. `PERSONAL_ORIGINAL_V6_32_1.md`.
Нижележащая остановка историческая и устранена этой версией. GitHub не опубликован.

**13:49:08 YEKT:** slot19 после ремонта batch_complete, jobs113–116 все verified
и сверены с actor ledgers: actor1 community127/like128, actor2 like120/personal121.
По одному reserved/final_click/result, cooldown>=20.006сек. Worker monitoring,
paused=null/active_uncertain=0; job112 failed/attempt=NULL сохранён. Это новые
штатные действия обоих аккаунтов; personal-original community live-send отдельно
ещё не принят (read-only proof именно проблемного источника пройден).

## Приёмка первой новой пачки на VM — 23 октября10:52YEKT

Slot7/jobs76–79: по2community каждого actor во все4назначения, всеverified/complete.
Истории111/103, новыхuncertain/guards/дублей0, прежние47/75сохранены. Паузы20сек
подтвержденыdiag, refresh5запросов/5групп/6постов занял33сек. Worker7800live,
свежийmonitoring, безостановки/ручныхотправок. Heartbeatпослеприёмки30мин;
неповторятьсообщённыйрезультат. Полныйденьещёнепринят. См.STARTUP_V6_32.md.


## v6.32 — свободное подключение аккаунтов 23 октября 2026

**Актуальная остановка после 12:48 YEKT:** job112/actor1 failed без attempt,
paused=action_stopped_source_reference. Read-only проверка доказала ограничение
парсера: исходный пост сообщества копирует оригинал личной страницы, а
_REPOST_REFERENCE_JS разрешает только отрицательные owner_id сообществ.
Кнопки доступны; это не VK-denied. Три чтения воспроизвели отказ, попытки126/126.
Worker живой, stop_requested=false, active_uncertain=0; пауза не снималась,
job не переоткрывался. Код не исправлялся. Подробности —
`SOURCE_REFERENCE_PERSONAL_2026_10_23.md`. Успешный статус ниже исторический.

**Реальная приёмка 11:55 YEKT:** пять пачек по четыре подтверждённых действия,
20/20 `verified` сверены с actor ledgers. Каждый аккаунт: 5 лайков, 1 личный
и 4 community-репоста. Один дополнительный `already` без попытки. Новых
failed/uncertain/guards нет, оба enabled, worker live с живым heartbeat.
Состав пачек динамический, максимум4 суммарно, интервалы соблюдены.
Это заменяет ожидание первых результатов в следующем абзаце; полный день
ещё не принят. Детали — `STARTUP_V6_32.md`. Heartbeat найден PAUSED/30мин;
при этой проверке не возобновлялся, работающий worker не останавливался.

**Последующая приёмка после входа пользователя, около 10:39 YEKT:** оба профиля
повторно открыты и дали точный matched собственного VK ID; известные actor/история
сохранены (actor1→profile4, actor2→profile3). Изолированно на VM 378 тестов OK,
7 Node пропусков отдельно PASS локально. По прямому запросу оба actor включены
с прежними квотами; существующий live worker продолжает без перезапуска.
Ближайшая пачка 10:46:42 YEKT, первые новые результаты ещё ожидаются. Heartbeat
проверяет каждые 5 минут до verified обоих, затем 30 минут. Детали/границы —
`STARTUP_V6_32.md`. Следующий абзац описывает исходную установку до ручного входа.

По прямому запросу пользователя UI больше не требует заранее выбрать VK ID
или номер старого аккаунта. Реестр переведён в `account_flow=add_any`; прежние
карточки архивированы с сохранением bindings, SQLite и расхода. Пользователь
добавляет профиль и входит в любой нужный VK на VM, worker автоматически читает
точный собственный header ID. Для известного ID подключается прежний actor с
историей, для нового создаётся отдельный. Включение очереди остаётся явным.

Причина сбоя открытия второго окна: исключительная `.vk-activity.guard` всего
первого входа блокировала второй до запуска браузера. `vk_activity` даёт разным
login-профилям совместную OS reader lease; профиль остаётся исключительным,
worker — с исключительной lease пачки. При занятости пачки вход безопасно ждёт.
Пути/physical locks согласованы для переподключённого actor1. Перед заменой
проверяются захваченный путь, занятость цели и вместимость будущего расписания.

Установлено на VM после приватного backup и одной штатной остановки для
обновления. В интерактивном сеансе VM подтверждены два видимых окна Chrome,
оба `awaiting_login`; фактический вход VK требует пользователя. Старая история
реакций совпала с исходной, попытки 109/101; новые профили — 0/0. Worker live,
без paused/активных uncertain, две разрешённые изоляции сохранены.
Локально 378 тестов PASS. Детали VM-приёмки и границы — `ACCOUNTS_V6_32.md`.
Публикации GitHub нет. Ниже — исторические срезы.

## v6.31 — вход и статистика 23 октября 2026

Сохранённая binding не proof: свежий точный header ID сравнивается с ожидаемым
и другими привязками, неверный профиль/legacy ready блокируют finish/enable.
Новая/отрицательная проверка отменяет старый положительный proof. На VM выявлен
ID аккаунта1 в профиле2; профиль1 не подтвердил вход, оба disabled, ID/история
не переставлены. Нужен вход пользователя в правильные профили.
Группы: последние100 на группу, четыре nullable счётчика, known_sum/K/N, даты;
bounded API refresh с прежним общим бюджетом/пауза5сек. Живой проход57/57,
просмотры5677/5700, partial2непригодные записи без ослабления валидатора.
Activity OS lease защищает пачки от конкуренции с читающими операциями.
354 локальных теста PASS; чистый VM прогон354 OK,4 пропуска Node fixtures.
Оба auth proof файла не изменились от тестов. GitHub не опубликован. Подробности —
`AUTH_STATS_V6_31.md`; нижележащие срезы исторические.

## v6.30 — перенос 23 октября 2026

Основная машина Windows VM `vk-bot@192.168.100.46`, `C:\Users\vk-bot\vk_bot`.
Единственный worker на VM; прежняя задача исходной Windows отключена.
HTTP панель также на VM `192.168.100.46:8787`, `--local-backend`; прежние локальные
worker и панель отключены. SSH RPC поддерживается как дополнительный режим.
Канонический GitHub **https://github.com/BorisDruzak/vk_bot**, `main`.
Исходная локальная папка без .git; наличие перенесённого кода не означает push.
Remote main проверен при переносе: `d58069198aa69ff49558dae7bf9385bd66d45199`.

`vk_accounts` — N аккаунтов/enabled/квоты; `vk_control` — фиксированный JSON RPC
и интерактивные входы; `vk_statistics` — mode=ro последние100/группы;
`vk_web`/`web/` — HTTP панель. Существующий day plan неизменен, новые квоты на
первый не запланированный день. enabled проверяется перед резервацией/кликом.
uncertain/изоляции/расход и защиты сохраняются. Факты и границы приёмки —
`VALIDATION_V6_30.md`. Следующие датированные разделы исторические.

**Дата среза:** 2026-10-22, рабочий режим v6.29, свежие счётчики перед пачкой и учёт комментариев. **Текущая локальная версия:** v6.29.8, ежедневная автоматика; исторический тест v6.27 по две отправки с каждого аккаунта выполнен успешно (4/4 `verified`, все `complete`), после каждой попытки в скрипте `time.sleep(20)`. Исторический тест личной стены 30/30 относится к v6.26. **Канонический репозиторий:** [BorisDruzak/vk_bot](https://github.com/BorisDruzak/vk_bot), основная ветка `main`. Правила проекта записаны в `../AGENTS.md`. Этот документ — исходная точка для последующих чатов и задач Codex. При противоречии с кодом сверять текущий `main`, локальные изменения и фактические результаты тестов; исторические разделы описывают состояние на соответствующем этапе.

**Публикация кода:** коммит `ee47478bce9341af97cd846d482bc2363df5c89b` первоначально опубликовал только документацию поверх v6.25. По следующему прямому запросу пользователя подготовлена публикация самой v6.26 с тестами и CI; все восемь исполняемых Python-модулей совпадают с локальным снимком успешного теста 30/30. Контрольные суммы и проверка — `VALIDATION_V6_26.md`. CI и актуальный SHA публикации проверять в GitHub отдельно от факта локального теста.

## Бюджет читающего API — v6.29.8

После отдельного прямого запроса22октября разрешён и настроен бюджет5000 за скользящие24ч вместо2000. Учитывается прежний api_read_attempts, сброса нет. Значение передаётся и в refresh перед пачкой, и в фоновой каталог; bound каталог/scheduler согласован с VKReadClient5000. Паузы/API5сек, действия20сек, пачки10мин, окно09–19 и квоты действий прежние. Полный день ещё не принят. См. API_BUDGET_V6_29_8.md.

## Пропускная способность и восстановление — v6.29.7

Итог22октября:71verified/73попытки,2uncertain аккаунта2 изолированы по явным ответам (47/87 и75/101), activeuncertain0. Подготовка поздних пачек21мин мешала сохранённым интервалам; пять остановок также потеряли около166мин. Разрешено API5сек, действия20сек и пачки10мин сохранены. Доказанные технические ошибки до intent получают ограниченный backoff без переоткрытия failed/сброса попыток; guards/uncertain не отменяются. Полный день новой версии ещё не принят, GitHub отдельно. Детали THROUGHPUT_RECOVERY_V6_29_7.md.

## Подробная диагностика — v6.29.6

Новые `[VK EVENT]` и scheduler_diagnostics сохраняют этап/время/job/attempt, fixed-код, типы причин и номера строк без текстов исключений/DOM/секретов. Метаданные response/requestfailed читаются без URL/body. Последняя проверка перед кликом выполняется после telemetry; ошибки логирования не останавливают действия и не коммитят чужую транзакцию. Квоты, паузы и история прежние. См. DIAGNOSTICS_V6_29_6.md. Первая новая штатная пачка slot34/jobs66–69 завершена22октября17:56:02: аккаунт1 лайк+community, аккаунт2 два community; все4 verified/complete, новые ошибки/uncertain/дубли0, истории106/98. Прежний изолированный47 сохранён. Результат сообщён; неизменный отчёт не повторять. Полный дневной план ещё не принят.

## Уточнение пользователя: проверка без остановки

22октября пользователь уточнил: остановка требуется самим скриптом, а не процедурой проверки. Мониторинг/аудит/тесты сами по себе не разрешают stop_requested/--scheduler-stop/остановку задачи или процесса. При занятом профиле/lock читающую диагностику откладывать. Действующие защитные остановки скрипта и явная команда пользователя сохраняются. Установка изменения — отдельная разрешённая операция, не обычная проверка. Job47 уже изолирован по явному ответу; старое ожидание решения больше не является основанием для паузы. Правило записано в AGENTS.md.

## Автопропуск по прямому запрету VK — v6.29.5

Scheduler обоих аккаунтов может пропустить действие без попытки/расхода квоты только по двум согласованным actor-page JSON чтениям точного поста: likes.can_like=false либо likes.can_publish=false/repost_disabled=true, при отсутствующей прежней реакции. Код/неизвестная разметка не выдаются за запрет VK. Actor флаги не меняются, failed job имеет vk_*_denied/evidence; следующая кандидатура выбирается в той же пачке. Остальные лимиты/паузы и истории сохранены. Подробности — AUTO_SKIP_V6_29_5.md.

## Новая остановка до попытки —22октября

Job61/account1/like/193586621_17663 failed без attempt, истории102/94. Точная панель реакций отсутствует в нескольких читающих открытиях; контрольный пост того же профиля исправен. При разрешённом безопасном ремонте до попытки точный пост откладывается в существующем post_access как unresolved, job не переоткрывается; код/селекторы не ослабляются. Подробности — PRE_ACTION_FOOTER_STOP_2026_10_22.md. Почему VK не отрисовал footer — UNKNOWN.

## Личные действия и авторемонт — v6.29.4

Job47/account2/like/attempt87 не подтверждён. Два читающих открытия219875672_1661 показали liked=false и footer=false, история96/87 сохранена, кликов0. Почему прежний клик не применился — UNKNOWN, сетевой ответ не сохранён. Проверка теперь ждёт согласия VK+footer до9сек без кликов, сохраняет отдельный timeout и boolean evidence. Авторемонт разрешён только для доказанных безопасных причин/положительных результатов без повторов; CAPTCHA429/профиль/неподтверждённый исход не разблокировать. Пользователь явно выбрал изоляцию и продолжение: точный пост219875672_1661 исключён для аккаунта2 из всех будущих действий scheduler; job47/attempt87 остаются uncertain, расход лимита сохранён. Это отдельное разрешённое исключение, остальные uncertain блокируют. См. POST_ISOLATION_V6_29_4.md. Подробности — LIKE_CONFIRMATION_V6_29_3.md.

## Репост репоста — v6.29.2

**Новая реальная приёмка22октября:** первая community-пачка worker6.29.2 завершена13:09:57YEKT. По2новых репоста каждого аккаунта, по одному во все4свои группы; jobs34–37, все verified/complete. Истории91/82, новых paused/uncertain/предупреждений/повторных canonical-попыток нет. Это приёмка новых отправок после исправления; старый job21 согласован отдельно. Полный дневной план ещё не принят. Подробности — COMMUNITY_CHAIN_FIX_V6_29_2.md.


Подтверждено: job21/attempt83 отправил242481741_3 в11:39:08 YEKT. Выбран69126405_4382, его copy_history=240769577_28, и именно оригинал VK вложил в цель. Прежняя проверка искала промежуточный ID и дала ложный timeout. Worker теперь проверяет точную исходную цепочку, подтверждает оригинал и сохраняет аккаунтные canonical refs против повторных обёрток. Истории83/74, лимиты/паузы сохраняются; восстановление без отправок по прямому запросу. Подробности — [COMMUNITY_CHAIN_FIX_V6_29_2.md](COMMUNITY_CHAIN_FIX_V6_29_2.md). Новые uncertain автоматически не снимать.

## Исправление проверки источника — v6.29.1

По прямому запросу пользователя разобрана пауза action_stopped_source задания18: оно failed, без попытки, не переоткрывать. Истории81/73, новых uncertain нет. Историческая точная причина потеряна старым журналом; текущий источник и профиль подтверждены read-only. Исправлена воспроизводимая ошибка однократного чтения ещё не появившейся даты, добавлены раздельные безопасные причины. Окно, квоты, sleep20 и свежий приоритет сохранены. Подробности — [SOURCE_STOP_2026_10_22.md](SOURCE_STOP_2026_10_22.md). Новые paused/uncertain автоматически не снимать.

## Свежий приоритет перед пачкой — v6.29

Перед каждой рабочей пачкой `vk_batch_engagement.refresh` обновляет через `wall.get` счётчики всех известных подходящих постов старшего доступного слоя: сначала сегодняшний день YEKT, затем резерв предыдущих семи дней. В пачку попадают только точные публикации, действительно возвращённые этим обновлением с тремя известными счётчиками. Приоритет — минимальная сумма **лайков + репостов + комментариев**; неизвестное значение не ноль. При равенстве сохраняются баланс лимитов/аккаунтов и дата публикации. Комментарии только считаются, бот их не пишет.

Перед каждым выбором внутри пачки перечитываются подтверждённые собственные действия обоих аккаунтов. Эффективный счётчик равен `max(API + наши verified после наблюдения, число наших различных verified)`: прежние действия не прибавляются повторно поверх API, а задержка API не уменьшает подтверждённый минимум. `uncertain` не становится подтверждённой активностью, но продолжает расходовать квоту и останавливать worker. Истории и запреты повторных отправок сохранены.

Чтение разделяет прежний бюджет API 2000 запросов/24 ч, паузу минимум 20 с и предел 10 страниц на группу. Курсоры фонового каталога сохраняются; code 7 откладывает только обновление счётчиков недоступной группы на час. При остановке, исчерпанном бюджете или отсутствии свежих полных данных действия по старому кешу не запускаются. Новые посты в прочитанных группах включаются, но минимум гарантируется только среди доступных обновлённых кандидатов, а не всех неизвестных публикаций VK. Долгое чтение может привести к пропуску плановых пачек; пропуски не догоняются.

Окно 09–19, дневные максимумы 50/10/20 на аккаунт, разделение community 10+10, пачка до четырёх, один action за раз и `sleep(20)` после попытки не меняются. Дополнительно сохранён `last_action_batch`: десять минут между началами фактических попыток разных пачек проверяются после обновления и при резервации, включая перезапуск. Диагностика: `refreshing_counters`, `last_counter_refresh`; обоснование выбора — отдельная таблица `scheduler_priority_evidence` с наблюдёнными и эффективными числами.

Проверка и границы приёмки — [VALIDATION_V6_29.md](VALIDATION_V6_29.md). Текущая v6.29 локальная; публикация в GitHub в этом изменении не выполнялась.

## Основа v6.28 — ежедневная автоматика в рабочем режиме (исторический срез)

- Последнее прямое указание пользователя 2026-10-22: включить реальные действия с 09:00 YEKT и проверить старт. Dry-run штатно остановлен, Windows worker запущен с `--scheduler-daemon --confirm`; первый старт состоялся. По отдельному запросу диагностики подтверждён фактический репост №2 и вручную согласован учёт, без повторной отправки. Не расширять объём и окно; новые предупреждения/uncertain автоматически не снимать.
- Окно 09–19 YEKT,50 лайков+10 личных+20community на аккаунт/календарный день, community10 в каждую из двух своих групп.160 всего,40 пачек по 4, ежедневный сохранённый jitter±120 с, уникальные плановые интервалы. Один action за раз; минимум 20 с после попытки.
- Новые — текущий день; резерв — предыдущие 7 дней. Меньшая известная сумма likes+reposts повышает приоритет внутри каждого возрастного слоя; неизвестные/старые счётчики не нули.
- Реальный API:57/57 групп,56complete/1partial invalid-items,5698 записей первых страниц. Счётчики отдельно подтверждены на 188206921, ключ принудительно прочитан из api_keys.txt без вывода значения. Живые read-only6 путей обоих аккаунтов прошли, action_attempts73/64 не изменились, scheduler jobs0.
-208 профильных+1 отдельный regression PASS. Команды/ограничения/актуальные результаты — `AUTOMATION_V6_28.md`, `VALIDATION_V6_28.md`. Первый старт 22 октября в09:00:56. Два community-репоста подтверждены; №2 сначала uncertain, затем вручную verified по точной записи на стене242479058_2 (09:02:57), без новой попытки. Исправлено ожидание подтверждения: одна навигация и до9сек DOM-чтений, без повторной отправки; безопасные коды причины сохраняют этап сбоя.129 профильных тестов при исправлении PASS. Истории74/65, квоты сохранены. После проверки исправления штатный live-worker восстановлен; Codex heartbeat30мин. Полный дневной цикл ещё не принят.
- Исходники пока локальные; публикация v6.28 в GitHub не выполнялась. Профили/базы/ключи/реальные CSV сохранять только локально.

## Локальное расширение v6.27

- `--repost-community ID` для обоих аккаунтов, одно назначение за запуск. Без флага используется прежняя личная стена.
- `--dry-run` выбирает сообщество и проверяет диалог без лайков, финального клика и записей попыток/запусков/репостов. `--confirm` с ним запрещён.
- `vk_community.py`: отдельная история назначения в базе аккаунта, бюджет, CLI, отчёт. `vk_community_ui.py`: точный исходный пост, выбор ID и проверка целевой стены. Личный `posts.repost_status` не используется как доказательство репоста в сообщество.
- Реальный тест 2026-10-21: по одному репосту в каждое из двух сообществ обоих аккаунтов (2+2), все подтверждены. Новых лайков нет, личные состояния сохранены, история попыток 73/64, предупреждений нет. Пауза `time.sleep(20)` встроена в скрипт, включая исключения после резервации и последний репост запуска.
- Проверка и команды: `COMMUNITY_REPOST_CLI.md`, `VALIDATION_V6_27.md`. Актуальная опубликованная основа проверена как `d58069198aa69ff49558dae7bf9385bd66d45199` (v6.26); расширение v6.27 пока локальное.

## 1. Назначение, окружение, пользовательские требования

Проект возник как локальный Python-скрипт для обработки публикаций ВКонтакте с одного собственного аккаунта. Затем развивался в систему чтения **57** публичных сообществ Сосновского муниципального округа Челябинской области, в том числе паблик администрации и ТУ/муниципальных учреждений. Перечень сформирован из пользовательского Excel и хранится в `vk_groups.json`.

Рабочая среда: **Codex Windows app, PowerShell, Python (`python`/`py`), Playwright Chromium**, сохранённые профили браузера, локальная **SQLite** и CSV отчёты. Исторические примеры CMD явно помечены; в текущем терминале по умолчанию использовать синтаксис PowerShell. Рабочая папка называется `vk_one_account_poc`; абсолютный путь не следует жёстко прошивать. Пользователь предпочитает запуск одной CLI-командой без подтверждений по каждой публикации и отчёт по фактическому результату. Никакие реальные учетные данные не должны передаваться в чат/публичный репозиторий.

Интересующий период отбора — **последние 7 дней**, приоритет более новых публикаций. Не считать дату `feed-approx` или текст `1 ч назад` точной датой. Для чтения публичных стен пользователь зарегистрировал приложение VK и получил **сервисный ключ**, который используется как `VK_API_TOKEN` в окружении Windows; сам ключ в исходниках отсутствует.

## 2. История по версиям

### Первые POC (v1–v5)

- Playwright использовался для входа и работы с одной конкретной публикацией VK.
- Подтвердили отдельный репост в личную стену. Базовый риск: неправильный клик по соседней публикации или комментарию.

### v6–v6.7: пакетный движок, DOM и SQLite

- Обнаружение записей, SQLite `pending/already/verified/uncertain`; проверка перед действием.
- Выявили ошибку: код нажимал **лайк комментария**, а не основной публикации.
- Точная разметка основного поста: `data-testid="post"` плюс `data-post-id="-<group>_<post>"` и `data-post-nesting-lvl="0"`. Внутри могут быть вложенные repost-посты с nesting-level 1; комментарий имеет `data-testid="comment-like"`.
- Основной footer имеет `data-testid="post_footer_action_like"`, `data-user-likes="false"`, иногда `aria-hidden="true"` даже когда кнопка доступна; сама кнопка может находиться ниже viewport. Это исправлено поиском в корне конкретного поста и Playwright прокруткой к элементу.
- 2026-10-20 подтверждено на живом VK: один targeted пост — новый лайк и репост `verified`; пакетный режим правильно пропускал уже имеющиеся действия.

### v6.8–v6.10: аудит, доступность, защита

- `--audit-pending`, `--audit-report` и очередь проверенных кандидатов.
- Записи VK могут выдавать «Пользователь предпочёл скрыть эту страницу»: отдельный `hidden`, безопасный пропуск, не путать с `unresolved`.
- Для ошибки до публикации не следует помечать успешный репост; при сомнении нужно сохранять `uncertain` и не повторять клик.
- Добавили общий учет попыток в `action_attempts`, предупреждений VK и первоначальные консервативные лимиты. Они были техническими предохранителями, **не** официальными квотами VK.

### v6.11–v6.15: 57 групп, даты и частота запросов

- Добавлены `vk_groups.py`, `vk_groups.json`; сканировали 57 групп с приоритетом недельной давности.
- Открытие отдельных постов иногда давало правильный URL, но пустой DOM. По диагностике отдельного запуска пост появлялся: `page-single-post=True`, `data-post-id` на месте, `like/share-buttons=1`. Исправлено однократным безопасным повторным открытием, без кликов, с отдельным `unresolved` при повторном сбое.
- В UI VK дата бывает `data-testid="post_date_block_preview">1 ч назад</a>`, tooltip может соединять `20 окт в 20:28` и `4 просмотра`. Надёжнее JSON timestamp конкретного поста.
- Критический сбой: VK показывал `Too many requests per second` при множестве браузерных запросов; старый парсер не обнаруживал текст. Добавлены реакция на HTTP 429/текст ошибки и глобальный стоп; пейсинг чтения, ограничение прокруток и прогресс сканирования в SQLite.
- Снятие блокировки допустимо только после ручной проверки и команды `--ack-warning`. Был эпизод видимой «пропажи» блокировки, который позднее объяснился фактически выполненной командой `--ack-warning` — это не подтверждённый баг.

### v6.16–v6.18: переход к официальному VK API

- Создан `vk_unified.py` + `vk_api_read.py`: чтение публичных стен `wall.get` через сервисный ключ, хранение пар `(owner_id, post_id)` и точных дат в SQLite.
- В первой версии общий кеш браузерного сканирования подавлял первое API-чтение (`[API CACHE]`); исправлено отдельной таблицей прогресса VK API.
- На живом VK успешно протестировано получение по **100** записей администрации, ТУ №1 и еще одного сообщества; даты получены без открывания страниц в браузере.
- v6.18 ввела API provenance: автоматическая очередь использует только ID и timestamp, точно подтверждённые через VK API; изначальные browser-only записи остаются в SQLite, но не используются для отбора. Пагинация через `offset`, статусы `complete/partial`, исключение старых закреплённых публикаций при определении недельной границы.
- Целевая ссылка собирается напрямую: `owner_id=-188206921`, `id=13413` → `https://vk.com/wall-188206921_13413`. При этом API не подтверждает, что текущий пользователь поставил лайк: состояние реакции проверяется в авторизованном Chromium.

### v6.19–v6.22: отчёты и репост-диалог

- Экспорт и аудит 60 публикаций, CSV как результат проверки, а не обязательный ручной input.
- Пользователь предпочёл **не вводить `approve=да` для каждой записи**, поэтому v6.21 стала выбирать посты автоматически из API SQLite за 7 дней; CSV только итоговая отчётность.
- v6.21 при попытке репоста основной публикации остановилась: «Не найдены обе радиокнопки назначения в диалоге VK». Проверка диалога через ~400 мс была слишком ранней и преждевременно переводила статус в `uncertain` ещё до финального клика. v6.22 добавила ожидание до 9 с, диагностику и учёт попытки только перед финальной публикацией.
- На живом VK v6.22 репост успешно подтверждён; `--pair-status` показывал 1/30 нового репоста, 0/30 новых лайков, потому что лайк уже существовал.
- Суточный бюджет 5 репостов исчерпывался при наличии доступных лайков, отчего планировщик мог выбирать `like` без `repost`. Пользователь захотел вводить числа в каждой команде, не менять их в JSON.

### v6.23: числовой запуск и подтверждённые 30/30

- Добавлен режим `--run-actions --likes N --reposts M --max-posts K --confirm` с числом **попыток** каждого действия за конкретный запуск. В нём старые квоты JSON не ограничивают цель, но остановка по реальным ограничениям VK, CAPTCHA и неопределённым итогам сохранена.
- Фактический **боевой тест 2026-10-21**: команда на **30 новых лайков + 30 новых репостов**, 60/60 действий подтверждены VK, проверено **33 публикации**, статус запуска `complete`. Продолжительность по логу: `07:31:33+00:00 → 08:02:17+00:00` (примерно 31 минута), CSV содержит 33 строки. `--action-status` подтвердил по 30/30 каждого вида.
- После теста `--safety-report` показывал **71 попытку действий за скользящие 24 часа**, активных предупреждений VK на тот момент не было. Это **не гарантия**, что VK никогда не применит позднее ограничения, и не официальная «безопасная частота».

### v6.24: подключение двух личных профилей — что реально готово

- Новый модуль `vk_accounts.py` и ключ `--account 1/2` для `--login-only`, `--check-account`, `--accounts-report`.
- Аккаунт 1 использует существующий `.vk-local-profile`. Аккаунт 2 — **отдельный** `.vk-local-profile-2`; cookies не копируются. Реестр ID — локальный `vk_account_bindings.json`.
- **Проверка на живом VK 2026-10-21 PASS:** оба профиля успешно привязаны к разным числовым VK ID, повторная проверка второго прошла без ввода пароля, `--accounts-report` подтвердил два профиля.
- ВАЖНО: в v6.24 **НЕ реализованы** отдельные базы статусов/действий второго аккаунта и его лайки/репосты. Любая старая команда действий с `--account 2` должна завершаться до обращения к SQLite/Chromium. Старые действия №1 остаются без изменений.
- По тестам: 15 новых unit-тестов аккаунтов PASS, выборочный регресс 77 PASS, но полный набор v6.24 не смог завершиться в доступное время из-за старых браузерных ожиданий. Полный CI нужно стабилизировать; не объявлять весь регресс зелёным без фактов.
### v6.25 — однопостовая проверка действий второго аккаунта (исторический этап)

- `vk_account_smoke.py`: режим `--account 2 --test-post GROUP_POST`, по умолчанию только чтение; `--test-actions like|repost|both --confirm` — максимум один явно выбранный пост за вызов.
- Отбор поста: `api_seen_posts` + `post_publication` в исходной базе №1, **read-only**, только совпадающие ID и точная дата за 7 дней. Прямой URL определяется из ID, браузерное сканирование списка не делается.
- Вторая база **`vk_batch_account_2.sqlite3`** с отдельными `posts`, `action_attempts`, `safety_lock` и таблицей локальной `local_account_identity`. Нельзя выдавать старые статусы №1 за статус №2. Копия существующей БД №1 без identity-binding будет отклонена.
- Перед любым кликом повторно проверяется VK ID текущей Chromium-сессии №2; сравнение с `vk_account_bindings.json`. При несовпадении — STOP. Профили и cookies не переносятся.
- Реакции второго аккаунта можно проверять **только на одном явно указанном посте**; массовые `--run-actions` для №2 остаются заблокированы. Это первый smoke, а не завершённая миграция многопрофильной модели.
- Команда `--account 2 --test-status` читает отдельную историю без подключения к VK. Нажатие на лайк или финальную отправку репоста учитывается перед реальным кликом; при `uncertain` повторного клика нет.
- Добавлены офлайн-тесты изоляции, неверной привязки, отсутствия API-поста, раздельных ledger и отказа при блокировке. На момент подготовки v6.25 живые действия №2 ещё ожидали проверки; последующие успешные тесты описаны в разделе v6.26 ниже.



### v6.26 — независимая числовая очередь №2, реальный тест 30/30 успешно пройден

- После публикации v6.25 пользователь **проверил на реальном VK** второй профиль на посте `188206921_13418`: отдельные `--test-actions like` и `--test-actions repost` дали `[LIKE] подтверждено VK` и `[REPOST] подтверждено VK`. `--test-status` показал `like=verified repost=verified`, 1+1 попытки в отдельной базе. Это был первый однопостовый тест; затем отдельно пройден многопостовый тест ниже.
- v6.26 добавляет `--account 2 --preview`, `--account 2 --run-actions --likes N --reposts M --max-posts K --confirm`, `--account 2 --action-status`. Файл `vk_account_batch.py` копирует из `vk_batch.sqlite3` в `vk_batch_account_2.sqlite3` только метаданные публичных API-постов (точные ID и UTC даты) из 7-дневного окна, читая первую базу **только read-only**; любые статусы и попытки №1 не копируются.
- Применяются прежние `CommandActionBudget`, журнал `command_action_runs`, `action_attempts`, контроль дата=VK API, запрет `uncertain`, остановка при 429/CAPTCHA и повторная сверка личного VK ID **перед каждым финальным кликом**. Отчёт №2 — `vk_actions_results_account_2.csv` (исключён из Git). Lock для №1 и №2 не допускает параллельных запусков.
- По прямому запросу пользователя **2026-10-21 выполнен реальный пакет 30/30 профиля №2**. Команда и проверенные результаты приведены ниже. Не выполнять одновременные задания двух профилей и не обходить реальные ограничения VK.
- Перед реальным запуском прошли 38 офлайн-тестов `tests/` и 7 тестов `test_command_actions_v623`, компиляция исходников. Включён полный CLI-регрессионный тест 5/2 на временных базах: независимые профиль/SQLite/CSV, сохранение прежних реакций и неизменность базы №1. Полный исторический набор в корне не объявляется пройденным: ранее зависал на старых браузерных фикстурах.

#### Реальный тест второго аккаунта 30/30 — подтверждённый результат

```powershell
python vk_unified.py --account 2 --run-actions --likes 30 --reposts 30 --max-posts 60 --confirm
python vk_unified.py --account 2 --action-status
```

- Запуск №1 в `command_action_runs` базы `vk_batch_account_2.sqlite3`: `result=complete`, код завершения процесса 0.
- Начало: **2026-10-21 19:36:09 Asia/Yekaterinburg** (`2026-10-21T14:36:09+00:00`); завершение: **20:07:53** (`2026-10-21T15:07:53+00:00`). Длительность **31 минута 44 секунды**.
- **30 новых лайков и 30 новых репостов подтверждены VK**. В журнале этого запуска ровно 30 попыток каждого вида, по 30 уникальных публикаций на действие, у всех соответствующий статус `verified`.
- Проверено **35 публикаций из разрешённых 60**. Уже существовавшие реакции пропускались и не засчитывались как новые; после 30-го репоста последняя публикация обрабатывалась только для недостающего лайка.
- `--account 2 --action-status` подтвердил `complete` и 30/30 каждого типа. Независимая SQL-проверка `command_action_attempts` → `action_attempts` → `posts` подтвердила те же итоги без дублей.
- Локальный `vk_actions_results_account_2.csv`: **35 строк**, `new_like_in_campaign=да` у 30 строк, `new_repost_in_campaign=да` у 30 строк. Реальный CSV, SQLite и профиль не публиковать в GitHub.
- История попыток первого аккаунта **не изменилась: 71 запись до и после**. На момент завершения сохранённых предупреждений VK и оставшихся `.vk*.lock` не было. Суточный отчёт №2 показывал 62 попытки, включая 2 прежние попытки однопостового теста; это не 62 действия текущего запуска.
- В ходе одного открытия VK показал загрузочные заглушки; после повторной загрузки поста лайк и репост были подтверждены. Не потребовалось снимать ограничения, менять профиль или повторять неопределённый финальный клик.
- Тест подтверждает работоспособность конкретного многопостового запуска №2. Он не доказывает будущую доступность сессии, гарантированную квоту VK или готовность незавершённой общей модели `AccountPostState`.


## 3. Последнее достоверное рабочее состояние

- Следующий диагностический этап 2026-10-21: на реальном VK для обоих аккаунтов проверены видимость `club_share_option`, список и выбор двух доступных сообществ каждого аккаунта; все четыре выбранных значения подтверждены через combobox. Финальная отправка не выполнялась, `action_attempts` остались 71/62. Подробности и локальные назначения — `COMMUNITY_REPOST_PROBE.md`. На том диагностическом этапе CLI ещё не поддерживал сообщества. В текущем локальном v6.27 добавлен явный `--repost-community ID`; штатный опубликованный v6.26 это назначение не поддерживает. При открытии диалога в обоих профилях по умолчанию было выбрано личное сообщение; всегда проверять переключение. Для будущей реализации требуется отдельный учёт по целевому сообществу и подтверждение на его стене.
- Публикуемая v6.26 после `ee47478bce9341af97cd846d482bc2363df5c89b` содержит `vk_account_batch.py`, изменения `vk_unified.py`/`vk_accounts.py`, новый пакетный тест №2 и обновлённые тесты маршрутизации. Остальные исполняемые модули уже совпадали с GitHub. Существующие исторические тесты сохранены; CI запускает 45 профильных тестов отдельно от полного набора. Все 45 и компиляция проходят на локальном Windows/Python 3.14.3/Playwright 1.64.0.
- Полный набор на Windows завершён: v6.26 — 218 тестов с 24 ошибками; прежний `main` — 209 тестов с теми же 24 ошибками по именам. Это ошибки существующих браузерных/кодировочных/SQLite-фикстур; полный набор не считать зелёным. Независимая проверка не выявила блокирующих публикацию P0/P1; два P2 сохранены открытыми в `ROADMAP.md` и `VALIDATION_V6_26.md`.
- До реального теста `--account 2 --check-account` успешно подтвердил сессию №2; обработчик CLI уже присутствовал, изменений рабочего алгоритма не потребовалось. Проверка временной копии тогда нашла 116 публичных записей и 115 кандидатов; это исторический снимок до теста, не актуальный размер очереди после 30/30. Рабочая папка не содержит `.git`; наличие URL GitHub не означает, что локальные изменения уже опубликованы.

- 57 публичных сообществ в конфигурации, окно по умолчанию 7 суток.
- Сервисный ключ VK API функционирует для `wall.get`. **Сам токен пользователем не передавался**, в Git отсутствует.
- Оба аккаунта выполнили отдельные подтверждённые тесты 30 лайков + 30 репостов.
- Вход и раздельность Chromium-сессий двух собственных VK-аккаунтов проверены в браузере.
- База и все журналы на компьютере пользователя, **в GitHub их нет**. Поэтому настоящий счётчик свежих постов, сессий и предупреждений узнать по коду репозитория невозможно: их нужно получать из новых локальных отчётов.
- Второй профиль прошёл реальный многопостовый тест v6.26: 30/30 новых подтверждённых действий, 35 публикаций, `complete`, 31:44. Повторно запускать тест для подтверждения уже известного факта не требуется.

## 4. Файлы и команды

Рабочий комплект: `vk_unified.py`, `vk_api_read.py`, `vk_batch.py`, `vk_groups.py`, `vk_test.py`, `vk_accounts.py`, `vk_groups.json`, `requirements.txt`, tests.

```cmd
py vk_unified.py --account 1 --check-account
py vk_unified.py --account 2 --login-only
py vk_unified.py --account 2 --check-account
py vk_unified.py --accounts-report
py vk_unified.py --discover --only 188206921
py vk_unified.py --api-report
py vk_unified.py --preview --max-posts 20
py vk_unified.py --action-status
py vk_batch.py --safety-report
```

Исторический однопрофильный числовой режим v6.23 (явные попытки, только аккаунт №1):

```cmd
py vk_unified.py --run-actions --likes 30 --reposts 30 --max-posts 60 --confirm
```

Проверенный многопостовый режим второго аккаунта v6.26:

```powershell
python vk_unified.py --account 2 --run-actions --likes 30 --reposts 30 --max-posts 60 --confirm
python vk_unified.py --account 2 --action-status
```

Это описание интерфейса, **не рекомендация** повторять массовые действия: реальная платформа может ограничивать запросы, и искусственную согласованную активность несколькими аккаунтами автоматизировать не следует.

## 5. Сущности, статусы, миграция

- Пост: `(group_id, post_id)`, данные `owner_id`, точная дата, источник (`VK API` или browser-only), прямая ссылка.
- Статус действия: `pending` — ожидание и необходимость проверки; `already` — уже было подтверждено; `verified` — подтверждено VK после действия; `uncertain` — неизвестный итог, повторный клик нельзя; `error`/`unavailable` — явно маркированные причины.
- Доступность: `hidden`, `deleted`, `missing`, `temporary`, `unresolved`; не путать «VK не нарисовал DOM» и «пост действительно скрыт».
- История попыток: до действия в SQLite, с временем, типом, post ID; `verified` считать только после подтверждения VK. Установленный статус `verified` при повторном аудите не откатывать в `pending` без явно обоснованной процедуры восстановления.
- Внешние состояния (`.vk-local-profile*`, `vk_account_bindings.json`, `.sqlite3`, `.env`, CSV) — локальны, не попадать в Git.

## 6. Известные ограничения и долги

1. Физическая изоляция состояния двух аккаунтов отдельными SQLite реализована и проверена реальным тестом №2 30/30. Открыта дальнейшая общая модель `Post`/`AccountPostState` с миграцией истории №1; нельзя направить второй профиль на ту же `posts.like_status`.
2. Тесты старого браузерного движка местами не детерминированы/долго ждут: требуется стабильная изоляция фикстур и CI.
3. `wall.get` может вернуть страницу из 100 постов, для полноты недельного окна важна пагинация и исключение закреплённых старых постов при расчёте cutoff.
4. VK имеет динамические DOM и ошибки частоты запросов. Безопасная реакция: остановка, лог, отсутствие слепого повторного клика.
5. Очередь базируется на данных последнего API-обновления; свежие публикации могут отсутствовать, пока `--discover` не прочитает их. Учитывать TTL/cache и статусы partial.
6. Если исходный CSV открыт в Excel, перезапись может завершиться `PermissionError`; действие в SQLite уже могло сохраниться, поэтому CSV не должен быть источником истины.
7. Нельзя считать факт 60/60 действий доказательством длительной устойчивости при другой нагрузке или другого аккаунта.
8. `vk_account_batch.refresh_public_catalog` переносит подходящие записи, но пока не отзывает ранее импортированную валидность в кеше №2, если источник затем пометил пост невалидным. Проверки точной даты, корня поста, состояния и VK ID перед действиями остаются; требуется отдельное исправление отзыва валидности без изменения истории реакций.
9. Пакетный обработчик №2 останавливается при сообщении `VK запросил вход/дополнительную проверку`, но это сообщение не попадает в его классификатор сохранения блокировки. Следующий ручной запуск не блокируется этой записью SQLite. Для буквального сообщения CAPTCHA блокировка сохраняется; классификаторы необходимо согласовать отдельным патчем.

## 7. Как продолжать разработку в следующем чате

1. Прочитать `AGENTS.md`, `README.md`, **этот** `docs/DEVELOPMENT_CONTEXT.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`.
2. В каноническом [BorisDruzak/vk_bot](https://github.com/BorisDruzak/vk_bot) проверить актуальные `main`, SHA, открытые Issues/PR и фактические отличия кода. Не полагаться исключительно на пересказ в истории чатов; локальная папка может быть копией без `.git`.
3. При изменениях подготовить ветку/PR либо согласованное обновление `main`; после успешных тестов записать результат и SHA. Никогда не публиковать сессии/ключи/SQLite/авторизованный DOM.
4. Если задача касается реального окружения, попросить только **безопасные текстовые строки диагностического отчёта**, а не файлы Chromium, cookies или сервисный ключ.
5. Продолжать по открытым задачам `docs/ROADMAP.md`: общая модель `AccountPostState`, миграция истории №1 и стабилизация исторических тестов. Многопостовый тест №2 30/30 уже закрыт; новые реальные действия требуют отдельной команды пользователя.
6. Обновлять этот документ после каждой вехи, чтобы он оставался self-contained.

## 8. Что запрещено ошибочно утверждать

- Что многопостовый тест второго аккаунта всё ещё ожидается: **нет**, 2026-10-21 он завершён — 30 лайков и 30 репостов подтверждены VK, 35 публикаций, `complete`.
- Что API автоматически проставляет лайки: **нет**, API используется для чтения; клики ранее выполнялись через авторизованный Chromium.
- Что v6.24 прошла полный набор тестов: **нет**, только выборочные.
- Что отсутствие `[SAFETY]` предупреждений означает гарантию отсутствия ограничений: **нет**.
- Что `pending` означает отсутствие лайка: **нет**, состояние может ещё не перепроверяться в VK.
- Что GitHub хранит рабочую SQLite или состояние авторизации: **нет**, они исключены.

---

**Краткое резюме для нового чата:** текущий локальный VK Unified v6.28 — ежедневная автоматика в рабочем режиме с 09:00 YEKT 2026-10-22, фактический старт ожидается; см. текущий раздел выше. Исторический этап v6.27 с `--repost-community ID` для обоих аккаунтов; опубликованная основа v6.26. Новый режим проверен: 87 профильных тестов, четыре защищённых CLI dry-run и четыре реальные публикации (по две с аккаунта), все `verified` / `complete`, с обязательным `time.sleep(20)` после отправки. Codex Windows app/PowerShell, Python/Playwright/SQLite/VK API, 57 публичных групп, окно 7 дней. Канонический GitHub — [BorisDruzak/vk_bot](https://github.com/BorisDruzak/vk_bot), правила — `AGENTS.md`. Оба аккаунта отдельно прошли реальные тесты 30/30. **Второй аккаунт: 30 новых лайков и 30 новых репостов подтверждены VK, 35 публикаций, `complete`, 2026-10-21 19:36:09–20:07:53 Asia/Yekaterinburg, 31:44**. Его SQLite/CSV независимы; история №1 не изменилась. Перед историческим запуском 30/30 прошли 45 профильных офлайн-тестов; полный исторический регресс и общая модель `AccountPostState` остаются открытыми. Сохранять точный корневой DOM, post ID, отсутствие дублей, стоп при 429/CAPTCHA/неопределённом результате, авторизацию вне Git. Актуальную публикацию сверять с `main`; наличие локальных файлов не доказывает их синхронизацию с GitHub.
