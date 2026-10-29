# Batch spacing wait implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Preserve a claimed batch while a short safe spacing wait completes.
**Architecture:** Worker waits before pick/intent within the original slot grace;
State reservation and click guards remain authoritative. Frontend distinguishes
finished skips from future slots without changing backend history.
**Tech Stack:** Existing Python/SQLite, vanilla JavaScript/CSS; no new dependency.
**Spec:** docs/superpowers/specs/2026-10-24-batch-spacing-wait-design.md

## Global constraints

540сек between actual batch starts; <=4 attempts/batch; sleep>=20;09–19YEKT;
5000 API/24h with5сек pacing; immutable plans/history; no catch-up or extra sends.

## Review focus

- Stop/paused/active uncertainty or platform warning during wait stops dispatch.
- Clock reversal/day rollover/deadline never create an unlimited wait.
- Ready refresh after slot grace remains valid; only a new wait is bounded.
- Historical batch_spacing_wait remains closed and cannot be reclaimed.
- Unknown/partial UI statuses never become successful or future-planned.

## Task1 — worker wait

- [x] Backup unchanged source; add regression tests using temporary ledgers.
- [x] Run RED for short spacing waits and cancellations without any live writes.
- [x] Add bounded worker helper and injectable sleeper; leave State unchanged.
- [x] Run GREEN and existing runner/state/recovery tests.

## Task2 — timeline meaning

- [x] Add rendered/helper regressions for completed skip and future/current slots.
- [x] Run RED; implement minimal helper/legend/style and run GREEN.
- [x] Verify fixture in desktop/mobile real browser, including errors and refresh.

## Task3 — review/install/acceptance

- [x] Run isolated full source-only suite and fresh-context review.
- [x] Backup VM and verify source hashes, absence of guards/inflight; install
      between actions with only the necessary graceful worker update restart.
- [x] Verify one live worker, unchanged protected history/dayplans and installed
      assets. Observe subsequent native work, no manual sends/no replay.
- [x] Record actual results and remaining limits in project context.
