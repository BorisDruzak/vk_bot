# Final scope amendment29Oct2026

Schedule and identity milestones COMPLETE: f32c868 schedule installed17:55:44,
auth088073a installed19:42:34YEKT after exactgreenLinux/Windows+OMSUCI.
Local1087PASS/312.555s,VM1087PASS/24Node-skip/618.533s;11DB/integrityok,
27JSON/151swapchecks/139postrestarttables preserved. Today4/80immutable,
future30Oct67/390/55six12five. Fresh interactive exactmatched19:43:41,
fifthactor7 enabled50/0/20; oldfourrecords/quotas/bindings/history unchanged.
Finalacceptance19:43:55PASS; worker3592/live/waiting_batch,pausedNULL,
stopFalse/future saved-slot19:52:08/no-catchup. Onlyusers.get alias preparation;
no production fallback. Intermediate227utils1051candidate never installed.

User explicitly added a fifth actor, disabled its personal reposts, and retained
exactly67future slots for390quota. Oldfour50/10/20 unchanged; fifth50/0/20.
Optional planned_batches67 + maxbatch6 yields55six-targets and12five-targets.
Every saved day (today4/80) and historicalpolicy withoutoverride remain unchanged.
Policy/runner/retry/diagnostics/simulation tests cover balanced target caps.
Current spec: docs/FIVE_ACCOUNTS_67_BATCHES_2026_10_29.md.
Final fullsuite/review/mainCI/VMtests/deployment/identity gates passed.

Previous plan follows as historical context:

# Six-action future batches implementation plan

**Goal:** authorized6-attempt batches/54daily batches for four unchanged80-attempt
account quotas; preserve every saved plan, history and quota; publishmain and
deploy only the successful CI revision.
**Spec:** direct user choice29Oct2026 in this chat:6/54, simulate/test, main,
greenCI thenVM, first new/unplanned day only. Baseline ffaeefdc42d9ca45647193d713efb0e7c5204199.
**Architecture:** extend validated maximum to6 while retaining historical
DEFAULT_CONFIG4. Future vk_automation policy6 + live registry320 produces
ceil(320/6)=54; saved day config remains authoritative. Runner capacities and
diagnostics must use that saved config. Stdlib/unittest/current Playwright.

## Invariants / decisions

- 09:00–22:00YEKT, quotas50like/10personal/20community each,320total unchanged.
- <=6 means maximum attempts, not guaranteed confirmations;54×6 has324places
  but daily ledgers forbid exceeding320. Retry spends the same capacity/quotas.
- Preserve unique intervals>=540s, sleep>=20s, API>=5s/5000rolling24h,
  read/intent/click deadlines, singleton/activity/profile leases, guards/no-catchup.
- Existing saved days, including29Oct4/80, are immutable. Future policy only
  applies to the first unsaved day. No actor bindings/config destinations change.
- Reuse clean publication clone, explicitmain publication authorization.
- VM-only vk_web.py POS overlay remains untouched; maintenance requires
  natural quiescence and verified backup/restore, no forced worker termination.

## Tasks / evidence ledger

- [x] Policy lane: validator<=6, default4 retained, configuredfuture6; tests
  generated54 unique schedules and actual/future capacity without quota mutation.
- [x] Runner/state lane: six real reservations/never seventh, bounded candidate
  selection, two-pass retries consume places, end/click deadlines unchanged,
  today's saved4/80 remains unchanged across refresh/restart/future config.
- [x] Simulation lane: deterministic real Worker/State future54x6 simulations,
  measured durations/refresh, fullquota limits, repeatability, partial/late/stop/
  retry-budget cases; retain historical80-slot expected3/4 case.
- [x] Diagnostics/UI lane: saved batch capacity and generic missing-next-action
  explanation, current4/80 vsfuture6/54; no misleading fixed-fourth label.
- [ ] Run focused RED/GREEN tests, full local offline suite, independent review;
  update AGENTS/context/docs. Commit/pushmain and wait for both CI workflows.
- [ ] Stage exact green artifact onVM, source-only VM suite, fresh live/source
  inventory, narrow future config change, quiescent graceful installation,
  10DB-style backup and protected history/schedule/quota/hash/task proof.
- [ ] Verify todaystill4/80 and future6/54 onVM without creating a live future
  plan merely for testing; record actual first-unplanned effective date.

## Review focus

Today config leaking from future config; retry overflow; incomplete six-place
selection after proven skips; quota ceiling320 vs324theoretical; new diagnostics
applied incorrectly to historical4-slot batches; VM overlay/config drift.

Local evidence:1015PASS/0skip/293.462s; fresh independent reviewer21focusedPASS,
no blockers. BrowserMCP1440x1000/390x844 policy+deadline explanationsPASS.
