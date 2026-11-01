# API budget and exact source dates — 1 November 2026

## Production evidence (read-only, 21:09 YEKT)

The common `api_read_attempts` ledger contained4440 requests in the current
calendar day/rolling24h snapshot:

| Consumer | Requests | Evidence |
| --- | ---: | --- |
| Worker public wall refreshes |4311|4310 completed HTTP reads plus1 transient failure in scheduler diagnostics |
| Exact own-header alias resolution (`users.get`, ledger group0) |65|Shared ledger, not one request per action |
| Wall reads outside batch execution |64|Evening analytics read64groups; morning analytics deferred without reads |

There were64 claimed batches by this snapshot.40 had66wall requests,21 had64,
2 had130 (fresh+reserve layers),1 had67 (one technical retry). This is shared
public data for all actors, not64 independent reads per actor. No extra VK
requests, profile opens, clicks or sends were made for this investigation.

The old planner deliberately chose the maximum balanced number of batches that
fit the remaining day and hard rolling budget. Each batch freshly reads all64
allowed groups. Repeating that preparation for small targets wastes most of
the budget and leaves little room for variance. The hard5000 guard was not
bypassed or exceeded.

## Fix

Keep every mandatory fresh group read and the existing common client/ledger.
Bound preferred batch count by useful work:

`preferred_count <= max(1, floor(remaining_weighted_action_seconds / (2 * refresh_seconds)))`

The existing time bound and exact rolling24h/pacing simulation can reduce that
count further. Batch targets remain balanced and automatic; neither11 nor36
is a new fixed setting. Measured p95 changes, account toggles and remaining
quotas still replan the next unclaimed batch. A short final remainder gets one
fresh batch. Zero refresh cost keeps the original time-limited calculation.

Ratio2 is an implementation choice: prefer at least twice the estimated
preparation time in useful action work, so refresh overhead is normally at most
one third of estimated batch work. It grants no action permission or deadline.

All390 daily attempt limits (including errors/uncertain), exact identity,
source/date/destination guards, disable checks, frozen active claim/roster,
cooldown>=20seconds, API pacing>=5seconds/5000rolling24h, rest>=540seconds after
actual finish and own-day midnight minus120seconds remain unchanged. No cached
previous-batch counts or partial observations are admitted to save requests.

## Date parsing

Receipts for jobs2245 and2353 prove four exhausted date read windows before
intent or attempt. No failing HTML snapshot exists, so their exact live cause
is unknown. Do not call those posts repaired or replay them.

Offline fixtures reproduce two parser defects: only the first inline
`apiPrefetchCache` assignment was inspected; multiple exact-source date links
were discarded even when they agreed. The parser now checks all bounded JSON
assignments and collects all exact-link ISO/UNIX evidence independently from
the link and its owned time descendants. Conflicting timestamps fail closed;
multiple post roots remain ambiguous, and embedded other-post times are ignored.
JSON provenance precedence is unchanged; scripts are parsed, never executed.

A final natural date skip now records only bounded numeric/boolean inspection
counts and read-failure counts. No raw HTML, URLs, names, identities, credentials
or exception text enter the new summary. The initial9second window and maximum
3repeat windows, then typed skip without global pause, remain unchanged.

## Verification and limits

Measured model: like67.581s, personal61.989s, community98.142s;
fresh refresh384.341s/66requests. Deterministic virtual execution replans after
each actual virtual finish; identity cost is conservatively1read per attempt,
and128reads are reserved for analytics (not counted as executed reads below).

| Scenario | Before: batches / reads | After: batches / reads | Attempts |
| --- | --- | --- | --- |
| Full five-account day |62 /4482|36 /2766|390 /390 |
| One batch3times slower |60 /4350|36 /2766|390 /390 |
| Failed/uncertain quota accounting |62 /4482|36 /2766|390charged attempts |
| Sixth account enabled at midday |56 /4166|43 /3308|470 /470 |

