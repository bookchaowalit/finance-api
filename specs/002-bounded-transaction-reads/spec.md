# SPEC-002 — Read only the transactions a view needs

Standard: ADR-0004 — TDD + SDD. Uses the cells' SPEC-007 date-range filters (live 2026-11-11 in all ten finance cells).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-002 / 1.0 |
| Status | approved (2026-11-11, see Approval) |
| Repository / path | bookchaowalit/finance-api / `specs/002-bounded-transaction-reads/spec.md` |
| Owner (maker) | Claude Code (session "ระบบบัญชีหลังบ้าน", 2026-11-11) |
| Reviewer (checker) | Codex review Stop hook + CI gates |
| Approver | owner, in chat 2026-11-11 ("เปลี่ยน finance-api มาใช้ filter ใหม่เลย") |
| Main-only | yes |

## 1. Problem and intent

- **Problem:** `/v1/overview` and `/v1/budget-vs-actual` read every transaction of the workspace (paged, capped at
  10 000 rows) and drop the ones outside the month in Python. A workspace with a few years of history hits the cap,
  and the month's totals are then computed from an incomplete set (`truncated: true`).
- **Outcome:** these two views ask the transactions cell only for the days they use, with
  `transaction_date_from` / `transaction_date_to` (SPEC-007).

## 2. Scope

| In scope | Out of scope |
|---|---|
| The transactions window of `/v1/overview` and `/v1/budget-vs-actual` | `/v1/net-worth` (a balance needs every transaction; it stays unbounded); other sources (small, already filtered by status); view arithmetic (unchanged) |

## 3. Behaviour

| ID | Given / When / Then |
|---|---|
| AC-01 | **Given** a month M and no plan reaching outside M, **then** transactions are read with `transaction_date_from` = first day of M and `transaction_date_to` = last day of M. |
| AC-02 | **Given** an active budget or cash-flow plan that overlaps M and starts before (or ends after) M, **then** the window widens to that plan's start (or end), so the plan's `spent` / `actual_*` stay the same as before. |
| AC-03 | **Given** such a plan with no `period_start` (or no `period_end`), **then** that side of the window is left open (the parameter is not sent). |
| AC-04 | **Given** the plans cannot be read, **then** the transactions window is just M (the plan sections already report the error). |
| AC-05 | **Given** any view, **then** its numbers equal those computed from all transactions (the views still filter by date themselves). |
| AC-06 | **Given** `/v1/net-worth`, **then** transactions are read with no date bounds. |

## 4. Interfaces and data

| Interface | Change |
|---|---|
| Calls to the transactions cell | + `transaction_date_from`, `transaction_date_to` (dates, `YYYY-MM-DD`) on overview and budget-vs-actual |
| `views.transaction_window(plans, month)` | new, pure: `(start | None, end | None)` |
| Latency | plans are read first, then transactions (two rounds instead of one for these two views) |

## 5. Tests (red → green)

`tests/test_views.py` (window: AC-01–AC-03) and `tests/test_api.py` (the parameters sent: AC-01, AC-04, AC-06; totals unchanged: AC-05).

## 6. Rollout and rollback

Merge to `main` deploys (Railway GitHub source + Wait for CI); check `/health` and one overview call. Rollback: revert;
the cells keep accepting unbounded lists.

## Approval

Owner in chat 2026-11-11: "เปลี่ยน finance-api มาใช้ filter ใหม่เลย".
