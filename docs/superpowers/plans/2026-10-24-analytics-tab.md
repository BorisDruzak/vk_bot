# Analytics tab — implementation and verification

User selected integration in the existing VM panel: latest measurements,
ER/P3.3 and three publications per week for all64communities, no Codex.

Chosen design: same authenticated panel/session; GET-only SQLite reader,
existing analytics database mode=ro, no VK requests, worker or actor mutations.
New tab uses existing visual style; filters for measurement, calendar/week
from14Oct, and repost inclusion. Show partial-data coverage and provisional
engagement explicitly; never convert missing counters into zero. Weekly
minimum is proven by available IDs, unknown archive cannot prove failure;
open weeks never fail. Each community can show exact dated post links.

- [x] RED/GREEN backend/HTTP tests: readonly/missing DB, historical selection,
  partial/NULL, calendar versus period, business cards, open weeks as of
  measurement time, auth/CSRF/query allowlist and fixed errors.
- [x] Add vk_analytics_web.py, protected API in vk_web.py and narrow wiring.
- [x] Add tab/analytics.js and scoped styles, refresh/filters/details, stale
  handling, logout clearing, preserve other tabs/forms. No new dependencies.
- [x] Isolated tests, browser desktop/mobile, bad/empty/stale recovery, review.
- [x] VM hashes/backup; install only scoped files. Restart web panel only,
  never worker. Verify native panel/read-only API and worker preservation.
- [x] Record education discrepancy: frozen morning IDs2149(19Oct)/2150(23Oct),
  new public2152(today after snapshot); do not rewrite old frozen data.

GitNexus has no vk_bot index; local checkout has no.git. Current shared public
API v6.34 is preserved. Python3.13 sqlite3 URI/closing verified with Context7.
Browser plugin absent; use installed Playwright MCP for browser validation.
