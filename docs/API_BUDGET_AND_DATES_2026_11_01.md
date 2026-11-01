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

Status: source repair verified locally and ready for publication. Running VM
process3084 remains unchanged and is not stopped,
restarted or hot-patched. Production histories, quotas, bindings, saved claims
and paused states are untouched. Activation requires a separate safe process
transition; publication alone does not install new code into a running process.
