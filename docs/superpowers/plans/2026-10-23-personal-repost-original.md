# Personal repost original implementation plan

**Goal:** process community posts whose exact original is a personal wall post,
then resume the paused VM after verified repair without reopening failed job112.

**Architecture:** keep the existing canonical key convention `-VK owner_id`:
positive means community, negative means personal. Old positive DB values stay
unchanged; SQLite INTEGER already preserves the sign. Only canonical reference
validation accepts a signed nonzero key. Selected source/destination communities,
post IDs, dates, identity guards and limits retain their positive validation.
Destination DOM matching uses the inverse signed owner, never abs or text.
This backward-compatible choice avoids a schema migration or history rewrite.

**Stack:** Python/Playwright1.64.0, VK schema5.199, SQLite, existing Windows VM.

- [x] RED: browser fixtures for personal/mixed chains, signed conflicts, exact
  target selection/confirmation; DB alias and uncertain isolation by sign;
  scheduler single-attempt flow and changed owner sign before reservation.
- [x] GREEN: adjust canonical validation and source-key formatting in
  vk_community_ui.py and remember_reference in vk_community.py; no fallback skip.
- [x] Run focused tests, full isolated suite and independent safety review.
- [x] Backup VM, gracefully install only relevant sources, re-read the exact
  failed source with correct actor; verify no new attempts and unchanged history.
- [x] Clear only action_stopped_source_reference after all guards, audit it,
  leave job112 failed/no attempt, restart one worker for installed code. Preserve
  day plan, quota ledger, no catch-up, two old isolations and all VK safeguards.
- [x] Observe fresh heartbeat, document deployed proof and remaining live scope.

Authorization: user confirmed "проверил, увидел, делаем" after the RCA; earlier
permission covers proven safe local repairs with backup/tests/review/audit.
No manual engagement test and no automatic isolation/skip of unsupported posts.
Local source has no .git; no publication is requested.
