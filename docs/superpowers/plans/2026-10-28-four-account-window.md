# Four-account window Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement inline.

**Goal:** Save four equal 50/10/20 policies within 09:00–22:00 YEKT.

**Architecture:** Preserve default/historical 09–19 policy; permit explicitly
configured end_hour up to22. Centralize future registry policy capacity checks
on vk_automation.json. Expose current and future windows to the panel.

**Spec:** ../specs/2026-10-28-four-account-window-design.md

**Constraints:** saved plans immutable; >=540sec between batches; <=4 attempts;
sleep20; guards/API5000/24h/history unchanged. New actor disabled until user enables.

## Task 1: policy and account configuration
- [x] Add failing tests: 320/80 fit22 but not19; stop22; old day remains19;
      account save and connection respect configured22; reject23 and overflow.
- [x] Update vk_schedule_policy.py, vk_accounts.py, vk_connections.py,
      vk_control.py, web/app.js and web/index.html; render saved/future windows.
- [x] Run targeted tests, full isolated unittest suite and synthetic browser QA.

## Task 2: installation and configuration
- [x] Confirm login6 opens naturally and waits for user or completes; no duplicate launch.
- [x] Stage only source/tests/web; run full isolated suite on VM; verify baseline hashes.
- [x] Back up VM configs and SQLite; install while quiet using graceful worker stop.
- [x] Set future automation window22; save account6 50/10/20 and supplied destinations,
      preserving enabled=False and all other actors' quotas.
- [x] Verify saved plans/history, four equal quotas, source hashes and panel status;
      restart task definitions unchanged and no-catchup with future saved slot gate.
- [x] Update current repository documentation with exact validation/deployment outcome.

Validation:728 local,728 VM (skip19Node),0failures/errors; Browser plugin absent, Playwright MCP desktop/mobile save flow passed. Independent reviewer: no blocking findings. Initial VM run lacked service-user browser cache; retry used existing cache with PLAYWRIGHT_BROWSERS_PATH, without reinstall. No live worker stop for any check.

Installed17:50:57YEKT;132swapchecks/121postrestarttables,61fileSHA+1unreadable,3TaskXML.Quota-result/final-state PASS:all4 policies50/10/20,current19/future22effective29Oct,actor6disabled.
