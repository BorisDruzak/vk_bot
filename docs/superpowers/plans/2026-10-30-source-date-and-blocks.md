# Source-date retries and scheduler block lifecycle

> Implement inline with superpowers:executing-plans; independent source-reading and block-lifecycle modules may be delegated with explicit file ownership.

**Goal:** Three additional date-reading attempts, then skip without pausing; manual blocks persist, automatic blocks expire at the next saved scheduled day.
**Spec:** User requests30October in this chat, superseding prior stop-on-missing-date and permanent automatic-pause rules. Preserve all actions/history/quotas/saved schedules and no-catchup; publishmain and deploy only exactgreenCI source.
**Tech:** Python, SQLite, existing Playwright>=1.55,<2 (verified1.64 family); stdlibunittest, real synthetic browser fixtures.

## Design

- Date-only retry: initial9s hydration window plus at most3 additional9s windows, no new action executor/pass/send. Recheck identity/protections and caller deadline before every read/wait. Missing metadata or date-evaluation errors are retryable only while those guards remain proven. Known mismatched/conflicting dates remain protective stops.
- Exhaustion produces a typed, exact-source date skip. The scheduler preserves spent attempts/uncertain evidence and excludes that post from further actions for that actor/day. No action-failure streak, no global pause, no resubmission. Existing retry passes retain max2 for other errors; old interrupted jobs are not reopened.
- Durable exact recovery receipts permit only the explicitly authorized date-skip uncertainty to coexist with other work. Unrelated uncertainty/cleanup/identity/platform errors remain blocking. Three or more date skips never trip the old three-failure guard.
- Block record: manual/automatic kind, reason, creation day/time. Manual blocks never expire or get overwritten by an automatic failure. Automatic block expires only when a later YEKT day has a saved plan; resumption selects a future saved slot, no catchup. Clearing the scheduler block never erases actor safety locks, unknown outcomes, quotas or history. Legacy untyped blocks remain manual until explicitly classified/released.
- Existing string paused remains compatible for all guards/status consumers; typed metadata is additional, transactionally consistent. Provide explicit manual block/release operator path and visible kind/expiry information, preserving other stops.

## Tasks

- [x] RED/GREEN typed bounded date reader tests (missing/evaluate error/success on fourth/conflict/protection/deadline; no send or repeated navigation).
- [x] RED/GREEN scheduler date-skip receipts, quota/intent preservation, same-post exclusions, first/second-pass and three-skip/non-stop regressions.
- [x] RED/GREEN manual/automatic block lifecycle, overwrite/restart/legacy/date rollover/no-catchup tests; wire tick/CLI/status.
- [x] Independent review, meaningful focused tests and full isolated local suite. Update architecture/context and user-facing behavior docs.
- [ ] Commit/pushmain; exactSHA green Linux/Windows/analyticsCI. Source-only isolatedVM suite.
- [ ] Canonical natural-quiescence/backup/source-only install. Preserve all saved plans/actor histories/quotas/guards/POSoverlay; restore tasks and future-slotgate. Verify live state without test actions.

## Review focus

1. Missing date after a prior real attempt must not resend or erase uncertainty/quota; hidden extra actor attempts must not gain an exemption.
2. Guard/deadline/cleanup errors nested under a date exception never become a skip.
3. Date-retry exhaustion is finite even with repeated browser-evaluation failures and never crosses a saved boundary.
4. Manual block survives new automatic failures and midnight/restart; stale/malformed type records do not auto-clear a different paused reason.
5. Automatic expiry preserves the saved day and starts only from a future eligible slot, leaving independent actor safety locks and uncertainty intact.

Execution ledger30Oct: local1116PASS/0skip/317.471s under -Xutf8; first non-UTF8 run1113 had one pre-existing cp1251fixture error. Final new cases and review regressions included in1116. Review legacy manual-overwrite and orphan-liveness findings fixed with RED/GREEN tests. UI desktop/mobile nooverflow/error. Private maintenance helper9Python+4PSguardsPASS. Runtime baseline088073a; publication baseline d7511d7, VMworker3592/pausedNULL at11:07. No runtime stops for checking.

Ruling: automatic pause calls under an existing manual block must not overwrite its already suspended original automatic record or create a new hidden automatic block. One-line fix plus2RED/GREENcases,98focusedPASS; finaldiscovery1118. Candidate7456b56 is superseded before deployment; rerun exactSHA CI and source-onlyVM suite for the final commit.
