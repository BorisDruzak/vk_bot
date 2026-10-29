# Резерв без задержки следующей пачки —24октября2026

Проблема:18:10 первый полный обход резерва обнаружил новый пост; после трёх
лайков прежняя версия повторно читала57групп. Пачка завершилась18:22:56,
пропустив запуск18:19:45 и его120сек допуска. Исторический пропуск не догонять.

Решение: если исходный полный reserve-обход уже дал свежие наблюдения обоих
слоёв, сохранять оба в pool и выбирать сегодняшние первыми. Это один refresh,
observed_at>=его точного начала; повторного прохода этих групп не нужно.
Если исходно читался только новый слой, один отдельный reserve refresh остаётся,
но получает deadline от следующего сохранённого slot (либо конца окна) минус
120сек. Перед каждым запросом, включая после pacing, нужен запас20сек под
существующий HTTP timeout. Неполный проход по deadline закрывает остаток;
не использовать ни прошлый, ни частичный pool. Проверять deadline заново
после refresh/выбора, до создания нового job.

План: TDD точного сценария57групп/3fresh+1reserve и следующего claim;
дедлайн до/после pacing, budget/429/stop/NULL/day/window/retry регрессии;
минимальные изменения Worker/refresh/явные partial подписи; полный unittest
в чистых копиях локально и VM; независимое ревью; backup и штатная установка.

Прежние4попытки/квоты/540сек/sleep20/API5сек/5000/24ч/09–19, guards,
uncertain/retry, day plan и аналитические frozen snapshots сохраняются.
Это ограничение дополнительного чтения резерва; существующие действия нельзя
прерывать убийством процесса ради следующего slot. Любая отправка/подтверждение
по-прежнему проходит собственные guards и штатную паузу.

Ограничена и исходная подготовка счётчиков. `bounded_transport` запускает
только существующий публичный HTTP transport в отдельном helper с общим20сек
timeout (включая чтение медленного ответа), завершает и дожидается только этого
helper. Ключ передаётся stdin, в argv/log/env его нет. Helper не открывает SQLite
или браузер; ledger/pacing/guards/PublicPages остались у VKReadClient.429 и VK
rate codes по-прежнему вызывают защиту. Сетевой timeout до cutoff остаётся
VkApiTransientRead с backoff и остановкой после трёх сбоев, не обычным пропуском.
Основание lifecycle: официальная [документацияPython3.14](https://docs.python.org/3.14/library/subprocess.html#subprocess.run).
Context7 был проверен, но вернул исчерпание месячной квоты.

Повтор после cutoff остаётся durable pending; полностью выполненные4/4
сохраняют batch_complete даже если последний cooldown закончился позже cutoff.
Тесты используют точный сохранённый план и искусственное время, отдельно
проверяют57групп,45сек на действие и следующий штатный claim без catch-up.

## Проверка

- TDD воспроизвёл114запросов вместо57 и длительность12мин20сек; исправленная
  ветка использует57запросов, выполняет3fresh+1reserve за7мин40сек и успешно
  исполняет следующий сохранённый slot через585сек. Между первыми реальными
  попытками двух пачек>=540сек, все4попытки/пачка и паузы>=20сек сохранены.
- Deadline до HTTP, после pacing, при исходном old-обходе и после медленного
  pick; частичные данные не разрешают действий. NULL/429/stop/window/day/quota,
  unknown source и retry проверки сохранены.
- Четыре теста с настоящими offline child процессами: общий timeout,
  fixed-error sanitization, rate limit propagation, ключ только stdin
  (VK_API_TOKEN/VK_API_TOKEN_FILE удалены из наследуемой среды).
- Дополнительно: pending без второго intent после cutoff, partial по общему
  batch attempted, completed4/4 после cooldown, технический timeout с read
  failures1/2/3 и сохранённой third-failure attention.
- Полный локальный source-only unittest:607тестов OK; окончательные30целевых
  регрессий OK. Полный VM source-only:607тестов OK,13Node fixtures skipped
  (они пройдены локально). На VM нет registry/bindings/SQLite/профилей/ключей
  в тестовом каталоге. Независимое ревью завершено, открытых замечаний нет.

## Установка

24октября22:11YEKT, после закрытия рабочего окна: штатный scheduler-stop
только для установки, backup, атомарная замена проверенных файлов и запуск
прежнейVKUnifiedDailyWorker. Изменены6runtime файлов: vk_scheduler.py,
vk_batch_engagement.py, vk_api_read.py, vk_action_retry.py, vk_diagnostics.py,
web/app.js; vk_scheduler_state.py совпадает с предыдущей установленной версией.
Панель не перезапускалась, HTTP app.js совпадает с проверенным файлом.

Backup: C:/Users/vk-bot/vk_backups/reserve-deadline-20261024T171131Z.
Все67protected table/file/meta hashes совпали до/после и после запуска:
истории, jobs/attempts/retry/изоляции, dayplans, API ledger/pacing/catalog
progress, registry/bindings/config и last_action_batch сохранены. Рядом
install-audit.json/post-audit.json. Никаких новых ручных действий или catch-up.

На22:12:30YEKT worker10480 с launcher8228: live/waiting_window, paused=null,
stop_requested=false, inflight0; heartbeat1.19сек. Задачиworker/webRunning,
panel9376/7628 прежние. Следующее окно25октября09:00YEKT. Рабочий дневной
сценарий после этой установки ещё не наблюдался; смешанный переход/следующая
пачка доказаны офлайн. GitHub не публиковался; локальная папка без.git.
