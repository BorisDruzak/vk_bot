# Resumable actions implementation plan

> **For agentic workers:** Use superpowers:executing-plans and TDD. Steps use checkboxes.

**Goal:** One durable retry per logical action and stop after three consecutive failed actions.
**Architecture:** Dedicated recovery ledger records execution passes, attempts and held uncertainty. Worker uses it only under explicit policy v1. The action executor verifies current state before a like retry; repost recovery only reads confirmation after intent.
**Tech Stack:** Existing Python, SQLite, Playwright and plain JS.
**Spec:** docs/RESUMABLE_ACTIONS_V6_33.md

## Global constraints

- Window09–19YEKT; actual attempts<=4/batch; action pause>=20s; batch starts>=600s.
- Quotas50/10/20; API>=5s,5000/24h; retain history and immutable day plans.
- No blind repost resend; protections and unsafe failures stop immediately.
- No git checkout here; preserve unrelated analytics changes; no Git publication.

## Review focus

- Crash between parent intent and actor reservation must fail closed.
- Batch boundary must retain exactly one pending retry without catch-up.
- A different uncertainty must never be exempted by a recovery record.
- Third failed logical action stops; success resets only this streak.
- Exact negative like evidence must precede the only retry click.

### Task1: executor recovery

Files: vk_scheduled_action.py, tests/test_action_retry.py.
Interface: execute_action gains recovery_attempt_id=None, allow_retry_click=True.
With prior attempt: verify exact immutable actor attempt and uncertain status;
return verified/already without send if proved, retry like once on strict negative
evidence, or return uncertain without send for unresolved repost/disabled click.
- [x] Add failing tests for identity/source/attempt validation, strict negative proof and no repost send.
- [x] Implement and run focused tests including cooldown/guards.

### Task2: durable ledger and worker

Files: new vk_action_retry.py, vk_scheduler_state.py, vk_scheduler.py,
vk_scheduler_recovery.py; tests/test_action_retry_state.py and runner tests.
Interface: snapshot has scheduler.action_retry {enabled,consecutive_failures,held,pending};
each job.recovery {state,passes,attempts:[{pass,attempt_id,outcome,reason}]}.
States: running, pending, held, complete, interrupted. Exact held/pending entries
exempt only their own uncertain job; running exemption exists only in current owner
process and disappears on restart. Attempt IDs remain in append-only pass journal.
- [x] Tests RED for retry once, real quota/batch cap, restart, hard stops, streak reset/third failure, unrelated uncertainty.
- [x] Implement ledger/orchestration, run targeted regression.

### Task3: panel and acceptance

Files: vk_control.py, web/app.js, frontend tests; AGENTS.md and context docs.
- [x] Test and render attempts/recovery state and failure counter, no misleading success.
- [x] Independent review, focused and isolated full VM tests.
- [x] Backup VM, graceful code update, enable policy and audited job120 reconciliation using manual124 proof.
- [x] Start one worker; observe only existing permitted schedule; document actual results and remaining limits.

## Execution record

475 local tests PASS;475 isolated VM tests PASS (10 Node skips covered locally).
Independent code and operator-script review: no important findings remain.
VM source hashes verified; backup/integrity; one graceful update stop.
Policy1 enabled; old manual124 reconciled with4positive readings, attempt123
retained uncertain; all attempt rows/other jobs/plans/isolations unchanged.
Worker live PID9492; first subsequent scheduled batch under observation.
