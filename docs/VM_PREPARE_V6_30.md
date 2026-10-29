# Подготовка Windows VM — v6.30

Дата: 2026-10-23, Asia/Yekaterinburg. Это отчёт о подготовке инфраструктуры, а не о приёмке действий VK.

## Подготовленная среда

- Windows 11, build 26100; рабочий пользователь `vk-bot`.
- Часовой пояс изменён на `Ekaterinburg Standard Time` (UTC+5), чтобы ежедневный Windows-триггер 08:55 соответствовал расписанию проекта.
- Python 3.13.17 x64: `C:\Users\vk-bot\AppData\Local\Programs\Python\Python313\python.exe`.
- Google Chrome 156.0.8078.48: `C:\Program Files\Google\Chrome\Application\chrome.exe`.
- Корень проекта: `C:\Users\vk-bot\vk_bot`.
- Окружение: `C:\Users\vk-bot\vk_bot\.venv\Scripts\python.exe`; `pythonw.exe` находится рядом.
- Зависимости из `requirements.txt`: Playwright 1.64.1, pyee 14.0.0, greenlet 3.5.6, typing-extensions 4.16.0. `pip check`: `No broken requirements found`.
- Playwright Chromium build 1248 / Chrome for Testing 156.0.8078.4 установлен в пользовательский `%LOCALAPPDATA%\ms-playwright`. Текущий код worker использует этот браузер по умолчанию; установленный Google Chrome проверен отдельно.

Python и Chrome скачаны из официальных источников. Перед запуском установщиков проверена действующая Authenticode-подпись Python Software Foundation / Google LLC. Установка завершилась без перезагрузки.

## Интерактивная проверка

Временная задача с `InteractiveToken` / `RunLevel Limited` запустила Python под `vk-bot` в существующей RDP-сессии 2. Проверены два независимых запуска `launch_persistent_context(headless=False)` с новыми временными профилями: канал `chrome` и стандартный Chromium Playwright.

Оба запуска вернули `ok=true`, открыли `about:blank`, показали нейтральную страницу с точным заголовком и проверенным текстом. Зафиксированы версии 156.0.8078.48 и 156.0.8078.4. Дополнительная проверка процессов подтвердила `SessionId=2` у Python и Chrome. Временная задача удалена; закрывались только созданные тестом контексты. Пользовательские окна и профили VK не использовались.

Локальное доказательство на VM: `%TEMP%\vk-headed-smoke-698706bebfb14b73a53e642c2b186121\result.json`. В нём только PID/пользователь/сессия/версии/нейтральный заголовок/результат. Это временный файл, не долговременный источник приёмки.

## Повторяемые скрипты

Выполнять в PowerShell на целевой VM под рабочим пользователем:

```powershell
# После размещения requirements.txt в корне проекта:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\vm\install-vm-runtime.ps1

# Проверка только нейтральных страниц; не запускает worker:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\vm\test-vm-headed.ps1

# После размещения vk_unified.py; только регистрация отключённой задачи:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\vm\register-vm-worker.ps1
```

`register-vm-worker.ps1` требует файлы проекта и venv, отказывается заменять существующую задачу и регистрирует `VKUnifiedDailyWorker` **отключённой**, без вызова запуска. Действие задачи использует полный путь `.venv\Scripts\pythonw.exe`, рабочий каталог проекта и `--scheduler-daemon --confirm`; триггеры — вход пользователя и ежедневно 08:55. Пароли в задачу не записываются. Включение и запуск относятся к отдельному контролируемому переключению после переноса данных и проверки отсутствия второго worker.

После размещения исходников скрипт регистрации выполнен на VM. Проверены фактические `State=Disabled`, XML `Enabled=false`, `LogonType=InteractiveToken`, `RunLevel=Limited`, точные Execute/Arguments/WorkingDirectory. Суточный триггер имеет `2026-10-23T08:55:00+05:00`; второй триггер — вход рабочего пользователя. `LastTaskResult=267011` соответствует задаче, которая ещё ни разу не запускалась.

Существующий `scripts/install_vk_scheduler.ps1` для этапа подготовки не применялся: он автоматически запускает worker. Локальный worker подготовкой VM не останавливался и не изменялся. Последующее переключение выполняется отдельно основным процессом переноса.

## Офлайн-регрессия на VM

После размещения исходников выполнено из корня проекта:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
```

Результат: **301 тест за 122.418 секунды, OK, exit code 0**. Журнал VM: `C:\Temp\vk-tests-v630.log`. Вывод `[VK EVENT]` и пометки `live` внутри этих сценариев относятся к подставным объектам/данным тестов и не доказывают реальных действий VK. Windows PowerShell 5 добавляет в перенаправленный журнал обёртку `NativeCommandError` для stderr, куда unittest пишет итог; итог `OK` и код процесса 0 проверены отдельно.

## Границы

Подготовка инфраструктуры не включает копирование профилей, SQLite, API-ключей или истории действий, вход в VK, запуск worker или действия VK. Такие операции относятся к отдельному основному этапу переноса. Подготовка не доказывает переносимость авторизации Chromium между Windows-машинами, корректность перенесённой истории, работу после разрыва RDP, перезагрузки или полного выхода пользователя. `InteractiveToken` требует существующей пользовательской сессии; служебный Session 0 для headed worker не подходит. Включение автологина подготовкой инфраструктуры не выполнялось. Состояния задачи выше зафиксированы до последующего переключения.

Диапазон `playwright>=1.55,<2` не фиксирует будущую версию; здесь проверена именно 1.64.1. После обновления Playwright требуется повторная установка совместимого браузера и проверка окружения.

Официальные источники: [Python 3.13.17](https://www.python.org/downloads/release/python-31317/), [Windows installer options](https://docs.python.org/3.13/using/windows.html), [Chrome Windows deployment](https://support.google.com/chrome/a/answer/3115278?hl=en), [Playwright browser channels and installation](https://playwright.dev/python/docs/browsers).
