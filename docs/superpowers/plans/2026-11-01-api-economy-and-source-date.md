# API economy and source-date repair implementation plan

**Goal:** Finish account attempt quotas with fewer repeated public reads, while improving exact browser date parsing and preserving live state.

**Architecture:** Keep the common client, hard rolling budget and complete fresh allowlist refresh before every adaptive batch. Bound preferred batch count by useful action work: aim for at least twice the estimated refresh duration in actions per batch. Time/API guards may reduce that count further; a small remainder still gets one batch. Improve only proven date parsing defects and add bounded anonymous failure evidence.

**Tech stack:** Existing Python, SQLite and Playwright; no new dependency or external API method.

## Constraints and decisions

- User authorizes fixes and publishing to main. Reuse the clean publication checkout; do not create a new worktree or disturb other work.
- Running VM worker is not stopped, restarted or hot-patched. Production investigation is SQLite read-only with no VK requests or browser inspection.
- Full freshness, exact identity/source/date/destination checks, >=540 seconds after actual finish, >=20 second cooldown, API >=5 seconds and <=5000 rolling24h remain mandatory.
- Quotas include failed/uncertain attempts. Do not refund, replay or change histories, claims, current target, actor bindings or paused states.
- Ratio 2 is an implementation choice: refresh preparation should take at most one third of estimated batch work, except a short final remainder. It is a preferred cadence, not an action authority or hard attempt limit.
- Date incidents have no saved failing DOM: demonstrate parser defects offline, do not claim they explain or resolve those exact live posts.

## Tasks / review focus

- [x] Read-only ledger attribution: Nov1 21:09 YEKT, 4440 reads: 4311 worker wall reads,65 alias identity reads,64 wall reads outside worker batches. Repeated full refresh dominates; current planner maximizes batch count.
- [x] Reproduce API overconsumption with measured costs in tests; limit preferred count, retain rolling expiry, late gates, small/zero remainder and no-refresh cases.
- [x] Simulate whole390, late32, slow batches, failures, account enable and continuous multi-day rolling budget. Compare old/new calls; never label simulations live acceptance.
- [x] Reproduce/fix exact-date script parsing and safe source-specific DOM handling. Preserve conflicting/mismatched dates and initial+3 windows, then date-skip without pause. Anonymous diagnostics must not expose raw DOM,URLs,headers,IDs or secrets.
- [x] Independent review; full local suite; document measured counts and limits.1235PASS/0skip/334.990s;151date-focused PASS. No installation into a running process.
- [ ] Publish main and inspect exact-SHA CI; activation remains separate from publication.

## Progress

- Initial clean baseline main00daa59 equals origin/main. Existing runtime911939c remains running unchanged on VM.
- Review found masked nested-time date conflicts in the initial duplicate-anchor fix;4RED regressions reproduced it. Aggregating all owned timestamp fields fixed it. No remaining review findings;140randomized planner comparisons show no capacity loss/read increase.
- Existing date-mocked CLI preview also failed with the old planner after calendar rollover. Explicitly pass the selected clock to the worker; RED/GREEN reproduced independently of the economy change.
