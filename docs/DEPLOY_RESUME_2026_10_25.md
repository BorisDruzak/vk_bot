# Установка, безопасное возобновление и доступ к VM — 25 октября

Пользователь вновь разрешил доступ к VM, установку и снятие только связанной
паузы с сохранением лимитов/истории и без catch-up. Дополнительно попросил
проверить блокировку SSH, установить QEMU Guest Agent через указанный Proxmox
и подтвердить новые реальные действия VK.

## Исправления и установка

Локальный source-only unittest: 620 тестов, OK. На VM из отдельного каталога
без реестра/bindings/SQLite/профилей/секретов: 620 тестов за192.992сек,
OK(skipped=13): Node fixtures отсутствуют наVM, но прошли локально.
Исправленные runtime модули: vk_scheduler.py, vk_scheduler_state.py,
vk_action_retry.py, vk_community_ui.py. Изменения описаны в
BATCH_WINDOWS_2026_10_25.md; полный резерв/свежие счётчики остаются обязательными.

Установка15:47:56YEKT: штатный stop только для замены кода; задача временно
отключена от триггеров на время backup/swap, затем включена и запущена с
прежней защитной паузой. Это не остановка ради проверки. Все SQLite сохранены
через backup API с integrity_check=ok. Backup:
`C:/Users/vk-bot/vk_backups/batch-window-20261025T104756Z`.
Все67 защищённых хешей таблиц/планов/истории/расхода/meta/config до/после
совпали; четыре runtime hash совпали с проверенной копией. Панель не
перезапускалась. GitHub не публиковался; локальный каталог без.git.

Установленные SHA256:

| Модуль | SHA256 |
| --- | --- |
| vk_scheduler.py | a5704c3708a77d795cdc4ba7e67f3d26e4c61396ac372032b86799fd2dc625d8 |
| vk_scheduler_state.py | 49167896188525ddecd3069bd32e476ec2a6448c38c8c22148b294ec83e79643 |
| vk_action_retry.py | ec2968cdddaa692e62e025e5b8cba0b7c89d3d2130edeb768b2679ddde54b1e0 |
| vk_community_ui.py | 92811e276db34b46524f22a4e0ec9bcdfb105b613ad7270b12f34b7ab3295ebe |

## Узкое снятие паузы

Свежий read-only proof15:48:55–15:49:23YEKT: точный job450, собственный аккаунт,
источник/дата/назначение подтверждены, финальная кнопка видима/enabled,
blocked_clicks=0, history hashes неизменны. Реальных отправок/резерваций нет.
Доказательство хранится приватно:
`C:/Users/vk-bot/vk_backups/resume-20261025-job450/result.json`.

В15:51:25YEKT под exclusive activity lease и BEGIN IMMEDIATE повторно проверены
точный paused/diagnostic450, failed job безattempt, pass1intent0/interrupted,
отсутствие in-flight/unfinished batches/active uncertain и platform locks,
свежий live heartbeat и возраст proof122сек. Снят только
paused=action_stopped_community_prepare. Job450, passes, попытки, квоты,
last_action_batch, изоляции и планы не изменены. Приватный unpause-audit.json
содержит прежнюю паузу, прямое разрешение, хеш proof и защищённых записей.
Durable resume_not_before установлен на уже сохранённый будущий slot16:00YEKT.
Пропущенные слоты не claimed и не догоняются. Worker6964 live/waiting_batch,
pausednull, active_uncertain0; это пока не доказательство новых действий.

## SSH и блокировка пользователя

Событие4740 в14:55:17YEKT подтверждает блокировку интерактивного vk-bot.
Десять предшествующих4625 за14:48:11–14:55:17 имеют процесс Playwright
chrome.exe, LogonType2 и неверный пароль0xc000006a. Это локальные Windows-входы,
не неудачная авторизация SSH. Точная внутренняя причина попыток Chromium
не установлена. После15:08 и до проверки15:55 новых4625/4740 нет.
Отдельный разрыв SSH около15:07 совпал с перезапуском Windows: System1074,
6006/6005 и остановка/новый listen sshd. Эта работа VM не перезагружала.

Для независимого обслуживания создан технический vk-maint с отдельным
локальным ключом, случайным невыводимым паролем и входом SSH только publickey.
Ключ ограничен адресом текущего оператора; парольная авторизация, PTY,
agent/port forwarding отключены для этого пользователя. ACL файла ключей —
только SYSTEM/Administrators. Прежний интерактивный аккаунт и его профиль
не заменены. Политика Windows10неуспехов/10минут блокировки/10минут окна
не ослаблена. Публичный ключ не является доказательством живого входа:
выполнены отдельные успешные подключения и сверка effective sshd -T.

