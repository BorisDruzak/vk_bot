# Проверка публикации VK Unified v6.26

Дата: 2026-10-21. Базовый GitHub `main`: `ee47478bce9341af97cd846d482bc2363df5c89b` (документация поверх исходников v6.25).

## Совпадение с проверенной реализацией

Публикуются исходники локальной v6.26, на которой второй аккаунт выполнил реальный тест: 30 новых лайков и 30 новых репостов подтверждены VK, 35 публикаций, `complete`, 31:44. Исполняемый код при подготовке публикации не изменялся. Сессии, SQLite, реальный CSV, токены и локальные VK ID в публикацию не включены.

Все восемь модулей ниже сверены с исходниками рабочей папки. SHA-256 вычислен по UTF-8 тексту с переводами строк LF, как в Git; различия CRLF/LF не считаются изменением кода. Оставшиеся `requirements.txt` и `vk_groups.json` также совпадают с рабочей папкой.

| Модуль | SHA-256 (UTF-8, LF) |
| --- | --- |
| `vk_unified.py` | `7fc916d7cfda7beb6eec1d0bec1327ef26857fc32401cd42aadb190dfe39cfc1` |
| `vk_accounts.py` | `357387a87d801a9ad466b5995e23b545ccc8c48597f38ff769b641e10efd4d86` |
| `vk_account_batch.py` | `9f3ab8b93ca790a63116b1ea849f86c7b62c570d89f09ca299b3395930fd406c` |
| `vk_account_smoke.py` | `fd3467def7bfa041308c37c3397522c868d46a8725e70a51f30aad7423df3d34` |
| `vk_api_read.py` | `ec7ab811d628626fbf28836a8a459d9080c619da4392a750858e601b8d0eab44` |
| `vk_batch.py` | `10d0cb4e5c6702bad9a241c2e3a2d029601b4afe976908424189093c2dc74bef` |
| `vk_groups.py` | `f5d1de92ed22c7b74f2f1037c274c4b67060580638db67099a2cd62601f62adf` |
| `vk_test.py` | `37bce85ea0dd5e269651f2d2e1336f8d7c922be30c88d0a297719a3fd2c0f9d6` |

## Локальные проверки

Среда публикационной проверки: Windows, Python 3.14.3, Playwright 1.64.0.

- Компиляция восьми модулей и `tests/`: PASS.
- Профильный набор: **45 PASS** — 15 тестов профилей, 14 однопостового режима №2, 9 пакетного режима №2 и 7 числового планировщика. Внешние браузерные действия подменены, базы временные.
- Полный исторический набор v6.26: **218 тестов, 24 ошибки**.
- Контрольный запуск прежнего `main` в отдельной временной копии: **209 тестов, те же 24 ошибки по именам**. Новая версия добавляет 9 пакетных тестов; новых ошибок относительно этого сравнения нет.
- `git diff --check` и сверка области публикации выполняются до создания коммита.

CI выполняет компиляцию и четыре профильных модуля на Ubuntu/Python 3.12 и Windows/Python 3.14. Браузерные бинарники и действующие VK-сессии для этих 45 тестов не требуются. Результат GitHub Actions относится к конкретному опубликованному SHA и проверяется после публикации.

```powershell
python -m unittest discover -s tests -p 'test_accounts_v624.py' -q
python -m unittest discover -s tests -p 'test_account2_smoke_v625.py' -q
python -m unittest discover -s tests -p 'test_account2_batch_v626.py' -q
python -m unittest discover -s tests -p 'test_command_actions_v623.py' -q
```

## Существующие ошибки полного набора на Windows

Первый браузерный setup требует Linux-путь `/usr/bin/chromium`; после его ошибки встречаются каскадные ошибки Playwright Sync API внутри оставшегося asyncio loop. Один старый тест читает UTF-8 JSON без явной кодировки, что падает в Windows cp1251. Некоторые тесты не закрывают SQLite до удаления временной папки (`WinError 32`). Исторические тесты при публикации снимка не исправлялись.

Имена всех 24 ошибок, совпавших на базовой и публикуемой версиях:

