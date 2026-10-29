# Three Accounts Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, with one final independent review.

**Goal:**60saved batches of4 permit240daily attempts for3actors in09–19YEKT.
**Architecture:**shared540second constant for generation/claim/reservation;
durable dayplans remain unchanged; registry applies next unplanned day.
**Tech Stack:**existing Python/SQLite, Windows task and browser guards.
**Spec:**../specs/2026-10-23-three-accounts-design.md

## Global Constraints

09–19YEKT,≥540sec actual batch spacing,≤4attempts/batch,≥20sec/attempt,
API≥5sec/5000rolling24h, no catchup, quotas50/10/20/actor, exact identity guards.

## Review Focus

- New60slotplan must fit for multiple/random/constant RNG, retain unique gaps.
- Both claim and durable reservation spacing use540, including restart.
- Previously saved dayplan remains unchanged when third quota is enabled.
- Raw uncertain/history/locks and API ledger must remain intact.
- Third actor maps to its exact existing physical profile and matching identity.

### Task1:Calendar and state spacing

- [x] Red tests:60slotthirdfullquota,539.999/540boundary, savedplan preservation.
- [x] Change vk_schedule_policy.MIN_BATCH_GAP_SECONDS and two state checks.
- [x] Adjust old tests whose assumptions specifically represent old600policy.
- [x] Green targeted and full isolated tests, stress random seeds, final review.

### Task2:VM and third account

- [x] Check third actor login/registry/targets read-only without stopping worker.
- [x] Backup code/config/SQLite, verify source hashes and wait for safe install point.
- [x] Install checked files through necessary update stop, preserve dayplan and history.
- [x] Fresh matched proof, configure exact third actor50/10/20 and prior targets.
- [x] Preview/persist nextunplanned60slotday safely; audit one worker and metadata.
- [x] Update currentAGENTS/context/automation if applicable; noGitHubpublish.
