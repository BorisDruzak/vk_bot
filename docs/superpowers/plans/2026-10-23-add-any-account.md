# Add any VK account implementation plan

Goal: add a browser connection without preassigning VK ID to UI slots1/2.
Architecture: isolated persistent profiles choose an existing internal actor by
the freshly observed VK ID, or create a new actor. Historical actor IDs, ledgers,
budgets and safeguards stay unchanged. Only profile connections are replaced.
Stack: Python, Playwright1.x, SQLite, stdlib HTTP, Windows VM.

- [x] Reproduce failed window launch; shared reader leases allow concurrent
  profiles while existing worker retains exclusive batch lease. OS tests first.
- [x] Add explicit flexible-flow migration: disabled legacy cards become history
  archive, never delete their bindings, databases or ledgers. New slots unbound.
- [x] Test automatic connect: arbitrary new ID; existing ID reuses its actor/DB;
  a different ID never inherits prior history/quotas; enabled targets reject.
- [x] Resolve profile pointer and physical profile lock together. Live action
  identity guard keeps using the internal actor's exact ID.
- [x] VM login worker waits for busy batch without stopping it, reports browser
  wait honestly, auto-saves detected identity and preserves profile cache.
- [x] UI add-any flow hides fixed numbered bindings/finish, supports names and
  polls completion/redirect once. Preserve legacy flow when opt-in absent.
- [x] Focused/full offline tests, independent review, backup before migration,
  install on VM, real button/browser-window validation and docs/context update.

No real engagement actions, no reset/unpause/isolation changes. User's explicit
request changes the UI account-selection policy; exact actor checks before real
actions remain required. No Git publication: current source folder has no .git.
