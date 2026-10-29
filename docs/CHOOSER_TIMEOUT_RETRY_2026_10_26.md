# Повтор начального тайм-аута диалога сообщества —26октября

Прямое «делай» после проверки631 и объяснения обхода счётчика3.
Исправляется только классификация известного30сек ожидания первой опции
«В сообществе» до intent/попытки; прочие ошибки подготовки не разрешаются.

## Реализация

- vk_community_ui.CommunityPreparationTimeout — отдельный TimeoutError с
  фиксированными stage=community_prepare/reason_code=community_prepare_timeout.
- _wait сохраняет прежний deadline/poll/ensure_ok, но только первый chooser
  использует этот тип. Перед его возвратом prepare заново сверяет identity и
  точный source, включая состояние под модальным окном. Ошибка любой такой
  проверки сохраняет обычную защитную остановку.
- Scheduled executor передаёт этот тип без упаковки в общий StopRun.
  Scheduler/diagnostics сохраняют фиксированную причину без DOM/сообщений.
- Retry принимает только точный тип/этап/код для community job в dispatching
  или failed, с attempt_idNULL; дополнительно проверены pass.intent=0 и
  pass.attempt_idNULL. Производный тип, другая фаза, другой вид действия,
  обычный StopRun/защита в цепочке и cleanup не становятся разрешёнными.
- SAFE_REASONS содержит новый конкретный код; общий
  action_stopped_community_prepare по-прежнему не retryable.
- Новый тип не попадает в legacy Worker.defer_technical: при отключённой
  policy повторов сохраняется немедленный attention, без новой автоматической
  ветки backoff/продолжения. Это отдельно воспроизведено RED и исправлено.

Применяется существующая policy1: максимум один повтор логического действия;
после двух неудачных подготовок — held и переход к другим действиям; третий
неуспешный логический action подряд — action_failures_repeated/attention.
verified/already сбрасывает streak. Три неудачи здесь означают не три отправки
одного репоста. Реальные попытки обеих фаз сохраняют общие квоты и<=4места.

Если времени/окна на повтор уже нет, остаётся pending для следующей штатной
пачки того же дня с новым полным refresh. Прежние restart/expiry/guards
сохраняются. Подтверждение репоста после durable intent не заменяется отправкой.
540сек/09–19/sleep20/API5сек/5000 и неизменный dayplan/no-catchup прежние.

## Проверки

TDD RED до изменений: нового исключения/классификации нет. Затем85целевых
тестов —OK, включая настоящий Chromium с локальными HTML и блокировкой сети.
Проверены: typedwait и guard первым, повтор/следующийaction,6подготовок для
3неуспешных действий без попыток, cutoff/pending/restart, скрытый intent,
последующая попытка/другой kind/stage/subclass, вложенная защита, ошибочные
identity/source после ожидания и передача типа через настоящий executor.
Ни проверочных VK действий, ни рабочего профиля в этих тестах нет.

Первый полный local653/OK иVM653/OK(skip13Node) подтвердил основные ветки.
После дополнительного opt-in regression итоговый local654/OK за140.038сек,
VM654/OK(skip13Node) за200.943сек. Набор содержит только исходники,
tests/web/public vk_groups.json, без accountregistry/bindings/SQLite/secrets.
GitNexus не индексирует vk_bot; сравнение с сохранёнными исходниками и VM SHA.
Context7 оказался недоступен из-за месячной квоты; fallback — официальная
[документация Locator](https://playwright.dev/python/docs/api/class-locator#locator-is-visible).
Playwright1.64.x/закреплённые требования проекта сохраняются; новых библиотек нет.

Независимое ревью четырёх runtime diff, тестов, установщика и opt-in guard:
блокеров нет; повторный независимый запуск11ChooserRetryTests —PASS.
Ни VM, ни файлы reviewer не менял.

## Установка и историческая пауза

Установлено26октября19:28:59YEKT: четыре runtimeмодуля
vk_community_ui.py,vk_action_retry.py,vk_scheduler.py,vk_diagnostics.py.
Source-only проверки, исходники до замены и SQLite online backup с
integrity_check=ok сохранены в:
`C:/Users/vk-bot/vk_backups/chooser-retry-20261026T141817Z`.
Для установки выполнен штатный stop при отсутствии in-flight работы;
hard kill/перерегистрации задачи не было. Определение Windows-задачи совпало,
194защищённых сверки таблиц/остальныхисходников/JSON неизменны при swap/restart,
включая vk_groups.json. Heartbeat/stop_requested и диагностические события
штатного перезапуска исключены из утверждения неизменности.

Новый worker5444/live/attention, текущая paused=action_stopped_community_prepare,
job631failed/intent0/interrupted, попытки/изоляции/квоты/планы/64источника
и прочие настройки сохранены.
Это исправление не мигрирует старый StopRun в новый typed timeout задним
числом и не переоткрывает631. Автоматического снятия текущей паузы нет.

Установленный код отдельно проверен19:29:29 без VK/браузера/actorwrites:
synthetic typed timeout retryable=True, generic legacy guard=False,
послеattempt=False, новый тип не входит в legacy defer_technical;
diagnostic reason/type фиксированы. Это не живой тест будущего тайм-аута
или новой отправки. Текущая защита остаётся до отдельного разрешённого разбора.

Evidence локально: outputs/chooser-retry-2026-10-26/baseline.json,
targeted.log и rollout.json. Runtime изменения установлены наVM и совпали
с проверенными SHA; публикация GitHub не выполнялась, локальный каталог без.git.
