# SPEC-001 — finance-api: one entry point and the cross-cell views of the finance suite

Standard: ADR-0004 — TDD + SDD. Phase 4 of the finance SaaS plan (2026-11-11).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-001 / 1.4 |
| Status | approved (2026-11-11, see Approval) |
| Repository / path | bookchaowalit/finance-api / `specs/001-finance-bff/spec.md` |
| Owner (maker) | Claude Code (implementer session, 2026-11-11) |
| Reviewer (checker) | Codex review Stop hook + CI gates |
| Approver | owner, in chat 2026-11-11 ("เริ่มระยะ 4"; plan approved earlier: BFF + one token) |
| Main-only | yes |

## 1. Problem and intent

- **Problem:** a finance screen ("this month", "budget vs actual", "net worth") needs five to ten finance cells.
  Each client would repeat the fan-out, the paging, the per-currency exact sums and the partial-failure
  handling, and needs a token for each cell.
- **Outcome:** `finance-api` answers those views in one call. It holds no data and no signing key: it checks
  the caller's token, forwards the same bearer to each cell (every cell still enforces its own permissions
  and workspace isolation), and computes the view with exact decimals per currency.
- **One token:** the issuers mint a *finance bundle* token whose `aud` holds `finance-api`, the ten cell
  audiences and `platform-api` (agreed with the identity session; they add the bundle on empire-platform and
  platform-identity). finance-api refuses tokens without `finance-api` in `aud`.

## 2. Scope

| In scope | Out of scope |
|---|---|
| `GET /v1/overview?month=YYYY-MM`, `GET /v1/net-worth`, `GET /v1/budget-vs-actual?month=YYYY-MM`, `GET /health`; token verification (empire-platform issuer, optional identity issuer); fan-out with timeouts and paging caps | Writes (clients write to the cells); currency conversion; caching; a read model fed by events (later, when load needs it); minting tokens |

## 3. Behaviour

| ID | Given / When / Then |
|---|---|
| AC-01 | **Given** no token, a bad signature, a wrong issuer or no `finance-api` in `aud`, **then** 401 and no cell is called. |
| AC-02 | **Given** a valid token, **then** every cell request carries that same bearer and nothing else that identifies the caller; finance-api never adds a workspace or actor. |
| AC-03 | **Given** `/v1/overview?month=2026-11`, **then** the answer has sections `transactions` (income and expense per currency in the month), `budgets` (each active plan covering the month: planned, spent by category in its period, remaining), `cash_flow` (active plans covering the month: expected vs actual), `subscriptions` (monthly cost per currency, renewals in the next 30 days), `liabilities` (outstanding per currency), `investments` (value and cost per currency), `tax` (open items: due minus paid) and `accounts` (active accounts). |
| AC-04 | **Given** a cell that times out, errors or refuses, **then** only its sections carry `{"ok": false, "error": "<class>"}`; the others are complete; the HTTP status stays 200. A 401/403 from a cell is reported as `forbidden` for that section. |
| AC-05 | **Given** amounts, **then** sums are exact decimals per currency (no float), returned as strings; currencies are never mixed. |
| AC-06 | **Given** more rows than one page, **then** finance-api pages with `after_id` up to a cap (default 10 000 rows per resource) and marks the section `truncated: true` past it. |
| AC-07 | **Given** `/v1/net-worth`, **then** per currency: accounts (opening balance + income/refund − expense/fee of their transactions), investments (current value), business assets (total value) minus liabilities (outstanding); transfers and adjustments are listed as not counted. |
| AC-08 | **Given** `month` that is not `YYYY-MM`, **then** 422. |
| AC-09 | **Given** no `FINANCE_API_CELLS`, **then** every cell is called through the edge gateway `https://gateway.bookchaowalit.com/v1/<cell>` (Cloudflare WAF, rate limit and the signed `X-Gateway-Assertion`), never a cell's public `*.up.railway.app` origin, so the cells can run `GATEWAY_ASSERTION_MODE=enforce`. A base URL keeps its path. |
| AC-10 | **Given** one subject (actor + workspace) over `FINANCE_API_RATE_PER_MINUTE` (default 12) requests in a sliding minute, **then** 429 `{"error": "rate_limited"}` with `Retry-After`, and no cell is called; other subjects and `/health` are unaffected. At most `FINANCE_API_MAX_INFLIGHT` (default 16) cell requests run at once per process. |

## 4. Interfaces and data

