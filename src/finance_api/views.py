"""The cross-cell finance views, computed from the cells' rows (SPEC-001 AC-03, AC-05, AC-07).

Pure functions: rows in, JSON-ready dicts out. Amounts arrive as exact decimal strings (SPEC-003) and
leave as strings; totals are kept per currency and never converted or mixed.
"""

from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

Row = Mapping[str, Any]
INFLOW = frozenset({"income", "refund"})
OUTFLOW = frozenset({"expense", "fee"})
# Transfers and adjustments carry no direction in the transactions cell, so no view counts them.
UNSIGNED = frozenset({"transfer", "adjustment"})
PER_MONTH = {
    "weekly": Decimal(52) / Decimal(12),
    "monthly": Decimal(1),
    "quarterly": Decimal(1) / Decimal(3),
    "yearly": Decimal(1) / Decimal(12),
}
OPEN_LIABILITY = frozenset({"active", "overdue"})
OPEN_TAX = frozenset({"estimated", "open", "overdue"})
LIVE_SUBSCRIPTION = frozenset({"active", "trial"})
MONEY = Decimal("0.0001")
_MONTH = re.compile(r"(\d{4})-(\d{2})")


def month_bounds(month: str) -> tuple[date, date]:
    """First and last day of ``YYYY-MM``; ValueError otherwise."""
    m = _MONTH.fullmatch(month)
    if not m:
        raise ValueError("month must be YYYY-MM")
    year, number = int(m[1]), int(m[2])
    if not 1 <= number <= 12:
        raise ValueError("month must be YYYY-MM")
    return date(year, number, 1), date(
        year, number, calendar.monthrange(year, number)[1]
    )


