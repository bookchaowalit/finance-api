"""SPEC-004: PDPA export and erase across the ten finance cells, with fake cells."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from test_api import ISSUER, PUBLIC, token

from finance_api.app import ERASE_STAGES, RESOURCES, create_app
from finance_api.auth import TokenVerifier
from finance_api.cells import DEFAULT_CELLS, CellClient

CELLS = {name: f"https://{name}.example" for name in DEFAULT_CELLS}

# What points at what (copied from the cells' cell.yaml: `references:` across cells,
# `type: ref` inside a cell). A resource must be erased in a later stage than everything
# that points at it.
POINTS_AT = {
    ("transactions", "transactions"): [
        ("finance-accounts", "accounts"),
        ("subscriptions", "subscriptions"),
    ],
    ("investments", "investments"): [("finance-accounts", "accounts")],
    ("liabilities", "liabilities"): [("finance-accounts", "accounts")],
    ("reconciliations", "reconciliations"): [("finance-accounts", "accounts")],
    ("subscriptions", "subscriptions"): [("finance-accounts", "accounts")],
    ("business-assets", "asset-metrics"): [("business-assets", "assets")],
    ("investments", "valuations"): [
        ("investments", "investments"),
        ("investments", "market-prices"),
    ],
    ("subscriptions", "subscription-reviews"): [("subscriptions", "subscriptions")],
    ("subscriptions", "subscription-aliases"): [("subscriptions", "subscriptions")],
}


class Cells:
    """Fake cells: rows per (cell, plural); erase removes up to `limit` unblocked rows."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.blocked: dict[tuple[str, str], int] = {}
        self.fail: dict[str, int | str] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        cell = request.url.host.removesuffix(".example")
        path = request.url.path.removeprefix("/v1/")
        plural = path.removesuffix("/erase")
        self.calls.append((request.method, cell, plural))
        assert request.headers["authorization"].startswith("Bearer ")
        if cell in self.fail:
            what = self.fail[cell]
            if what == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(int(what), json={"detail": {"error": "x"}})
        rows = self.rows.setdefault((cell, plural), [])
        if request.method == "POST":
            body = json.loads(request.content)
            self.bodies.append(body)
            keep = self.blocked.get((cell, plural), 0)
            gone = rows[keep:][: body["limit"]]
            del rows[keep : keep + len(gone)]
            return httpx.Response(
                200, json={"erased": len(gone), "remaining": len(rows)}
            )
        after = int(request.url.params.get("after_id", 0))
        limit = int(request.url.params.get("limit", 100))
        return httpx.Response(
            200, json={"items": [r for r in rows if r["id"] > after][:limit]}
        )


@pytest.fixture
def cells():
    return Cells()


def make_client(cells: Cells, **options: Any) -> TestClient:
    return TestClient(
        create_app(
            TokenVerifier(PUBLIC, ISSUER, "finance-api"),
            CellClient(
                CELLS, transport=httpx.MockTransport(cells.handler), timeout=2.0
            ),
            **options,
        )
    )


@pytest.fixture
def client(cells):
    return make_client(cells)


def caller():
    t = token()
    return {"Authorization": f"Bearer {t}"}, jwt.decode(
        t, options={"verify_signature": False}
    )["ws"]


def fill(cells: Cells, n: int = 3) -> None:
    for cell, plural in RESOURCES:
        cells.rows[(cell, plural)] = [{"id": i + 1, "name": "x"} for i in range(n)]


def test_resources_cover_every_cell_and_stages_follow_the_references():
    assert {cell for cell, _ in RESOURCES} == set(DEFAULT_CELLS)
    assert len(RESOURCES) == len(set(RESOURCES)) == 15
    staged = [r for stage in ERASE_STAGES for r in stage]
    assert sorted(staged) == sorted(RESOURCES)
    stage_of = {r: i for i, stage in enumerate(ERASE_STAGES) for r in stage}
    for source, targets in POINTS_AT.items():
        for target in targets:
            assert stage_of[source] < stage_of[target], (source, target)