Операторские SSH aliases на рабочем компьютере: `vk-bot-vm`, `vk-maint-vm`.
Один проверенный ключ, BatchMode/IdentitiesOnly, без password fallback,
ConnectTimeout10, ConnectionAttempts1, keepalive20/3. Прежний config сохранён.
Server config backup:
`C:/ProgramData/vk-maintenance/ssh-20261025/sshd_config.before`.
Перед применением sshd -t прошёл; служба перезапущена через QGA и проверена.
Никакие неясные операции VK при обрыве SSH автоматически не повторяются.
Проверки VK identity по-прежнему исполняются в интерактивном контексте
vk-bot. Прочитанный техническим пользователем authentication snapshot может
быть unverified из-за machine/user binding; это не live proof профиля VK.

## QEMU Guest Agent

Указанный Proxmox: VM125 с именем vk-bot, agent=1 и подключённым VirtIO ISO.
До установки qm agent ping: not running; службы QEMU-GA не было.
Установлен MSI из уже подключённого virtio-win-0.1.285, vioserial driver
oem15.inf уже актуален. MSI в этом ISO без Authenticode; это явно зафиксировано,
проверка подписи не объявляется успешной. SHA256:
C50EA2E7C04730A1097AB6C112138645BE4DA26015518329DAEBE8D3630E0790.
Первый запуск MSI с прямым D:/ путём вернул2 до установки; локальная копия
с нормальным Windows-путём успешно установлена, MSI exit0, /norestart.
Служба QEMU-GA Running/Auto. PID worker/panel при установке не изменились.
Аудит: C:/Users/vk-bot/vk_backups/qga-20261025/audit.json.

Через Proxmox успешно выполнены qm agent125ping, get-osinfo, guest exec
(QEMU-GA/sshd Running) и network-get-interfaces с ожидаемым IP VM.
Перезагрузка/выключение/заморозка файловых систем не выполнялись.

## Проверка штатной пачки после возобновления

Slot42 стартовал16:00:03.701590 и завершился16:05:30.393176YEKT,
status=batch_complete,4новые реальные попытки и4verified. Все три аккаунта
получили подтверждённые действия:

| Job | Actor | Действие | Attempt | Подтверждение |
| --- | --- | --- | --- | --- |
| 451 | 5 | community | 95 | community ledger verified, target_post_id15 |
| 452 | 2 | community | 237 | community ledger verified, target_post_id26 |
| 453 | 5 | community | 96 | community ledger verified |
| 454 | 1 | personal | 244 | posts.repost_status verified |

Все четыре действия относятся к первой фазе. По реальным stage events четыре
cooldown составили20.006219/20.006467/20.008389/20.006378сек. Priority evidence
содержит свежие known likes/reposts/comments, observed_at16:00:17.859398 после
точного refresh_started_at16:00:03.753203; свои verified последовательно
добавлялись к API reposts2→3→4→5 перед следующими выборами. Эта пачка исчерпала
четыре места на сегодняшней публикации; живой reserve fallback здесь не
потребовался, его deadline/полнота проверены офлайн.

API ledger:10новых чтений с16:00, min gap6.0сек, rolling24h1819/5000 при
проверке16:06. Публичные чтения сохраняют общий ledger. Дневные попытки после
пачки: actor1=9like/5personal/7community, actor2=9/4/8, actor5=8/4/8;
все лимиты50/10/20 и два community назначения по10 соблюдены. Last_action_batch
сохранён, прежние фактические начала были более540сек назад.

Сверка с backup подтвердила неизменность всех старых scheduler_jobs<=450,
всех прежних scheduler_batches, schedule_days и prefix action_attempts каждого
аккаунта. Пропущенные slots19–41 не claimed, единственная новая пачка42.
Job450failed не переоткрывался; target personal в454 — отдельное разрешённое
действие, не повтор failed community450. Safety locks пусты, active_uncertain0,
pausednull, failure streak0. Последняя ошибка остаётся исторической450, новых
action_failed после установки нет. Следующий сохранённый slot16:10YEKT.

Приватные локальные evidence: C:/Temp/vk-window-acceptance.json,
vk-window-acceptance-summary.json, vk-window-events.json, vk-window-live.json.
Приёмка одной пачки не является приёмкой полного оставшегося дня.

Итоговая проверка16:08:38YEKT: worker6964/monitoring/live, задача Running/Enabled
под исходным vk-bot; panel Running; sshd/QEMU-GA Running. pausednull,
active_uncertain0, runtime hashes всё ещё совпадают с tested stage. Временная
задача VKResumeProof450-20261025 завершилась с0 и удалена после проверки.
