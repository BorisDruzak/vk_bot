# Public read diagnostics implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Explain public read/access failures with safe phase/timing/error evidence, including a helper killed at the existing20second deadline.

**Architecture:** Keep urllib transport, TLS verification, shared ledger/cache, pacing and timeout unchanged. The disposable HTTP-only helper emits allowlisted numeric progress on stderr; parent captures it even on timeout and attaches a sanitized summary to exceptions. Prebatch refresh forwards bounded read diagnostics into the existing scheduler event store. No keys, URLs, bodies, headers, DNS names or raw exceptions are logged.

**Tech Stack:** Existing Python3.13stdlib subprocess/socket/ssl/http.client/urllib; no new dependency.

**Spec:** User26October: add logging explaining timeout/network accessibility; advise separately on insufficient action time.

## Constraints and review focus

- Keep20second overall deadline,5second API pacing/5000ledger,guards/third-failure policy and no partial pools.
- Network observers delegate original calls exactly; restore wrappers, never change certificates/proxies, never issue probe/retry HTTP.
- Diagnostics callback failures must not change result/exception or reserve actions.
- Capture startup/import, DNS,TCP,TLS,send,headers,body/JSON and safe errno/winerror/certificate/status codes; unknown remains unknown.
- Success is compact; failure retains bounded phase evidence. CLI stdout protocol remains compatible.
- Implement logging only; do not change action-time selection or heartbeat UI in this request.
- Source-only local/VM tests; install at idle batch boundary using existing task and protected-state backup. No unpause/history/plan changes.

## Task

- [x] RED: synthetic child deadlines, network classifications, redaction, loopback success and observer restoration.
- [x] Implement helper observer/protocol, parent summaries/exception data and scheduler integration.
- [x] GREEN: targeted and full source-only suites local/VM; independent review.
- [x] Backup/atomic installation and graceful idle restart of existing worker; prove unchanged protected state and verify logging.
- [x] Update context/report and explain time-budget recommendation without changing that policy.

Ruling: checkout has no.git; compare saved source baselines and VM hashes. No GitHub publication.