- `setUpClass (test_access.BrowserProbeTests)`
- `test_report_written_when_run_raises (test_auto_pairs_v621.AutomaticPairsTests.test_report_written_when_run_raises)`
- `setUpClass (test_batch.BrowserJSFixtureTests)`
- `setUpClass (test_batch.PostLocatorRegressionTests)`
- `test_long_post_offscreen_footer_scrolls_but_nested_repost_and_comment_do_not (test_batch.RealVkLongPostRegressionTests.test_long_post_offscreen_footer_scrolls_but_nested_repost_and_comment_do_not)`
- `test_two_main_footer_controls_fail_closed (test_batch.RealVkLongPostRegressionTests.test_two_main_footer_controls_fail_closed)`
- `test_async_share_radio_render_is_waited_for (test_batch.RepostModalRegressionV622.test_async_share_radio_render_is_waited_for)`
- `test_unknown_modal_labels_not_publish_and_diagnostic_no_private_text (test_batch.RepostModalRegressionV622.test_unknown_modal_labels_not_publish_and_diagnostic_no_private_text)`
- `setUpClass (test_batch.StateRecoveryRegressionTests)`
- `test_live_label_matcher_rejects_removal_and_other_reactions (test_batch.V65LiveLabelAndRecoveryTests.test_live_label_matcher_rejects_removal_and_other_reactions)`
- `test_post_13376_live_label_only_one_safe_like_candidate (test_batch.V65LiveLabelAndRecoveryTests.test_post_13376_live_label_only_one_safe_like_candidate)`
- `test_comment_share_is_not_same_toolbar_as_main_post (test_batch.WrongCommentLikeSafetyTests.test_comment_share_is_not_same_toolbar_as_main_post)`
- `test_missing_post_footer_fails_even_if_comment_like_is_visible (test_batch.WrongCommentLikeSafetyTests.test_missing_post_footer_fails_even_if_comment_like_is_visible)`
- `setUpClass (test_dates_v613.PostDateBrowserTests)`
- `test_structural_diagnostics_do_not_capture_text_or_credentials (test_markup_unknown.UnknownMarkupTests.test_structural_diagnostics_do_not_capture_text_or_credentials)`
- `test_exact_date_from_matching_post_only (test_multigroup.BrowserMarkupTests.test_exact_date_from_matching_post_only)`
- `test_feed_scan_ignores_nested_other_group (test_multigroup.BrowserMarkupTests.test_feed_scan_ignores_nested_other_group)`
- `setUpClass (test_poc.TestBrowserFixtures)`
- `setUpClass (test_rate_limits_v614.UiRateLimitTests)`
- `test_account_budget_and_warning_are_global_across_groups (test_safety.CrossGroupSafetyTests.test_account_budget_and_warning_are_global_across_groups)`
- `test_action_budget_exhausted_before_click_preserves_pending (test_safety.SafetyActionStateTests.test_action_budget_exhausted_before_click_preserves_pending)`
- `test_budget_never_logs_a_failed_limit_reservation (test_safety.SafetyGateTests.test_budget_never_logs_a_failed_limit_reservation)`
- `test_limits_are_persistent_and_attempt_is_booked_before_click (test_safety.SafetyGateTests.test_limits_are_persistent_and_attempt_is_booked_before_click)`
- `test_warning_latches_across_restarts_until_manual_ack (test_safety.SafetyGateTests.test_warning_latches_across_restarts_until_manual_ack)`

## Результат независимой проверки кода

Блокирующих публикацию P0/P1 не найдено. Две задачи P2 оставлены для последующего патча, чтобы сохранить именно исходники проверенного запуска:

1. `vk_account_batch.py`: обновление только добавляет/обновляет подходящие записи; не отзывает валидность ранее импортированного поста. Воспроизведение: импортировать два поста, пометить первый `api_seen_posts.valid=0` в источнике, повторить импорт. В источнике остаётся один кандидат, в кеше №2 — два. Исправление должно переносить отзыв валидности без изменения истории действий аккаунта. Текущие проверки даты, корня, состояния поста и VK ID перед кликом сохраняются.
2. `vk_unified.py`: исключение `VK запросил вход/дополнительную проверку` останавливает пакетный запуск №2 с кодом 2, но не сохраняет `safety_lock`; буквальное сообщение `Появилась CAPTCHA` сохраняет блокировку. Требуется согласовать классификатор предупреждений отдельным изменением. Текущий запуск после исключения не продолжает действия.

Независимая проверка была read-only и не повторяла реальные действия в VK. Успешный тест 30/30 не устанавливает постоянную квоту VK, не закрывает полную модель `AccountPostState` и не устраняет перечисленные ограничения.
