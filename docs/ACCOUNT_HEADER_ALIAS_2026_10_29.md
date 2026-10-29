# Точный вход по буквенному адресу собственного профиля

НаVM новый аккаунт уже вошёл вVK, но оставалсяawaiting_login: единственная
ссылкаa[data-testid=header-profile-menu-button] содержала буквенный адрес,
а прежний инспектор принимал только/idNNN. Переход на собственную страницу
не дал числовыхcanonical/og:url; заголовок остался тем же. Привязку по имени,
скриншоту или произвольному числовому посту не делаем.

## Контракт и доказательство

Явная prepare_context_identity вызывается вlogin и один раз при первой
проверке источника ДОintent. Только собственная единственная ссылка шапки
наHTTPSразрешённомVKхосте, один допустимыйASCIIalias, безquery/fragment,
credentials/port/redirect и маршрутовfeed/menu/account/profile и т.п.
Набор собственных заголовков всех вкладок должен быть согласован.

Официальныйusers.get(v5.199,user_ids=alias,fields=screen_name) возвращает ровно
одного активного пользователя с точным положительнымid и ASCII screen_name,
совпадающим сalias без учёта регистра; empty/multiple/malformed/bool/mismatch
иdeactivated не принимаются. Это пользовательский endpoint, не resolverгрупп.
Контракт: [официальная схемаVK](https://github.com/VKCOM/vk-api-schema/blob/master/users/methods.json).
Шапки перечитываются послеpacing/guards перед резервированиемHTTP и после
ответа. Любая смена/неоднозначность/небезопаснаяVK-вкладка отменяетproof.
Чужой числовойID в другой вкладке не игнорируется из-за еёHTTP/port URL.

Связьalias→ID хранится только втомжеBrowserContext, WeakKeyDictionary,
не дольше300с monotonic. Исчезновение/смена заголовка отзываетproof;
возврат прежней ссылки не восстанавливает его автоматически. Другой
BrowserContext требует новой подготовки. В файлах/общем кешеalias не сохраняется.
Обычныеinspect_header_identity/inspect_context выполняют толькоDOM/память,
безHTTP; каждое повторное действие/клик сохраняет свежую проверку шапки.
Прямой числовойзаголовок остаётся прежним быстрым путём безAPI.

## Общий бюджет и сроки

Отдельный методVKReadClient.resolve_screen_name использует существующий
общийledgerapi_read_attempts вvk_batch.sqlite3 и primarylease, pacing>=5сек,
5000/rolling24ч. Записьgroup_id0 — служебное чтение, безalias/IDпользователя.
Ответ не кладётся подwall.get и не меняетPublicPages/счётчики/actorhistory.
Внутреннийключ_read_method разрешает толькоwall.get/users.get и
удаляется передPOST; произвольныйURL/метод запрещён. 1051 даёт отказ авторизациипосле одного запроса, безfallback. Ключ остаётся в локальном
файле/HTTPSPOST/stdin boundedhelper; параметры/адреса/токены не логируются.
Rate/429/CAPTCHA/protection latch сохраняется; метод сам запрос не повторяет.

Scheduledподготовка получаетDintent=B−60сек. ПередHTTP нужен запас20сек,
он проверяется передpacing, после ожидания/guard/чтенияшапки и доreserve.
Самtransport ограничен20сек общим сроком. Не вместившееся чтение не тратит
запрос и не разрешает позднийintent. Финальныеbefore_attempt/before_click
проверки времени, квот, аккаунта и защит сохраняются.

IdentityPreparationDeadline — нормальный отказ подготовки только когда у
текущегоpass нетintent/attempt: batch_action_deadline безновойквоты/паузы.
Тот жеexception послеintent сохраняетuncertain/attention. Confirmation-only
recovery уже существующей попытки можетчитать при нулевойквоте; новыйклик
по-прежнему требуетbefore_attempt и положительного остатка. Читающийguard
сохраняетowner/изоляцию/включение/stop/paused/day/window проверки.

## Проверки и установка

Офлайнпроверки включаютURI/вкладки/смену/TTL/context, APIbudget/pacing/latches,
реальныйboundedchild, namespaceisolation, confirmationбезновойквоты,
deadlineдоintent/послеintent. Полный localdiscovery1087PASS/0ошибок/312.555сек,
Python3.14.3/Windows;39целевыхAPI/alias/integrationPASS. Независимое ревью после
исправленияHTTP-VKcross-tab иquota-confirmation замечаний блокеров не нашло.
58регрессийrunner/recovery такжеPASS; отсутствующий savedboundary остаётсяNone,
сохранённыйслот получаетB−60. ТочныеSHA/CI/VMрезультаты дополняются по факту.
Само включение новогоactor требует свежегоexactmatched наVM вего физическом
профиле и интерактивномWindowsпользователе; старыеbindings/история неизменны.
Текущая установленнаяполитика67/390 и личные репосты0 описаны отдельно в
[FIVE_ACCOUNTS_67_BATCHES_2026_10_29.md](FIVE_ACCOUNTS_67_BATCHES_2026_10_29.md).

## Проверка доступности метода наVM

Промежуточный227f3da сutils.resolveScreenName прошёл1086local/VMтестов
иCI, но НЕ установлен: одиночная stagedread-onlyпроверка получила1051.
Registry/bindings/actorhistory сохранены, исходникиruntime не менялись,
аккаунт не включён. ЛокальныйAPIконтракт и фактическая доступность с
конкретным типомключа — разные gates.

Выбран users.get, официально принимающийscreen_name. Это единственный
производственный методподготовкиalias; цепочкиавтоперебора методов/ключей нет.
39целевыхAPI/alias/integration проверок наVM PASS;107API/transport/catalog
регрессий локальноPASS. Отдельная stagedпроба сначала классифицирует1051
по точному фиксированному сообщению, затем проверяетusers.get на томже
ключе/свежейшапке, с общимledger/pacing и безизмененияпривязок. Полные
результаты/SHA/CI/живаяприёмка нового варианта дополняются пофакту.
