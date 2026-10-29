# Typed chooser timeout retry

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Put the specific initial community-option timeout before send into the existing one-retry/three-logical-failures policy.

**Architecture:** A fixed CommunityPreparationTimeout(TimeoutError) is emitted only for the initial30second option wait. Before propagating it, prepare rechecks identity and exact source. Only its exact type/stage/reason on a community job with no intent/attempt is retryable. Ordinary StopRun/guards, later selection errors, cleanup failures and post-intent cases remain immediate stops. No blanket community_prepare allowlist.

**Tech Stack:** Existing Python/Playwright; no new API/dependency or altered timeout/selectors.

**Spec:** User «делай» after guarded631proof and explanation of classifier gap.

## Constraints/review

- One retry/two passes; third failed logical action stops; success resets streak.
- Real attempts alone spend existing quota and<=4 batch places; no new send after durable intent.
- Same20sec cooldown/540sec batches/API5sec/5000/day/window09–19/no catch-up.
- Existing job631/interrupted pass/paused/history are not migrated or reopened.
- Source-only checks and guarded install; preserve current pause and existing Windows task.
- Check positive timeout/retry/counter and negative wrong-account/source/guard/cleanup/intent cases.

- [x] RED tests for typed wait and retry ledger behavior.
- [x] Minimal classifier/UI/diagnostic changes.
- [x] Full source-only local/VM tests and independent review.
- [x] Backup, install, preserve pause/state, verify installed classification.
- [x] Update docs/context and report live-acceptance limits.

Ruling: local checkout has no.git; compare saved baselines and VM hashes; no GitHub publication.
