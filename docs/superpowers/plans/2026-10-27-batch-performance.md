# Batch performance and explicit reasons Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task-by-task; independent selection and UI tasks are delegated with dispatching-parallel-agents. Steps use checkbox syntax.

**Goal:** Remove repeated SQLite work so available batch time is spent on up to four actions, and distinguish actual time, quota, spacing and stop causes.

**Architecture:** One fresh selection context per pick/refresh eligibility pass, bulk actor and scheduler exclusions, no persistent candidate cache. Existing action executor, source priority, actual attempt accounting and safety checks remain authoritative. Granular batch statuses and absolute ready/deadline timestamps explain waits.

**Tech Stack:** Existing Python 3.13+ sqlite3, plain JavaScript, unittest/Node, Playwright; no new dependencies or schema.

**Spec:** User request 27 October: four-action throughput, clear panel reasons, unchanged limits/intervals, no catchup. A cold full public refresh, unavailable posts or slow VK can still prevent four; do not bypass those constraints or claim a universal guarantee.

## Global Constraints

- <=4 actual attempts per batch, all retries consume the same slots/quotas.
- >=540sec between actual first reservations of different batches; API>=5sec/5000rolling24h; sleep>=20sec/attempt;09–19YEKT.
- Keep complete fresh refresh, exact fresh_after,120sec read cutoff,60sec new-intent cutoff and final-click guards.
- Preserve saved plans, actor histories, protections, job631failed/interrupted, and isolated posts.
- No process stops for verification; only a justified installation may gracefully restart the worker. No extra VK actions or API probes.
- Source-only tests without accounts/bindings/SQLite/profiles/secrets; baseline/work C:/Temp/vk-perf-20261027.

## Review Focus

- Equal score ordering and signed canonical source dedup must remain identical.
- Re-read changes between selections; never retain a pool/status snapshot across actions.
- Unknown/partial counts and refresh failure must not authorize stale candidates.
- Time may advance during SQLite reads; preserve final safety checks.
- Old mixed status remains ambiguous; stale heartbeat does not become live from a countdown.

### Task 1: Bulk selection

- [x] Capture source-only baseline and installed evidence; GitNexus repository absent, source fallback.
- [x] Add failing semantic, fresh-read and query-scaling tests.
- [x] Implement State.selection_context + bulk exclusions and use once per refresh eligibility pass.
- [x] Wire Worker.pick to one fresh context; test complete selection and representative four-action batch.
- [x] Differential benchmark baseline/new for full ordered results and query/runtime cost.

### Task 2: Explicit reasons and timing

- [x] Add red regressions for cutoff/stop/quota distinctions; implement granular phases preserving the stopping decisions.
- [x] Add ready/deadline timestamps to real spacing heartbeat, with unchanged interval.
- [x] Node red/green regressions and UI labels/countdowns; legacy ambiguity explicitly retained.
- [x] Run rendered synthetic desktop/mobile panel checks with no VK access.

### Task 3: Verify and deliver

- [x] Fresh full source-only suite + compile/Node, independent review and fixes.
- [x] Copy only verified changed sources to workspace, preserving baseline backups/hash evidence.
- [x] Deploy to VM through graceful installation at quiescence; keep backups/config/tasks/history and gate to a strictly future saved slot.
- [x] Confirm installed hashes, fresh heartbeat and natural scheduled action evidence. Report actual throughput and remaining cold-refresh limits separately.
