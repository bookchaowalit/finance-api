"""SPEC-001 AC-03, AC-05, AC-07: the views, from fixed cell rows."""

from __future__ import annotations

from datetime import date

import pytest

from finance_api import views


def tx(day, kind, amount, currency="THB", **extra):
    return {
        "id": hash((day, kind, amount)) & 0xFFFF,
        "transaction_date": day,
        "transaction_type": kind,
        "amount": amount,
        "currency": currency,
        **extra,
    }


def test_month_bounds_and_bad_months():
    assert views.month_bounds("2026-11") == (date(2026, 11, 1), date(2026, 11, 30))
    assert views.month_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    for bad in ("2026-13", "26-11", "2026-1", "", "2026-11-01"):
        with pytest.raises(ValueError, match="YYYY-MM"):
            views.month_bounds(bad)


def test_income_and_expense_are_exact_per_currency_inside_the_month():
    rows = [
        tx("2026-11-01", "income", "0.1000"),
        tx("2026-11-30", "income", "0.2000"),
        tx("2026-11-15", "expense", "10.0000"),
        tx("2026-11-15", "fee", "0.0100"),
        tx("2026-11-15", "expense", "5.0000", "USD"),
        tx("2026-10-31", "income", "999.0000"),
        tx("2026-11-15", "transfer", "50.0000"),
    ]
    out = views.transactions(rows, "2026-11")
    assert out["income"] == {"THB": "0.3000"}  # not 0.30000000000000004
    assert out["expense"] == {"THB": "10.0100", "USD": "5.0000"}
    assert out["not_counted"] == {"transfer": 1}


def test_budget_lines_count_spending_of_their_category_and_period():
    plans = [
        {
            "id": 1,
            "name": "Food",
            "category": "food",
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "planned_amount": "100.0000",
            "currency": "THB",
            "status": "active",
        },
        {
            "id": 2,
            "name": "All",
            "category": None,
            "period_start": "2026-11-10",
            "period_end": "2026-11-20",
            "planned_amount": "50.0000",
            "currency": "THB",
            "status": "active",
        },
        {
            "id": 3,
            "name": "Old",
            "category": "food",
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "planned_amount": "1.0000",
            "currency": "THB",
            "status": "active",
        },
    ]
    rows = [
        tx("2026-11-05", "expense", "30.5000", category="food"),
        tx("2026-11-12", "expense", "20.0000", category="travel"),
        tx("2026-11-12", "expense", "7.0000", "USD", category="food"),
        tx("2026-11-12", "income", "500.0000", category="food"),
    ]
    lines = views.budgets(plans, rows, "2026-11")
    assert [(line["id"], line["spent"], line["remaining"]) for line in lines] == [
        (1, "30.5000", "69.5000"),
        (2, "20.0000", "30.0000"),  # no category = every expense in its own period
    ]


def test_cash_flow_plans_compare_expected_with_actual():
    plans = [
        {
            "id": 1,
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "expected_inflow": "100.0000",
            "expected_outflow": "60.0000",
            "currency": "THB",
            "status": "active",
        }
    ]
    rows = [
        tx("2026-11-02", "income", "80.0000"),
        tx("2026-11-03", "expense", "70.0000"),
    ]
    (line,) = views.cash_flow(plans, rows, "2026-11")
    assert (
        line["actual_inflow"],
        line["actual_outflow"],
        line["net_expected"],
        line["net_actual"],
    ) == ("80.0000", "70.0000", "40.0000", "10.0000")


def test_subscriptions_monthly_cost_and_renewals():
    rows = [
        {
            "id": 1,
            "amount": "120.0000",
            "currency": "THB",
            "billing_interval": "yearly",
            "status": "active",
            "next_billing_date": "2026-11-20",
        },
        {
            "id": 2,
            "amount": "99.0000",
            "currency": "THB",
            "billing_interval": "monthly",
            "status": "trial",
            "next_billing_date": "2027-01-20",
        },
        {
            "id": 3,
            "amount": "9.0000",
            "currency": "USD",
            "billing_interval": "one_time",
            "status": "active",
        },
    ]
    out = views.subscriptions(rows, date(2026, 11, 11))
    assert out["monthly"] == {"THB": "109.0000"}  # 120/12 + 99
    assert [s["id"] for s in out["renewing"]] == [1]


