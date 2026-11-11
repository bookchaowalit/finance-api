# Product

<!-- impeccable:product-schema 1 -->

## Platform

api

## Users

- The finance suite web app (`finance-suite-web`, planned) and other server-side clients that show a
  finance dashboard: they call finance-api once instead of five to ten finance cells.
- Callers hold a finance bundle token (`aud` = `finance-api` + the ten cell audiences + `platform-api`)
  from `empire-platform` or, after cutover, `platform-identity`.

## Product Purpose

The cross-cell views of the finance suite: this month's income and expense, budget vs actual, cash-flow
plan vs actual, subscription cost and renewals, liabilities, investments, open tax items, and net worth.
Every amount is an exact decimal per currency.

Success: a dashboard renders from one call; a cold or failing cell blanks only its own section.

## Positioning

A stateless view layer (SPEC-001). It owns no data: each cell keeps its own data, permissions and
workspace isolation, and finance-api reads them with the caller's own bearer.

## Operating Context

- Production: Railway serverless project `finance-api` (`/health`), deployed by merging to `main` after CI.
- No database, no stored tokens; errors go to Sentry without personal data.

## Capabilities and Constraints

- `GET /v1/overview?month=YYYY-MM`, `GET /v1/budget-vs-actual?month=YYYY-MM`, `GET /v1/net-worth`, `GET /health`.
- Pages each cell list with `after_id` up to 10 000 rows per resource and marks `truncated` past that.
- Transfers and adjustments carry no direction in the transactions cell, so no view counts them; they are
  listed under `not_counted`.
- Out of scope: writes, currency conversion, caching, minting tokens.