def _day(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _amount(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def _text(totals: Mapping[str, Decimal]) -> dict[str, str]:
    return {
        currency: str(total.quantize(MONEY))
        for currency, total in sorted(totals.items())
    }


def _add(totals: defaultdict[str, Decimal], row: Row, field: str) -> None:
    if row.get(field) in (None, ""):
        return
    totals[str(row.get("currency") or "THB")] += _amount(row[field])


def _in(day: date | None, start: date | None, end: date | None) -> bool:
    return (
        day is not None
        and (start is None or day >= start)
        and (end is None or day <= end)
    )


def _covers(row: Row, start: date, end: date) -> bool:
    """A plan whose period overlaps [start, end]."""
    first, last = _day(row.get("period_start")), _day(row.get("period_end"))
    return (first is None or first <= end) and (last is None or last >= start)


def _flows(
    rows: Iterable[Row],
    start: date | None,
    end: date | None,
    category: str | None = None,
    currency: str | None = None,
) -> tuple[defaultdict[str, Decimal], defaultdict[str, Decimal], Counter[str]]:
    income: defaultdict[str, Decimal] = defaultdict(Decimal)
    expense: defaultdict[str, Decimal] = defaultdict(Decimal)
    skipped: Counter[str] = Counter()
    for t in rows:
        if not _in(_day(t.get("transaction_date")), start, end):
            continue
        if category is not None and t.get("category") != category:
            continue
        if currency is not None and str(t.get("currency") or "THB") != currency:
            continue
        kind = str(t.get("transaction_type"))
        if kind in INFLOW:
            _add(income, t, "amount")
        elif kind in OUTFLOW:
            _add(expense, t, "amount")
        else:
            skipped[kind] += 1
    return income, expense, skipped


def transactions(rows: Sequence[Row], month: str) -> dict[str, Any]:
    start, end = month_bounds(month)
    income, expense, skipped = _flows(rows, start, end)
    return {
        "income": _text(income),
        "expense": _text(expense),
        "not_counted": dict(sorted(skipped.items())),
    }


def budgets(
    plans: Sequence[Row], rows: Sequence[Row], month: str
) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    lines = []
    for plan in plans:
        if plan.get("status", "active") != "active" or not _covers(plan, start, end):
            continue
        currency = str(plan.get("currency") or "THB")
        _, spent, _ = _flows(
            rows,
            _day(plan.get("period_start")),
            _day(plan.get("period_end")),
            plan.get("category") or None,
            currency,
        )
        planned = _amount(plan.get("planned_amount"))
        used = spent.get(currency, Decimal(0))
        lines.append(
            {
                "id": plan.get("id"),
                "name": plan.get("name"),
                "category": plan.get("category"),
                "period_start": plan.get("period_start"),
                "period_end": plan.get("period_end"),
                "currency": currency,
                "planned": str(planned.quantize(MONEY)),
                "spent": str(used.quantize(MONEY)),
                "remaining": str((planned - used).quantize(MONEY)),
            }
        )
    return lines


def cash_flow(
    plans: Sequence[Row], rows: Sequence[Row], month: str
) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    lines = []
    for plan in plans:
        if plan.get("status", "active") != "active" or not _covers(plan, start, end):
            continue
        currency = str(plan.get("currency") or "THB")
        income, expense, _ = _flows(
            rows,
            _day(plan.get("period_start")),
            _day(plan.get("period_end")),
            currency=currency,
        )
        inflow, outflow = (
            _amount(plan.get("expected_inflow")),
            _amount(plan.get("expected_outflow")),
        )
        actual_in, actual_out = (
            income.get(currency, Decimal(0)),
            expense.get(currency, Decimal(0)),
        )
        lines.append(
            {
                "id": plan.get("id"),
                "name": plan.get("name"),
                "currency": currency,
                "period_start": plan.get("period_start"),
                "period_end": plan.get("period_end"),
                "expected_inflow": str(inflow.quantize(MONEY)),
                "expected_outflow": str(outflow.quantize(MONEY)),
                "actual_inflow": str(actual_in.quantize(MONEY)),
                "actual_outflow": str(actual_out.quantize(MONEY)),
                "net_expected": str((inflow - outflow).quantize(MONEY)),
                "net_actual": str((actual_in - actual_out).quantize(MONEY)),
                "reserve_target": plan.get("reserve_target"),
            }
        )
    return lines


def subscriptions(rows: Sequence[Row], today: date, days: int = 30) -> dict[str, Any]:
    monthly: defaultdict[str, Decimal] = defaultdict(Decimal)
    renewing = []
    until = today + timedelta(days=days)
    for s in rows:
        if s.get("status") not in LIVE_SUBSCRIPTION:
            continue
        factor = PER_MONTH.get(str(s.get("billing_interval")))
        if factor is not None:
            monthly[str(s.get("currency") or "THB")] += (
                _amount(s.get("amount")) * factor
            )
        if _in(_day(s.get("next_billing_date")), today, until):
            renewing.append(
                {
                    k: s.get(k)
                    for k in (
                        "id",
                        "name",
                        "provider",
                        "amount",
                        "currency",
                        "next_billing_date",
                    )
                }
            )
    return {"monthly": _text(monthly), "renewing": renewing}


def liabilities(rows: Sequence[Row]) -> dict[str, Any]:
    outstanding: defaultdict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        if row.get("status") in OPEN_LIABILITY:
            _add(outstanding, row, "outstanding_amount")
    return {"outstanding": _text(outstanding)}


def investments(rows: Sequence[Row]) -> dict[str, Any]:
    value: defaultdict[str, Decimal] = defaultdict(Decimal)
    cost: defaultdict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        if row.get("status") == "active":
            _add(value, row, "current_value")
            _add(cost, row, "cost_basis")
    return {"value": _text(value), "cost": _text(cost)}


def assets(rows: Sequence[Row]) -> dict[str, Any]:
    value: defaultdict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        if row.get("status", "active") == "active":
            _add(value, row, "total_value")
    return {"value": _text(value)}


def tax(rows: Sequence[Row]) -> dict[str, Any]:
    outstanding: defaultdict[str, Decimal] = defaultdict(Decimal)
    items = []
    for row in rows:
        if row.get("status") not in OPEN_TAX:
            continue
        left = _amount(row.get("amount_due")) - _amount(row.get("amount_paid"))
        outstanding[str(row.get("currency") or "THB")] += left
        items.append(
            {
                k: row.get(k)
                for k in ("id", "tax_type", "due_date", "status", "currency")
            }
        )
    return {"outstanding": _text(outstanding), "items": items}


# The keyword arguments of net_worth() share these names.
_investments_view, _assets_view, _liabilities_view = investments, assets, liabilities


def net_worth(
    accounts: Sequence[Row],
    rows: Sequence[Row],
    *,
    investments: Sequence[Row],
    assets: Sequence[Row],
    liabilities: Sequence[Row],
) -> dict[str, Any]:
    balance: defaultdict[str, Decimal] = defaultdict(Decimal)
    currency_of = {}
    for account in accounts:
        if account.get("status") == "closed":
            continue
        currency = str(account.get("currency") or "THB")
        currency_of[account.get("id")] = currency
        balance[currency] += _amount(account.get("opening_balance"))
    skipped: Counter[str] = Counter()
    for t in rows:
        if t.get("account_id") not in currency_of:
            continue
        currency = str(t.get("currency") or "THB")
        kind = str(t.get("transaction_type"))
        if kind in INFLOW:
            balance[currency] += _amount(t.get("amount"))
        elif kind in OUTFLOW:
            balance[currency] -= _amount(t.get("amount"))
        else:
            skipped[kind] += 1
    held = _investments_view(investments)["value"]
    owned = _assets_view(assets)["value"]
    owed = _liabilities_view(liabilities)["outstanding"]
    total: defaultdict[str, Decimal] = defaultdict(Decimal, balance)
    for part, sign in ((held, 1), (owned, 1), (owed, -1)):
        for currency, amount in part.items():
            total[currency] += sign * Decimal(amount)
    return {
        "accounts": _text(balance),
        "investments": held,
        "assets": owned,
        "liabilities": owed,
        "net_worth": _text(total),
        "not_counted": dict(sorted(skipped.items())),
    }
