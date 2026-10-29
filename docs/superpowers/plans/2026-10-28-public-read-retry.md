# Public read connection retry Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans and TDD; independent runtime and UI tasks use dispatching-parallel-agents, followed by independent requesting-code-review.

**Goal:** Retry eligible transient public counter-read failures up to three total attempts without changing action retry policy or weakening limits.

**Architecture:** Page-local bounded retry inside vk_batch_engagement.refresh. Each attempt creates the normal one-request VKReadClient, preserving shared cache/ledger/pacing/fresh_after and existing transport20sec bound. No retries for actions, authentication/protection/rate limits, malformed payloads, unknown or guarded exception chains. Diagnostic/heartbeat fields identify retry2/3 and wait-until.

**Tech Stack:** Existing Python3.13+ sqlite3/urllib, plainJS, unittest/Node; no dependency or schema changes. CPython3.13.9 Context7 docs checked (closest supported to VM3.13.17).

**Spec:** Direct user request following INCIDENT_2026_10_28.md. Three total attempts; delays5/10sec with default settings (preserve longer configured API pause). Every physical request spends the same <=5000/24h budget. No retry if the next full20sec request cannot fit before the existing refresh deadline. If an ordinary network failure leaves no room/budget for another attempt, propagate the original failure into the existing technical backoff/third-failure guard; do not relabel it as success or benign cutoff.

## Global Constraints

- Keep >=5sec API pacing, HTTP20sec per request, full fresh pool and exact fresh_after, next-slot-minus120sec preparation cutoff.
- Keep <=4 actual actions, >=540sec actual batch spacing, sleep>=20sec per action,09–19YEKT, quotas50/10/20 per enabled actor.
- No retry on CAPTCHA14/validation17/429/rate limits/auth/identity/source/destination/cleanup guards or ambiguous chains.
- New checks before/after bounded retry waits; preserve pause/stop/disable/day/window/budget semantics and historical631/isolations.
- No action resends or source-list expansion, no old-slot reopening/catchup, no changes to saved plans/history.
- Source-only baseline/work C:/Temp/vk-read-retry-20261028; no account registry/bindings/SQLite/profiles/secrets in tests.
- GitNexus vk_bot absent: verified local/source baseline. Local directory has no.git; no fabricated commit/push.

## Review Focus

- All failed request reservations count; only complete fresh payload can enter pool.
- Guards arising during sleep, cache recheck, or nested error chain prevent all further HTTP.
- No-headroom retry preserves original ordinary network error/backoff rather than bypassing its streak.
- Longer configured API pacing is never shortened; waits remain bounded/responsive to stop.
- Heartbeat retry indicators reset for next page/success and do not imply more action attempts.

### Task1: Runtime retry

- [x] RED tests: two failures then success, three exhausted, counts/pacing/freshness, cutoff/budget, stop/pause/platform/HTTP guards.
- [x] Implement page-local retries and numeric sanitized progress/events; run focused existing regressions (103 PASS).

### Task2: Panel visibility

- [x] Add Node RED/green tests for refreshing-counter retry2/3 and fresh/stale countdown.
- [x] Render retry progress without exposing source/body/exception contents; synthetic browser desktop/mobile checks (1365x900/390x844, no overflow/console errors; stale/attention).

### Task3: Verify and install

- [x] Fresh full isolated suite (719 PASS,137.730sec), compile/Node, independent review and fixes. Review found acceptance-helper dict parsing issue, corrected; worker/UI no blocking findings.
- [x] Copy verified changed files preserving originals; create manifest/package with exact hashes (112 source-only files,5 changes).
- [x] VM source-only full suite using existing browser cache (719 PASS,226.654sec,18Node skips which passed locally); reviewed installer,9SQLite backup, natural quiet update and strict future-slot gate. Installed15:08:44YEKT; gate15:10:51,worker3152.115protected tables/62readable saved files/3task XML verified.
- [x] Confirm installed hashes, protected data/tasks/config, healthy scheduled continuation; no induced VK errors or manual test actions. Slot37(15:10:51–15:18:23)4attempts/4verified,4cooldowns20.012–20.054sec,spacing549.264>=540; all prior jobs/actorattempts/isolations/plans unchanged. Worker3152/live/monitoring,pausedNULL.
- [x] Update documentation and distinguish offline retry proof from any naturally observed live retry. No natural network failure/retry in the accepted batch; branch proven offline only.
