# Adaptive non-overlapping daily queue implementation plan

**Goal:** automatic targets/count, full YEKTcalendar day,540seconds rest AFTERfinish,
liveaccount changes nextbatch, actual-attempt quotas preserved, no next-day backlog.
**Spec:** docs/ADAPTIVE_QUEUE_2026_10_30.md and explicit user replies30Oct.
**Architecture:** opt-in adaptive pureforecast + durablestate + workersubclass,
reuse existing actionexecutor/RetryLedger/platformguards. Legacydata immutable.
**Tech:** Python/SQLite/existingPlaywright, current publicsharedreadledger.

- [x] Pure forecast: RED/GREEN weightedcost/time/rest/APIrollingbudget, feasible
  targetpartition, mid-day insertion, impossible/zeroquota/dayboundary scenarios.
- [x] Read-only telemetry costcollector and reproducible full/dayremaining simulation.
- [x] Opt-in config24h and policyvalidation, activeaccount snapshot at nextbatch.
- [x] Durable adaptiveactivation/revisions/claim/rest/restart; preserve oldrows/IDs.
- [x] Worker dispatch from actualfinished+540, only midnight/actionguards boundary,
  dynamic targetlimits/retries, no stale/partial pool or budget bypass.
- [x] Snapshot/timing/UI predicted-vs-actual/rest/admission/shortfall, desktop/mobile.
- [x] Integration tests: longbatches/no overlaps, enabledaccount nextbatch,
  failed/date-skipped/uncertain quotas, crash/restart, dayrollover and budget.
- [ ] Fresh independent review/full local suite, docs/context update, publishmain.
- [ ] ExactSHAgreenCI + source-onlyVMfullsuite, canonicalbackup/source/config
  cutover preserving today’s history/quotas and superseding only unstartedforecast.
- [ ] Verify installed source/policy/schema/history/rest/no-catchup and live state.

Global constraints: actor50like/10personal/20community maxima, explicittwogroups;
attempts and intent spend oldledgers, sleep>=20, API>=5/5000rolling24h; fresh
identity/source/destination/protection/cleanupguards and exactuncertainty proofs.
No actorbinding/profile/secret/history copies to Git. No test-induced worker stop.

Review focus: activebatch snapshot cannot absorb new quota midflight; no batch
claimed before actualfinish+540 even across midnight/restart; APIexpiry forecasts
never authorize exceeding the commonledger; live slowactions shift predictions
without interrupting at nextforecast; full-day bounds reject booleans/naivetimes;
infeasible report does not hide/reset spentquota or carry oldjobs into nextday.

Execution ledger: plan saved before production edits. User confirmed540s after
finish and attempted-action quotas. Live16:14sample145/390,245remaining,
8batch_action_deadline, API3190/5000, pausedNULL. Currentruntime5229d66 untouched.