def test_liabilities_investments_tax():
    assert views.liabilities(
        [
            {"outstanding_amount": "1000.5000", "currency": "THB", "status": "active"},
            {"outstanding_amount": None, "currency": "THB", "status": "overdue"},
        ]
    )["outstanding"] == {"THB": "1000.5000"}
    inv = views.investments(
        [
            {
                "current_value": "10.0000",
                "cost_basis": "8.0000",
                "currency": "USD",
                "status": "active",
            }
        ]
    )
    assert (inv["value"], inv["cost"]) == ({"USD": "10.0000"}, {"USD": "8.0000"})
    tax = views.tax(
        [
            {
                "amount_due": "100.0000",
                "amount_paid": "30.0000",
                "currency": "THB",
                "status": "open",
                "due_date": "2026-12-31",
            }
        ]
    )
    assert tax["outstanding"] == {"THB": "70.0000"}


def test_net_worth_per_currency():
    accounts = [
        {"id": 1, "opening_balance": "100.0000", "currency": "THB", "status": "active"},
        {"id": 2, "opening_balance": None, "currency": "USD", "status": "active"},
    ]
    rows = [
        tx("2026-11-01", "income", "50.0000", account_id=1),
        tx("2026-11-02", "expense", "20.0000", account_id=1),
        tx("2026-11-02", "refund", "5.0000", account_id=1),
        tx("2026-11-02", "transfer", "9.0000", account_id=1),
        tx("2026-11-02", "expense", "1.0000", "USD", account_id=2),
    ]
    out = views.net_worth(
        accounts,
        rows,
        investments=[
            {"current_value": "200.0000", "currency": "THB", "status": "active"}
        ],
        assets=[{"total_value": "300.0000", "currency": "THB", "status": "active"}],
        liabilities=[
            {"outstanding_amount": "150.0000", "currency": "THB", "status": "active"}
        ],
    )
    assert out["accounts"] == {"THB": "135.0000", "USD": "-1.0000"}
    assert out["net_worth"] == {"THB": "485.0000", "USD": "-1.0000"}
    assert out["not_counted"] == {"transfer": 1}


# --- SPEC-002: the transactions window ---------------------------------------------------------


def plan(start, end, status="active"):
    return {"period_start": start, "period_end": end, "status": status}


def test_window_is_the_month_when_no_plan_reaches_outside():  # SPEC-002 AC-01
    inside = [plan("2026-11-01", "2026-11-30"), plan("2026-11-10", "2026-11-20")]
    assert views.transaction_window([inside], "2026-11") == (
        date(2026, 11, 1),
        date(2026, 11, 30),
    )
    assert views.transaction_window([], "2026-11") == (
        date(2026, 11, 1),
        date(2026, 11, 30),
    )


def test_window_widens_to_overlapping_plans():  # SPEC-002 AC-02
    budgets = [plan("2026-10-15", "2026-11-15")]
    cash = [plan("2026-11-20", "2027-01-31")]
    # A plan that does not overlap the month, or is not active, does not widen it.
    other = [
        plan("2025-01-01", "2025-12-31"),
        plan("2020-01-01", "2030-01-01", "archived"),
    ]
    assert views.transaction_window([budgets, cash, other], "2026-11") == (
        date(2026, 10, 15),
        date(2027, 1, 31),
    )


def test_an_open_ended_plan_leaves_that_side_open():  # SPEC-002 AC-03
    assert views.transaction_window([[plan(None, "2026-11-30")]], "2026-11") == (
        None,
        date(2026, 11, 30),
    )
    assert views.transaction_window([[plan("2026-11-01", "")]], "2026-11") == (
        date(2026, 11, 1),
        None,
    )


def test_plans_count_only_transactions_of_their_own_scope():
    """A personal plan never counts business transactions (and the other way round)."""
    budget = {
        "id": 1,
        "category": None,
        "period_start": "2026-11-01",
        "period_end": "2026-11-30",
        "planned_amount": "100.0000",
        "currency": "THB",
        "status": "active",
        "scope": "personal",
    }
    cash = {
        "id": 2,
        "period_start": "2026-11-01",
        "period_end": "2026-11-30",
        "expected_inflow": "0.0000",
        "expected_outflow": "0.0000",
        "currency": "THB",
        "status": "active",
        "scope": "business",
    }
    rows = [
        tx("2026-11-05", "expense", "10.0000", scope="personal"),
        tx("2026-11-06", "expense", "80.0000", scope="business"),
        tx("2026-11-07", "income", "5.0000", scope="business"),
    ]
    (line,) = views.budgets([budget], rows, "2026-11")
    assert line["spent"] == "10.0000"
    (flow,) = views.cash_flow([cash], rows, "2026-11")
    assert (flow["actual_inflow"], flow["actual_outflow"]) == ("5.0000", "80.0000")
