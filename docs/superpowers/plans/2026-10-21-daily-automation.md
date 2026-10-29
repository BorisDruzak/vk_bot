# Daily VK automation implementation plan

**Goal:** Continuous local worker with API refresh, calendar-day quotas and sequential batches in Asia/Yekaterinburg.
**Architecture:** Existing verified action UI remains the execution layer. A pure policy module computes local day/window and 40 slots at 15min with persisted jitter±120s and unique gaps; an additive scheduler ledger records jobs before dispatch and adopts durable action attempts. The runner selects one action and exact API source at a time. No simultaneous like/repost and no automatic retry of uncertain sends.
**Spec:** User request and clarifications on 2026-10-21. Two independent own profiles: each 50 likes, 10 personal reposts, 20 community reposts/day; community budget is 10 per each configured group (four existing groups). New posts are published on current YEKT day; reserve previous 7 days. Batches <=4 across both accounts. Window 09:00 inclusive to19:00 exclusive; daemon survives restarts, waits at night. Mandatory >=20s action cooldown, plus existing read pacing. Count existing manual attempts today. Late launch does not create catch-up bursts or extend window automatically.

## Tasks
- [x] API key loader: environment precedence, local api_keys.txt fallback, no secret logging/tracking; fresh all-group discovery with explicit finite read budgets and coverage, regression tests and real read-only scan.
- [x] Pure policy: calendar boundaries, quotas and targets,40 batch slots, new/old ordering, no off-window action, tests.
- [x] Persistent scheduler/runner: import usage from account ledgers, durable job reservations, one action at a time with final-click window/quota guard, crash ambiguity fail closed, CSV/status, tests.
- [x] CLI, local daemon/task installation, scoped safe test, documentation and local commit. Start24h worker waiting for next work window and heartbeat monitoring with notification only on changed failure/completion.

## Verification
API token never printed or copied into source/control. Temporary SQLite and local Chromium fixtures; existing87 tests. Live API scan all enabled groups. Scheduler batches tested with fake clock in-window; current real clock outside work window means no engagement clicks in acceptance. Retain existing actual four community publish evidence. No GitHub publication requested.

User clarification:50likes EACHaccount,community10eachfourgroups,total160/40 batches. Finalwindow9–19, randomizeddailyoffsets, sourceengagementcounts lowestknownsum withinnew/oldbands.

Acceptance2026-10-21:57APIheads(56complete/1partial invalid-items); realfile-key+countsverified;6browserpathsNOactions,workingledgers73/64 unchanged,jobs0.208offline+1compatibilityPASS. WindowsTask and30minCodexheartbeatactive ONLYdry-run by latestuserinstruction. A full live dailycycle is not accepted/tested.