def test_export_returns_every_record(client, cells):
    fill(cells, 3)
    cells.rows[("transactions", "transactions")] = [
        {"id": i + 1, "amount": "1.00"} for i in range(2500)
    ]
    headers, ws = caller()
    response = client.get("/v1/export", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="finance-export-{ws}-')
    data = response.json()
    assert data["format"] == "finance-export/1"
    assert data["workspace"] == ws
    assert data["generated_at"]
    assert set(data["records"]) == {f"{c}.{p}" for c, p in RESOURCES}
    assert len(data["records"]["transactions.transactions"]) == 2500
    assert data["records"]["finance-accounts.accounts"][0] == {"id": 1, "name": "x"}
    assert all(m == "GET" for m, _, _ in cells.calls)


@pytest.mark.parametrize(
    ("fail", "reason"),
    [("403", "forbidden"), ("500", "cell_error"), ("timeout", "timeout")],
)
def test_export_is_complete_or_refused(client, cells, fail, reason):
    fill(cells, 1)
    cells.fail["tax"] = fail
    response = client.get("/v1/export", headers=caller()[0])
    assert response.status_code == 502
    assert response.json() == {
        "error": "export_incomplete",
        "sources": {"tax.tax-items": reason},
    }


def test_export_refuses_more_than_the_cap(cells):
    fill(cells, 1)
    cells.rows[("budgets", "budget-plans")] = [{"id": i + 1} for i in range(6)]
    response = make_client(cells, export_cap=5).get("/v1/export", headers=caller()[0])
    assert response.status_code == 502
    assert response.json()["sources"] == {"budgets.budget-plans": "too_large"}


def test_erase_needs_the_callers_workspace(client, cells):
    headers, ws = caller()
    for bad in (
        {},
        {"confirm": str(uuid.uuid4())},
        {"confirm": "x"},
        {"confirm": ws, "x": 1},
    ):
        assert client.post("/v1/erase", headers=headers, json=bad).status_code == 422
    assert client.post("/v1/erase", json={"confirm": ws}).status_code == 401
    assert cells.calls == []


def test_erase_runs_the_stages_in_order(client, cells):
    fill(cells, 1200)
    headers, ws = caller()
    response = client.post("/v1/erase", headers=headers, json={"confirm": ws})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["done"] is True
    assert data["resources"] == {
        f"{c}.{p}": {"erased": 1200, "remaining": 0} for c, p in RESOURCES
    }
    assert all(rows == [] for rows in cells.rows.values())
    assert all(b == {"confirm": ws, "limit": 500} for b in cells.bodies)
    stage_of = {r: i for i, stage in enumerate(ERASE_STAGES) for r in stage}
    order = [stage_of[(c, p)] for m, c, p in cells.calls]
    assert order == sorted(order)
    assert {m for m, _, _ in cells.calls} == {"POST"}


def test_erase_stops_at_records_still_referenced(cells):
    fill(cells, 2)
    cells.blocked[("subscriptions", "subscriptions")] = 1
    headers, ws = caller()
    started = time.monotonic()
    data = (
        make_client(cells, erase_budget=0.3, erase_pause=0.05)
        .post("/v1/erase", headers=headers, json={"confirm": ws})
        .json()
    )
    assert time.monotonic() - started < 2  # the budget bounds the waiting
    assert data["done"] is False
    assert data["resources"]["subscriptions.subscriptions"] == {
        "erased": 1,
        "remaining": 1,
    }
    # Stage 3 waits: accounts are untouched and not reached.
    assert data["resources"]["finance-accounts.accounts"] == {
        "erased": 0,
        "remaining": None,
    }
    assert ("POST", "finance-accounts", "accounts") not in cells.calls
    assert len(cells.rows[("finance-accounts", "accounts")]) == 2


@pytest.mark.parametrize(
    ("fail", "reason"),
    [
        ("403", "forbidden"),
        ("503", "unavailable"),
        ("409", "cell_error"),
        ("timeout", "timeout"),
    ],
)
def test_erase_reports_a_refusing_cell(client, cells, fail, reason):
    fill(cells, 1)
    cells.fail["transactions"] = fail
    headers, ws = caller()
    data = client.post("/v1/erase", headers=headers, json={"confirm": ws}).json()
    assert data["done"] is False
    assert data["resources"]["transactions.transactions"] == {"error": reason}
    assert not any(cell == "finance-accounts" for _, cell, _ in cells.calls)


def test_erase_stops_when_the_budget_is_spent(cells):
    fill(cells, 1)
    headers, ws = caller()
    data = (
        make_client(cells, erase_budget=0.0)
        .post("/v1/erase", headers=headers, json={"confirm": ws})
        .json()
    )
    assert data["done"] is False
    assert cells.calls == []
    assert all(
        v == {"erased": 0, "remaining": None} for v in data["resources"].values()
    )


def test_erase_waits_for_references_to_catch_up(cells):
    fill(cells, 1)
    key = ("finance-accounts", "accounts")
    cells.blocked[key] = 1
    original = cells.handler
    tries: list[int] = []

    def catching_up(request):
        if request.url.host.startswith("finance-accounts") and request.method == "POST":
            tries.append(1)
            if len(tries) == 2:  # the references caught up after the first refusal
                cells.blocked[key] = 0
        return original(request)

    cells.handler = catching_up
    headers, ws = caller()
    client = TestClient(
        create_app(
            TokenVerifier(PUBLIC, ISSUER, "finance-api"),
            CellClient(CELLS, transport=httpx.MockTransport(catching_up), timeout=2.0),
            erase_pause=0.01,
        )
    )
    data = client.post("/v1/erase", headers=headers, json={"confirm": ws}).json()
    assert data["done"] is True
    assert len(tries) >= 2
    assert data["resources"]["finance-accounts.accounts"] == {
        "erased": 1,
        "remaining": 0,
    }


def test_export_and_erase_have_their_own_stricter_limits(cells):
    client = make_client(cells, rate_per_minute=100)
    headers, ws = caller()
    for _ in range(3):
        assert client.get("/v1/export", headers=headers).status_code == 200
    refused = client.get("/v1/export", headers=headers)
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 60
    # Erase has its own window (10 per 10 minutes); a mistyped confirmation costs no slot.
    for _ in range(5):
        assert (
            client.post(
                "/v1/erase", headers=headers, json={"confirm": str(uuid.uuid4())}
            ).status_code
            == 422
        )
    for _ in range(10):
        assert (
            client.post("/v1/erase", headers=headers, json={"confirm": ws}).status_code
            == 200
        )
    assert (
        client.post("/v1/erase", headers=headers, json={"confirm": ws}).status_code
        == 429
    )
    # Other routes still answer, and another subject is not affected.
    assert client.get("/v1/net-worth", headers=headers).status_code == 200
    assert client.get("/v1/export", headers=caller()[0]).status_code == 200


def test_export_has_a_deadline(cells):
    fill(cells, 1)

    async def slow_tax(request):
        if request.url.host.startswith("tax"):
            await asyncio.sleep(2)
        return cells.handler(request)

    client = TestClient(
        create_app(
            TokenVerifier(PUBLIC, ISSUER, "finance-api"),
            CellClient(CELLS, transport=httpx.MockTransport(slow_tax), timeout=5.0),
            export_deadline=0.3,
        )
    )
    started = time.monotonic()
    response = client.get("/v1/export", headers=caller()[0])
    assert time.monotonic() - started < 1.5
    assert response.status_code == 502
    assert response.json()["sources"] == {"tax.tax-items": "timeout"}


def test_logging_is_configured_for_the_audit_lines():
    from finance_api.server import configure_logging

    logger = configure_logging()
    assert logger.name == "finance_api"
    assert logger.isEnabledFor(logging.INFO)
    assert logger.handlers
    assert configure_logging().handlers == logger.handlers  # no duplicate handler


def test_audit_lines_name_who_and_what_without_content(client, cells, caplog):
    fill(cells, 2)
    headers, ws = caller()
    with caplog.at_level(logging.INFO, logger="finance_api"):
        client.get("/v1/export", headers=headers)
        client.post("/v1/erase", headers=headers, json={"confirm": ws})
    lines = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("{")
    ]
    events = {line["event"]: line for line in lines}
    assert events["finance-api.export"]["workspace"] == ws
    assert events["finance-api.export"]["outcome"] == "ok"
    assert events["finance-api.export"]["records"] == 2 * len(RESOURCES)
    assert events["finance-api.erase"]["outcome"] == "done"
    assert events["finance-api.erase"]["erased"] == 2 * len(RESOURCES)
    assert all(e["actor"] for e in events.values())
    assert '"name"' not in caplog.text
    assert "Bearer" not in caplog.text


def test_export_and_erase_end_before_the_gateway_gives_up():
    """The edge gateway cuts an upstream call at 30 s (GATEWAY_UPSTREAM_TIMEOUT_SECONDS)."""
    import inspect

    from finance_api.app import EXPORT_DEADLINE, create_app

    defaults = inspect.signature(create_app).parameters
    assert EXPORT_DEADLINE < 30
    assert defaults["export_deadline"].default < 30
    assert defaults["erase_budget"].default < 30
