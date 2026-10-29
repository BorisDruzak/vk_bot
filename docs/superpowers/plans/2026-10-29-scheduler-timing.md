# Scheduler timing diagnostics — implementation plan

**Goal:** implement issue #4, diagnostic effective windows, stage distributions,
and deterministic offline simulation without changing scheduling permission.
**Spec:** https://github.com/BorisDruzak/vk_bot/issues/4
**Baseline:** cb8ec8f20a148a247bed9736d65c6b51748699f4.
**Architecture:** a pure timing/report module; buffered monotonic stage samples;
observations saved after batch completion; read-only panel report. Offline
simulation uses temporary databases, FakeClock, real Worker/State guards and
synthetic action transports. Existing clean publication clone isolates work
from original runtime files and the VM. Python stdlib, existing unittest/Node.

## Constraints and decisions

- Preserve saved slots/policy, 540s spacing, 20s cooldown, B-120/60/20, HTTP20,
  retry5/10, quotas, actor ledgers, singleton/activity locks, no-catchup.
- Diagnostic estimates never authorize or suppress an action. Unknown is null.
- Record only allowlisted stages, numbers and aware timestamps, no private data.
- No VM deployment or worker changes in this task; installation needs its own window.
- The detailed issue is the chosen design. Additive observation is the only
  approach within scope; changing scheduling or predictive admission is excluded.
- Parallel independent implementation: simulator and action timing have separate
  file ownership; primary agent owns effective-window/report integration and UI.

## Tasks / verification ledger

- [x] Pure effective_window(S,N,E, observations): deadlines, remaining time,
  multiple cause flags, explicit unknowns. Tests at S+120, N-120/60/20,
  midnight and final slot; no mutation of supplied records.
- [x] Stage timing: browser_open, source_navigation, source_guard,
  community_prepare, click, confirmation, cooldown. Monotonic buffered spans,
  failed spans distinct from complete; measured median/p95 with sample coverage.
  Test exceptions/telemetry failures preserve original intent/cooldown semantics.
- [x] Observe actual claim/refresh/spacing/first durable intent and batch result;
  persist diagnostic rows separately after finish, avoid writes inside intent.
  Read-only report includes untouched historical/missed slots and reasons.
- [x] Simulator: fixed saved80 slots, reproducible09:29 expected3/4, measured
  preparation47–48 and cooldown20, spacing dependency, long batch/late claim,
  stops and before/after intent failures, API timeout/retry budget. No network.
- [x] Panel: readable per-slot timing/completeness and stage distributions,
  explicit320 theoretical attempts versus verified; desktop/mobile UI checks.
- [x] Focused RED/GREEN tests, full offline suite, independent final review;
  update context/architecture and release evidence, commit/push main per user.

## Review focus

Missing historic telemetry; diagnostics failing during intent; clock reversal;
partial/failed stage samples; skipped slots after blocking work. Each must remain
unknown or explain existing behavior, never produce new execution permission.

## Execution evidence

All six tasks completed. First full run981PASS; final985PASS/0skip/265.334s.
Final reviewer found no blockers after RED/GREEN fixes for malformed telemetry,
current deadline reporting, quota explanation and failed-refresh timestamp.
Playwright MCP UI fixture80rows:1440×1100 and390×844, no page overflow,
working disclosure/table scroll, explicit unknown median/p95. No VM mutations.
Ruling: use existing clean publication clone as source-only isolation; detailed
issue supplies the selected diagnostic design and main-publication authorization
persists from the user. No additional schedule parameter selection is inferred.
