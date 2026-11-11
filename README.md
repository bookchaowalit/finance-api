# finance-api

The finance suite's entry point: the views that need several finance cells at once, answered in one call
(SPEC-001). It holds no data and no signing key. It checks the caller's token, forwards the same bearer to
each cell (every cell still enforces its own permissions and workspace isolation), and computes the view
with exact decimals per currency.

| Path | View |
|---|---|
| `GET /v1/overview?month=YYYY-MM` | accounts, income/expense, budget vs actual, cash-flow plan vs actual, subscriptions (monthly cost, renewals in 30 days), liabilities, investments, open tax items |
| `GET /v1/budget-vs-actual?month=YYYY-MM` | active budget plans covering the month: planned, spent in their category and period, remaining |
| `GET /v1/net-worth` | per currency: accounts (opening balance ± their income/expense), investments, business assets, minus liabilities |
| `GET /health` | `{"status": "ok", "commit": ...}` |

Each section is `{"ok": true, ..., "truncated": false}` or `{"ok": false, "error": "timeout|forbidden|unreachable|cell_error"}`:
one cold or failing cell blanks only its own sections. Writes go straight to the cells.

## Token

`aud` must contain `finance-api`. The issuers mint a *finance bundle* (`finance-api`, the ten cell audiences,
`platform-api`), so the same bearer is valid at every cell it is forwarded to.

## Configuration

| Variable | Meaning |
|---|---|
| `FINANCE_API_JWT_ISSUER` | main issuer (`empire-platform`) |
| `FINANCE_API_JWT_PUBLIC_KEY_PEM` or `FINANCE_API_JWT_JWKS_URL` | its verification key |
| `FINANCE_API_JWT_IDENTITY_ISSUER` + `FINANCE_API_JWT_IDENTITY_JWKS_URL` | optional second issuer (platform-identity), both or neither |
| `FINANCE_API_CELLS` | optional JSON `{"<cell>": "https://..."}` overriding the production URLs |
| `FINANCE_API_TIMEOUT_SECONDS` | per cell request, default 20 (cells may cold-start) |
| `GATEWAY_ASSERTION_MODE`, `GATEWAY_ASSERTION_KEY` | the edge gateway assertion check (`off`/`report`/`enforce`); set by the gateway owner only |
| `SENTRY_DSN` | optional; events are scrubbed |

## Develop

```bash
uv sync --extra dev
.venv/bin/pytest -q --cov
.venv/bin/mypy
uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .
```
