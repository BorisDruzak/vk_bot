# Shared public data implementation plan

> **For agentic workers:** Use TDD and focused parallel ownership; one independent final review.

**Goal:** Share VK wall reads across analytics, queue and web without losing history or action freshness.
**Architecture:** VKReadClient delegates opt-in wall.get to a common append-only page store with OS single-flight and observed timestamps. Existing operational tables are projections; analytics snapshots remain frozen per run.
**Tech Stack:** Existing Python/SQLite/OS leases; no new dependency.
**Spec:** docs/SHARED_PUBLIC_DATA_V6_34.md

## Global constraints

- API5000 rolling24h,>=5s; actual actions>=20s; batch<=4; starts>=600s;09–19YEKT.
- No action/history/quota/plan resets; two original isolations and v6.33 retry rules retained.
- Keep analytics schedule08/20 and working-window skip/deadlines; no extra live reactions.
- Preserve unrelated ongoing analytics scheduling work and local diffs; no .git here.

## Review focus

- Timestamp on cache reuse must remain original, including analytics snapshots.
- Fresh-after batch bound must reject stale cache, even if TTL permits ordinary reads.
- Concurrent consumers must issue one HTTP call; errors/429 must never become cache success.
- Legacy stores and frozen reports must remain byte/logically unchanged by activation.
- Non-wall.get probe/custom transports must never poison wall.get cache.

### Task1 shared store

Files: new vk_public_data.py, tests/test_public_data.py.
Interface: PublicPages(path).get(group_id,count,offset,fetch,*,max_age_seconds=300,fresh_after=None,clock=time.time).
fetch() returns successful wall payload. get returns same payload plus
_observed_at (UTC ISO),_observation_id(int),_cache_hit(bool). lookup(...) returns
same cached payload or None without network; missing schema/file returnsNone.
Cache identity namespace wall.get5.199/owner, group/count/offset. `.guard` OS lease
double-checks cache, append-only page/valid post observations, no credential/DOM.
- [x] RED tests reuse/time/expiry/freshafter/concurrency/invalid/error/NULL/rollback.
- [x] Implement, focusedGREEN, report safe interface.

### Task2 API and operational consumers

Files: vk_api_read.py, vk_batch_engagement.py, vk_statistics_refresh.py,
vk_statistics_job.py; integration tests.
VKReadClient kwargs shared_path=None,max_age_seconds=300,fresh_after=None;
None resolves only `.vk-public-data.json` under ledger DB parent, enabledversion1.
False explicitly disables sharing (by-id probe). Existing constructor unchanged.
has_cached_wall(group_id,*,count=100,offset=0) permits budget-free cached reads.
_wall_get_network retains all reserve/pacing/rate handling. Cache returned only
after platform guard. Worker passes fresh_after=start; all projections persist
_observed_at rather than current time; no fallback on budget/unknown counters.
- [x] RED cross-consumer one-network/budget/pacing/guard/freshness tests.
- [x] Implement operational projections and accurate request metrics.

### Task3 analytics integration

Files: vk_analytics_collect.py and new tests/test_shared_analytics.py.
Use shared VKReadClient wall.get by default through runtime activation; pass
shared_path=False for wall.getById custom transport. collect_run uses payload
_observed_at for snapshots (legacy fallback store.now); preserve snapshots onresume.
Per-group continuation requires observations no earlier than head. Keep scheduled
before_request callback and08/20 deadlines unchanged; preserve unpublished work.
- [x] RED analytics cached snapshot originaltime/history/probe/schedule tests.
- [x] Implement and verify analytics regression suite.

### Task4 acceptance/deploy

- [x] Independent review and isolated full tests, including Windows single-flight.
- [x] Backup VM; one graceful update stop, preserve all DBs/schedules/attempts.
- [x] Install source and enable runtime store; verify real API read reused by all
  three consumers with one ledger charge, history unchanged, no action clicks.
- [x] Resume one worker, inspect next scheduled batch; update docs/context and
  distinguish deployment from GitHub publication.

Acceptance:543local/543isolatedVM(skip10Node),73independentreview;real100posts HTTP1/0/0 ledgerdelta1;slot31fourverified. See docs/VALIDATION_V6_34.md for exact raw-page fallback boundary and UNKNOWN rejection cause.
