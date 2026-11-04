# Analytics completeness repair implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Recover all obtainable wall statistics for the unchanged 64-community registry and prevent transient worker contention from permanently freezing an empty scheduled measurement.

**Architecture:** Keep the existing VM reader, ledger, pacing and immutable historical reports. Wait only for proven pre-network contention inside scheduled fetch; investigate wall variants using correctly isolated cache semantics. Preserve unknown/deleted data and identity mismatches explicitly rather than inventing counters or completeness.

**Tech stack:** Python, SQLite, existing VK API 5.199 client, unittest, Windows Task Scheduler.

**Spec:** User request in this chat; `docs/OMSU_ANALYTICS.md` and `docs/OMSU_OBSERVATION_FROM_14.md`.

## Global constraints

- Never stop the worker for diagnostics or replace files imported by its live process.
- Preserve actor databases, quotas, bindings, scheduler claims, global 5000/24h ledger and at least 5-second API pacing.
- Preserve 250 scheduled request reservations, existing 25-minute/deadline cutoff, no catch-up, protection/auth/rate failure behavior.
- Never mix wall filters in `wall.get:5.199:owner` cache. Existing frozen runs/reports/checkpoints remain immutable.
- Unknown counters stay NULL; absence is not proof of deletion; foreign owner IDs are never reassigned.
- Exact historical ER may remain unknowable for genuinely unavailable posts; describe scope and evidence honestly.

## Review focus

- Busy before page one and after prior pages: wait without leases or open transactions and resume the exact requested page.
- Deadline after pacing and interruption during waiting: no extra request or renewed deadline; durable reservation remains conservative.
- Actual read/auth/protection failure following busy: never grant a retry.
- Alternative wall filters and malformed foreign items: cache isolation and exact identity must hold.
- Missing/deleted publications or open periods: never falsely advertise a complete historical or official result.

## Task 1: Scheduled contention

Files: `vk_analytics_scheduled.py`, `tests/test_analytics_scheduled.py`.

- [x] Reproduce busy-to-permanent-empty behavior with fake clock/reader tests.
- [x] Add injectable sleeper to scheduler entry points; wait inside fetch only for exact `Deferred('worker_or_profile_busy')`, one durable page reservation, unchanged deadline.
- [x] Test busy recovery, busy after first page, cutoff, interruption/resume, and read/protection error after contention.
- [x] Run scheduled and collector tests; preserve historical checkpoints unchanged.

## Task 2: Establish missing-post and foreign-item causes

Files initially inspected only: `vk_analytics_collect.py`, `vk_analytics_store.py`, `vk_public_data.py`, `vk_api_read.py`, `scripts/vm/probe-analytics-counts.py`.

- [x] Obtain bounded public metadata for the two invalid-owner walls under existing reader/leases.
- [x] Compare explicit owner/all filtering without owner-cache reuse or writes; prove whether missing IDs are guest posts, unavailable originals, or other documented objects.
- [x] Add failing tests for confirmed code defects, then implement only source-verified handling. Update this task with the exact interface before editing a new boundary.
- [x] Keep unsupported identities and unavailable counters fail-closed. Do not backfill historical measurements with current values.

Confirmed interface: `store.post_identity(gid,item)` returns explicit local alias
and canonical owner/post only from the strict observed `coowners` witness.
Two additive metadata tables retain identities and all date witnesses; reports
deduplicate actual canonical posts using the latest whole observation. No
existing table/row is migrated. Missing aliases carry only previously proven
identity, never counters; conflicting dates outside the period remain visible.
All 71 historical IDs were absent in uncached `all` pages too; no unsupported
by-ID retry was attempted. Their removal/accessibility cause remains unknown.

## Task 3: Verification and installation

- [ ] Run full offline suite, focused regressions, source review and exact-commit CI required by project workflow.
- [ ] Prepare reviewed source-only installation after natural process completion; protect analytics backup and immutable old records, leave worker source/state unchanged.
- [ ] Perform a bounded new collection with unchanged registry and explicit period, verify all 16 causes individually and report exact obtainable coverage/ER.
- [ ] Verify scheduling behavior, worker health, history preservation and remaining limits; update operational docs and local working source separately from publication/deployment status.

## Diagnosis so far

- 14 walls have 71 historical references absent from owner-filtered wall pages.
- 2 walls contain foreign top-level owner IDs, correctly rejected by current identity guards.
- One additional publication lacks views, independently preventing complete counters.
- Nov4 08:00 run froze 0 posts after one worker-busy defer, leaving 63 groups pending.
- Existing diagnostic filter probe modifies transport but shares owner cache; its variant comparisons cannot be trusted as-is.
- Clean canonical checkout c5ea8662a6ccc2b4124753aae7ee35cf48b117a2 matches deployed analytics modules; 71 focused baseline tests pass.
