# Review guide (finance-api)

The Codex Stop-hook review reads this file from the default branch, never from the change under
review. Owner-written; keep it under 16 KiB.

## What failure looks like here

Treat these as `[P0]`/`[P1]`:

- **Money**: amounts exact decimals per currency, returned as strings, never float, never added
  across currencies (`specs/001-finance-bff/spec.md`).
- **Statelessness towards the cells**: finance cell requests carry only the caller's bearer; no
  workspace, actor or token of finance-api's own (`AGENTS.md`, spec 001). Not a finding: billing
  calling platform-credits with finance-api's own service token and the in-memory pass-state cache
  (`specs/005-monthly-pass/spec.md` §3, AC-06); the customer's token must never reach credits.
- **Gateway bypass**: with no `FINANCE_API_CELLS` override, a cell called anywhere but through the
  gateway (never `*.up.railway.app` by default); `X-Edge-Bypass` sent to any host but the gateway, or
  shorter than 32 characters (`specs/003-edge-rate-limit-bypass/spec.md`). An override to test or mock
  https origins in tests is allowed (spec 001 AC-09).
- **PDPA** (`specs/004-pdpa-export-erase/spec.md`): an export is complete or refused with 502
  `export_incomplete`; a second concurrent export on a replica is 503 `export_busy` with
  `Retry-After` (correct, not a finding); erase needs `confirm` equal to the caller's workspace and
  runs its 3 stages within 25 s.
- **Partial failure**: a failing cell fails every section that depends on it (e.g. `transactions`
  also fails `budgets` and `cash_flow`) and no other section (response stays 200); the 10 000-row
  page cap marks the section `truncated`.
- **Copies out of step**: `gateway.py` (byte-identical to platform-notifications-api's), `auth.py` and
  `_sentry.py` (cellkit copies) changed here alone.

Merging to `main` deploys production (Railway Wait for CI).

## Severity

- `[P0]` wrong money totals, data from another workspace, gateway bypassed, erase of the wrong workspace.
- `[P1]` a view that errors or misreports for real callers.
- `[P2]` edge case, or changed behaviour without a test.
- `[P3]` nit. **At most 3 nits.**

## Skip

- `uv.lock`.

## Evidence

Every finding gives `file:line` and input -> wrong result. No finding without evidence from the diff
or the code it calls. Start the summary with the count, e.g. `2 findings (1 P1, 1 P3)`.
