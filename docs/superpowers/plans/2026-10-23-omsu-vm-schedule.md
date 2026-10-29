# OMSU VM Schedule Implementation Plan

**Goal:** Run the existing public analytics on the primary VM at08:00/20:00
YEKT while Codex is closed, without disturbing the live worker.

**Architecture:** Separate Windows Scheduled Task, same limited VM account,
S4U noninteractive login, existing VM Python and key. Dedicated Python entry
uses existing analytics reader/store/calculation. Existing worker unchanged.

**Spec:** User requested VM-native schedule and priority for the worker;
period/registry from docs/OMSU_OBSERVATION_FROM_14.md.

## Constraints and decisions

- Keep start14.10.2026, frozen64-group registry, append-only snapshots/reports.
- Daily08:00/20:00, accept starts only in first15minutes of those hours;
  check deadline before each request, at most25minutes, stop by08:40/20:40.
- No catch-up/restart attempts; IgnoreNew, background priority8. Existing
  nonblocking activity/profile guards and global API ledger/pacing remain.
- Worker busy: yield immediately. No worker task/process/state mutations.
- Slot checkpoint survives interruption; duplicate completed slot is no-op.
- Do not retry API errors; unavailable posts and missing counters remain NULL.
- Existing Codex heartbeat14 is paused only after native task verification.
- Local checkout has no.git and vk_bot has no GitNexus index; preserve dirty
  unrelated files, no commit/push. No new dependencies.

## Tasks

- [x] RED tests: outside slot and deadlines prevent API; frozen registry;
  actual worker/analytics locks; interrupted slot resume; no duplicate run,
  snapshots/report; API errors never retry; raw export preservesNULL.
- [x] Implement vk_analytics_scheduled.py using existing reader/collector.
- [x] Register scripts/vm/register-analytics-task.ps1 disabled S4U task;
  parse/verify task settings, then enable after preflight checks.
- [x] Isolated VM suite; independent review of scheduler and interference.
- [x] Native S4U preflight: runtime/key/TLS/files; normal manual daytime
  start must skip without API or actor changes. Remove only preflight task.
- [x] Enable native task; pause Codex14 with full original fields preserved.
- [x] Update docs/context and capture task XML/status/next run/last results.

## Results and rulings

78 isolatedVM tests PASS; review3crash/deadline findings reproducedRED and
fixedGREEN. Deterministic validated run_id adds only optional store behavior;
existing calls stay unchanged. Reader callback is optional and checks after
pacing. Native task registered/enabled S4U/Limited, key/TLSpreflight0API,
ordinary daytime skip LastTaskResult0. Next full run23Oct20:00YEKT.
Heartbeat14 PAUSED. Protected comparison: only scheduler heartbeat changed.

Ruling: system watchdog30min is grace-only (DisallowHardTerminate), because
force-killing existing conservative O_EXCL profile guard could leave a stale
file that blocks the worker. Python25min deadline stops new transport calls
and bounded current calls leave through existing finally cleanup.

## Review focus

Clock crossing cutoff; reader lock races; crash after run/checkpoint or
report freeze; secret-safe unattended errors; task catch-up/user-logon modes.
Acceptance distinguishes verified native preflight from the first future
full scheduled measurement.