The full-day read reduction is38.3%; including the conservative analytics
reservation, modeled total is2894/5000. The late32-like plan uses2refreshes
and164estimated reads instead of9refreshes/626. Three consecutive simulated
days carry the rolling ledger across midnight and complete390daily attempts
without exceeding3300reads including analytics reservation. All simulated
rests remain>=540seconds. These are capacity estimates, not live acceptance,
and the uncertain accounting scenario does not bypass production stop guards.

Tests include small/zero remainder, no refresh cost, tight time and API bounds,
new actor, actual slow finish, restart gates, conflict/mismatch and diagnostic
redaction. Independent review also compared140deterministic randomized planner
inputs against HEAD: no loss of planned capacity and no increase in reads.

The CLI now passes its clock explicitly into the worker. Its existing
date-mocked preview test failed with the unchanged old planner after the
calendar day changed: a default-bound real clock was mixed with mocked time.
Explicit clock injection repairs that inconsistency without changing live time.

Final local verification:1235tests PASS,0skips,334.990seconds;151date-focused
tests PASS, compilation and diff checks PASS. The added regressions were first
observed failing, then passing. Independent review has no remaining findings.
Exact commit CI is checked separately on GitHub; a local pass is not CI proof.

## Installed after explicit authorization

Source `5fefa96fc40c0ba3113faf8caaea9aede14cf7b3` installed on the canonical VM
at22:16:06YEKT. Exact [offline Linux/Windows CI](https://github.com/BorisDruzak/vk_bot/actions/runs/41326336867)
and [analytics CI](https://github.com/BorisDruzak/vk_bot/actions/runs/41326336861)
both completed successfully. The291-file frozen package was tested separately
on the VM:1235tests,0errors/0failures,33Node-only skips,583.465seconds.
Those Node checks passed locally and in CI. The first VM test attempt lacked
the SSH user's browser cache; its failed receipt is retained. A per-test-process
browser cache path fixed the environment; the final full run used a clean
extraction of the same immutable package. No dependency/runtime setting changed.

The user explicitly authorized a safe update/restart after natural completion.
Batch66 finished22:10:26.244642. An exclusive quiet activity lease admitted the
graceful stop request22:14:03.998084, with no running job/pass/batch and no worker
force-kill. Backup:11SQLite databases/integrityOK and27JSON configs. Only5Python
modules changed;47unchanged source/assets and the VM-only POS web overlay/favicon
were checked.168table/schema checks at swap and157tables after restart passed.
All prior adaptive claims, forecast rows, recoveries, jobs, actor attempts,
date-skips, quotas, bindings and JSON bytes were preserved. Schema/config changes:0.
The original task XML and enabled flags were restored exactly.

Read-only acceptance22:17:23: PID9936 live/waiting_batch, pausedNULL/stopFalse,
0inflight/unfinished/running passes,2373old jobs unchanged; HTTP root/app/session
all200,0VKrequests/0actions made for acceptance. Idle diagnostic/current-day
forecast appends are allowed; every old row remains unchanged.

Resume gate and next forecast are22:19:26.244642, exactly540seconds after batch66
finished. At22:18:14, daily spend remains374/390 (234likes,40personal,100community),
16likes remaining, no active uncertainty. The running new planner reports
`refresh_work_ratio=2`,1future batch/target16/82estimated reads, versus the old
4batches/target4/280reads for the identical remainder.296reads are available;
the estimated completion22:43:51 is a forecast, not a verified action result.
Old skipped jobs are not reopened; only future unclaimed work is recalculated.

Actual first restart claim: batch67 began22:19:26.761824,540.517182seconds after
batch66's full finish. PID9936 was refreshing counters at22:19:37, no pause or
active uncertainty; spend374 remains unchanged before the new attempts.
There was no overlap or early start. This proves resume admission and the
new16-attempt claim, not completion of that still-running batch.

Private backup/receipts: `C:\Users\vk-bot\vk_backups\api-date-20261101`.
Local receipt copies: `outputs/api-budget-dates-2026-11-01`.
