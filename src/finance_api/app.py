"""finance-api HTTP routes (SPEC-001).

No `from __future__ import annotations` here: FastAPI resolves the local `Caller` dependency
alias from the real annotation objects.
"""

import asyncio
import os
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from finance_api import views
from finance_api.auth import Principal, TokenVerifier, Unauthenticated
from finance_api.cells import CellClient, CellError

BANGKOK = ZoneInfo("Asia/Bangkok")

# (cell, plural, filters) per source the views read.
SOURCES: dict[str, tuple[str, str, dict[str, str | list[str]]]] = {
    "accounts": ("finance-accounts", "accounts", {}),
    "transactions": ("transactions", "transactions", {}),
    "budgets": ("budgets", "budget-plans", {"status": "active"}),
    "cash_flow": ("cash-flow", "cashflow-plans", {"status": "active"}),
    "subscriptions": (
        "subscriptions",
        "subscriptions",
        {"status": ["active", "trial"]},
    ),
    "liabilities": ("liabilities", "liabilities", {"status": ["active", "overdue"]}),
    "investments": ("investments", "investments", {"status": "active"}),
    "assets": ("business-assets", "assets", {"status": "active"}),
    "tax": ("tax", "tax-items", {"status": ["estimated", "open", "overdue"]}),
}


def _month(value: str | None) -> str:
    month = datetime.now(BANGKOK).strftime("%Y-%m") if value is None else value
    try:
        views.month_bounds(month)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_value", "fields": ["month"]}
        ) from exc
    return month


class Loaded:
    """Rows of one source, or the reason they are missing."""

    def __init__(
        self, rows: list[dict[str, Any]] | None, truncated: bool, error: str | None
    ) -> None:
        self.rows, self.truncated, self.error = rows or [], truncated, error


def create_app(verifier: TokenVerifier, cells: CellClient) -> FastAPI:
    app = FastAPI(
        title="finance-api",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def caller(authorization: Annotated[str | None, Header()] = None) -> Principal:
        try:
            return verifier.verify(authorization, None)
        except Unauthenticated as exc:
            raise HTTPException(
                status_code=401, detail={"error": "unauthenticated"}
            ) from exc

    Caller = Annotated[Principal, Depends(caller)]

    async def load(
        principal: Principal,
        names: list[str],
        extra: dict[str, dict[str, str | list[str]]] | None = None,
    ) -> dict[str, Loaded]:
        async with cells.session() as http:

            async def one(name: str) -> Loaded:
                cell, plural, filters = SOURCES[name]
                filters = {**filters, **(extra or {}).get(name, {})}
                try:
                    rows, truncated = await cells.list_all(
                        http, cell, plural, principal.token, filters
                    )
                except CellError as exc:
                    return Loaded(None, False, exc.kind)
                return Loaded(rows, truncated, None)

            results = await asyncio.gather(*(one(name) for name in names))
        return dict(zip(names, results, strict=True))

    async def load_month(
        principal: Principal, names: list[str], plans: list[str], month: str
    ) -> dict[str, Loaded]:
        """``names`` first, then only the transactions the month's plans need (SPEC-002)."""
        d = await load(principal, names)
        start, end = views.transaction_window((d[p].rows for p in plans), month)
        bounds: dict[str, str | list[str]] = {}
        if start is not None:
            bounds["transaction_date_from"] = start.isoformat()
        if end is not None:
            bounds["transaction_date_to"] = end.isoformat()
        d.update(await load(principal, ["transactions"], {"transactions": bounds}))
        return d

    def section(
        needs: list[Loaded], build: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        for part in needs:
            if part.error:
                return {"ok": False, "error": part.error}
        return {
            "ok": True,
            **build(),
            "truncated": any(part.truncated for part in needs),
        }

    @app.get("/health")
    def health() -> dict[str, str | None]:
        return {"status": "ok", "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA")}

    @app.get("/v1/overview")
    async def overview(
        principal: Caller, month: Annotated[str | None, Query()] = None
    ) -> dict[str, Any]:
        m = _month(month)
        today = datetime.now(BANGKOK).date()
        d = await load_month(
            principal,
            [
                "accounts",
                "budgets",
                "cash_flow",
                "subscriptions",
                "liabilities",
                "investments",
                "tax",
            ],
            ["budgets", "cash_flow"],
            m,
        )
        tx = d["transactions"]
        return {
            "month": m,
            "accounts": section(
                [d["accounts"]],
                lambda: {
                    "items": [
                        {
                            k: a.get(k)
                            for k in (
                                "id",
                                "name",
                                "account_type",
                                "institution",
                                "currency",
                                "status",
                                "scope",
                            )
                        }
                        for a in d["accounts"].rows
                        if a.get("status") == "active"
                    ]
                },
            ),
            "transactions": section([tx], lambda: views.transactions(tx.rows, m)),
            "budgets": section(
                [d["budgets"], tx],
                lambda: {"lines": views.budgets(d["budgets"].rows, tx.rows, m)},
            ),
            "cash_flow": section(
                [d["cash_flow"], tx],
                lambda: {"lines": views.cash_flow(d["cash_flow"].rows, tx.rows, m)},
            ),
            "subscriptions": section(
                [d["subscriptions"]],
                lambda: views.subscriptions(d["subscriptions"].rows, today),
            ),
            "liabilities": section(
                [d["liabilities"]], lambda: views.liabilities(d["liabilities"].rows)
            ),
            "investments": section(
                [d["investments"]], lambda: views.investments(d["investments"].rows)
            ),
            "tax": section([d["tax"]], lambda: views.tax(d["tax"].rows)),
        }

    @app.get("/v1/budget-vs-actual")
    async def budget_vs_actual(
        principal: Caller, month: Annotated[str | None, Query()] = None
    ) -> dict[str, Any]:
        m = _month(month)
        d = await load_month(principal, ["budgets"], ["budgets"], m)
        return {
            "month": m,
            "budgets": section(
                [d["budgets"], d["transactions"]],
                lambda: {
                    "lines": views.budgets(d["budgets"].rows, d["transactions"].rows, m)
                },
            ),
        }

    @app.get("/v1/net-worth")
    async def net_worth(principal: Caller) -> dict[str, Any]:
        names = ["accounts", "transactions", "investments", "assets", "liabilities"]
        d = await load(principal, names)
        for name in names:
            if d[name].error:
                return {"ok": False, "error": d[name].error, "section": name}
        return {
            "ok": True,
            **views.net_worth(
                d["accounts"].rows,
                d["transactions"].rows,
                investments=d["investments"].rows,
                assets=d["assets"].rows,
                liabilities=d["liabilities"].rows,
            ),
            "truncated": any(d[n].truncated for n in names),
        }

    return app