| Interface | Contract |
|---|---|
| Env | `FINANCE_API_JWT_ISSUER` + `FINANCE_API_JWT_PUBLIC_KEY_PEM` or `_JWT_JWKS_URL`; optional `FINANCE_API_JWT_IDENTITY_ISSUER` + `_JWT_IDENTITY_JWKS_URL`; optional `FINANCE_API_CELLS` (JSON name → https base URL; defaults are the production URLs); `FINANCE_API_TIMEOUT_SECONDS` (default 20, cells may cold-start); `FINANCE_API_RATE_PER_MINUTE` (default 12); `FINANCE_API_MAX_INFLIGHT` (default 16) |
| Cells | `GET /v1/<plural>` with filters and `after_id` paging, as the cellkit list API |

## 5. Non-functional requirements

| NFR | Target | How it is tested |
|---|---|---|
| Security | no data at rest; the bearer goes only to the configured https cell URLs; never logged | AC-01/AC-02 tests; URL check at start |
| Latency | cells in parallel; one slow cell bounds only its own sections | AC-04 test |
| Correctness | exact decimals; per currency | AC-05 tests |

## 6. Test plan

- Red first: tests in `tests/` fail before `src/` exists.
- Levels: unit (views from fixed rows), integration (FastAPI app with an `httpx.MockTransport` standing in for the cells).
- Red/green log (2026-11-11): red `.venv/bin/pytest -q` with the tests and no `src/`: 2 collection errors (modules missing); green `.venv/bin/pytest -q --cov`: 39 passed, total coverage 94.93% (CI gate 92); `mypy --strict` and `ruff` clean.
- Red/green log AC-09/AC-10 (2026-11-11, security review P2): red `uv run pytest -q`: 4 failed, 39 passed (gateway defaults, rate limit, inflight bound, env); green: 43 passed, coverage 95%; `mypy --strict src` and `ruff` clean. Parity before the switch: finance-accounts and transactions returned the same status and ids direct and through the gateway.

## 7. Risks

| # | Risk | Resolution |
|---|---|---|
| 1 | Cold starts: ten serverless cells waking at once | parallel calls, 20 s timeout, partial answers |
| 2 | No date filters on the cells yet, so a month view reads all transactions | page cap + `truncated`; add range filters to cellkit in phase 5 |
| 3 | The finance bundle audience does not exist yet on the issuers | finance-api answers 401 until it does; the identity session adds it with owner approval |

## 8. Change log

| Version | Date | Change | Approved by |
|---|---|---|---|
| 1.0 | 2026-11-11 | first version | owner, chat 2026-11-11 |
| 1.1 | 2026-11-11 | Phase 5: finance-api checks the edge gateway assertion on its own routes (`gateway.py`, byte-identical copy of platform-notifications-api except `SYSTEM = "finance-api"` and the logger; `GATEWAY_ASSERTION_MODE`/`_KEY` set by the gateway owner, off until then). Tests copied with the module (no separate red run); green: 75 passed, coverage 96%. | owner, chat 2026-11-11 ("เริ่มระยะ 5") |
| 1.2 | 2026-11-12 | Codex review: a budget or cash-flow plan counts only transactions of its own `scope` (personal vs business); a page the gateway refuses as too large (502, 1 MiB cap) is read again in halves and the rest of the read keeps that size (a 502 at one row stays `cell_error`); `gateway.py` synced to the reference's v2 (open `/healthz`, `/ready`, a trailing slash; a stalled body gets 408). Red: `test_plans_count_only_transactions_of_their_own_scope`, `test_a_page_too_big_for_the_gateway_is_read_in_smaller_pages` (and the two copied gateway tests on the old module); green: 126 passed, coverage 95%. | owner, chat 2026-11-11 |
| 1.3 | 2026-11-12 | Security review: only the gateway's `upstream response too large` 502 halves the page; any other 502 (a cell that is down) is `cell_error` after one call, never a burst of retries. Red: `test_a_cell_that_is_down_is_not_retried_in_smaller_pages`; green: 130 passed, coverage 95%. | owner, chat 2026-11-11 |
| 1.4 | 2026-11-12 | Codex review: one 25-second deadline covers a whole view (the plans, then the transactions read gets what is left), under the edge gateway's 30 s; a source still out is a `timeout` section error, not a 504. Red: `test_a_month_view_answers_inside_one_view_deadline`; green: 134 passed. | owner, chat 2026-11-11 |

## Approval

| Gate | Role | Name / date | Decision |
|---|---|---|---|
| Approved to implement | Approver | Owner, chat 2026-11-11 ("เริ่มระยะ 4") | approved |
