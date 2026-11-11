"""finance-api HTTP routes (SPEC-001).

No `from __future__ import annotations` here: FastAPI resolves the local `Caller` dependency
alias from the real annotation objects.
"""

import asyncio
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from finance_api import views
from finance_api.auth import Principal, TokenVerifier, Unauthenticated
from finance_api.cells import CellClient, CellError

BANGKOK = ZoneInfo("Asia/Bangkok")
log = logging.getLogger("finance_api")

# SPEC-004: every resource of the ten cells, as (cell, plural).
RESOURCES: list[tuple[str, str]] = [
    ("finance-accounts", "accounts"),
    ("transactions", "transactions"),
    ("budgets", "budget-plans"),
    ("cash-flow", "cashflow-plans"),
    ("reconciliations", "reconciliations"),
    ("business-assets", "assets"),
    ("business-assets", "asset-metrics"),
    ("investments", "investments"),
    ("investments", "market-prices"),
    ("investments", "valuations"),
    ("liabilities", "liabilities"),
    ("subscriptions", "subscriptions"),
    ("subscriptions", "subscription-reviews"),
    ("subscriptions", "subscription-aliases"),
    ("tax", "tax-items"),
]
# Every resource is erased after everything that points at it (cells' references).
ERASE_STAGES: list[list[tuple[str, str]]] = [
    [
        ("transactions", "transactions"),
        ("reconciliations", "reconciliations"),
        ("liabilities", "liabilities"),
        ("investments", "valuations"),
        ("business-assets", "asset-metrics"),
        ("subscriptions", "subscription-reviews"),
        ("subscriptions", "subscription-aliases"),
        ("budgets", "budget-plans"),
        ("cash-flow", "cashflow-plans"),
        ("tax", "tax-items"),
    ],
    [
        ("investments", "investments"),
        ("investments", "market-prices"),
        ("business-assets", "assets"),
        ("subscriptions", "subscriptions"),
    ],
    [("finance-accounts", "accounts")],
]
EXPORT_CAP = 20_000  # records per resource
EXPORT_DEADLINE = 70.0  # seconds; Cloudflare gives up at 100


class EraseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str


def _key(cell: str, plural: str) -> str:
    return f"{cell}.{plural}"


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


class RateLimiter:
    """Requests per subject (actor + workspace) in a sliding window, 60 s unless given (SPEC-001 AC-10).

    One process, in memory: finance-api runs one replica. Each allowed request fans out to
    the cells, so this caps what one token can make the cells and the gateway do.
    """

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        *,
        window: float = 60.0,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self.per_minute, self.clock, self.WINDOW = per_minute, clock, window
        self.hits: dict[tuple[str, str], deque[float]] = {}
        # FastAPI runs the sync auth dependency in a threadpool: check() must be atomic.
        self._lock = threading.Lock()

    def check(self, principal: Principal) -> float:
        """0 when allowed (and counted), else seconds until the next slot."""
        with self._lock:
            now = self.clock()
            cutoff = now - self.WINDOW
            for key in [k for k, q in self.hits.items() if not q or q[-1] <= cutoff]:
                del self.hits[key]  # forget idle subjects so memory stays bounded
            q = self.hits.setdefault(
                (str(principal.workspace), str(principal.actor)), deque()
            )
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.per_minute:
                return q[0] + self.WINDOW - now
            q.append(now)
            return 0.0


def create_app(
    verifier: TokenVerifier,
    cells: CellClient,
    *,
    rate_per_minute: int = 12,
    export_cap: int = EXPORT_CAP,
    erase_budget: float = 60.0,
    erase_pause: float = 3.0,
    export_deadline: float = EXPORT_DEADLINE,
) -> FastAPI:
    limiter = RateLimiter(rate_per_minute)
    # SPEC-004 AC-07: export and erase read or change everything; each has its own window.
    export_limiter = RateLimiter(3, window=600.0)
    erase_limiter = RateLimiter(10, window=600.0)
    # One export at a time per replica: it holds every record of a workspace in memory.
    exporting = asyncio.Semaphore(1)
    app = FastAPI(
        title="finance-api",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def caller(authorization: Annotated[str | None, Header()] = None) -> Principal:
        try:
            principal = verifier.verify(authorization, None)
        except Unauthenticated as exc:
            raise HTTPException(
                status_code=401, detail={"error": "unauthenticated"}
            ) from exc
        wait = limiter.check(principal)
        if wait:
            raise HTTPException(
                status_code=429,
                detail={"error": "rate_limited"},
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )
        return principal

    Caller = Annotated[Principal, Depends(caller)]

    def limited(window: RateLimiter, principal: Principal) -> None:
        wait = window.check(principal)
        if wait:
            raise HTTPException(
                status_code=429,
                detail={"error": "rate_limited"},
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )

    def audit(event: str, principal: Principal, **fields: Any) -> None:
        # SPEC-004 AC-08: who and what, never record content or the token.
        log.info(
            json.dumps(
                {
                    "event": event,
                    "actor": str(principal.actor),
                    "workspace": str(principal.workspace),
                    **fields,
                }
            )
        )

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
        """The plans first, then only the transactions they need (SPEC-002); the other
        ``names`` load alongside, so a slow unrelated cell doesn't delay transactions (AC-07)."""
        others = [n for n in names if n not in plans]
        rest = asyncio.ensure_future(load(principal, others))
        tasks = [rest]
        try:
            d = await load(principal, plans)
            start, end = views.transaction_window((d[p].rows for p in plans), month)
            bounds: dict[str, str | list[str]] = {}
            if start is not None:
                bounds["transaction_date_from"] = start.isoformat()
            if end is not None:
                bounds["transaction_date_to"] = end.isoformat()
            tx = asyncio.ensure_future(
                load(principal, ["transactions"], {"transactions": bounds})
            )
            tasks.append(tx)
            for part in await asyncio.gather(tx, rest):
                d.update(part)
            return d
        finally:
            # Any early exit (gather doesn't cancel siblings when one fails):
            # never leave cell calls running unowned.
            for task in tasks:
                if not task.done():
                    task.cancel()

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

    @app.get("/v1/export")
    async def export(principal: Caller) -> JSONResponse:
        """SPEC-004 AC-01/02: every record of the workspace, or nothing."""
        limited(export_limiter, principal)
        if exporting.locked():
            raise HTTPException(
                status_code=503,
                detail={"error": "export_busy"},
                headers={"Retry-After": "30"},
            )
        async with exporting, cells.session() as http:

            async def one(
                cell: str, plural: str
            ) -> tuple[list[dict[str, Any]], str | None]:
                try:
                    rows, truncated = await cells.list_all(
                        http, cell, plural, principal.token, page_cap=export_cap
                    )
                except CellError as exc:
                    return [], exc.kind
                return (rows, "too_large") if truncated else (rows, None)

            tasks = [asyncio.ensure_future(one(c, p)) for c, p in RESOURCES]
            try:
                await asyncio.wait(tasks, timeout=export_deadline)
            finally:
                late = [task for task in tasks if not task.done()]
                for task in late:
                    task.cancel()
                # Let the cancelled reads finish unwinding (slots released) before answering.
                await asyncio.gather(*late, return_exceptions=True)
            results = [
                task.result() if not task.cancelled() else ([], "timeout")
                for task in tasks
            ]
        failed = {
            _key(c, p): error
            for (c, p), (_, error) in zip(RESOURCES, results, strict=True)
            if error
        }
        if failed:
            audit("finance-api.export", principal, outcome="incomplete", sources=failed)
            return JSONResponse(
                {"error": "export_incomplete", "sources": failed},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
        records = {
            _key(c, p): rows
            for (c, p), (rows, _) in zip(RESOURCES, results, strict=True)
        }
        now = datetime.now(BANGKOK)
        audit(
            "finance-api.export",
            principal,
            outcome="ok",
            records=sum(len(rows) for rows in records.values()),
        )
        name = f"finance-export-{principal.workspace}-{now:%Y%m%d}.json"
        return JSONResponse(
            {
                "format": "finance-export/1",
                "workspace": str(principal.workspace),
                "generated_at": now.isoformat(),
                "records": records,
            },
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{name}"',
            },
        )

    @app.post("/v1/erase")
    async def erase(payload: EraseBody, principal: Caller) -> dict[str, Any]:
        """SPEC-004 AC-03..06: erase in reference order, in bounded steps the caller repeats."""
        try:
            confirmed = uuid.UUID(payload.confirm) == principal.workspace
        except ValueError:
            confirmed = False
        if not confirmed:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_value", "fields": ["confirm"]},
            )
        limited(
            erase_limiter, principal
        )  # after the confirmation: a mistyped one costs no slot
        loop = asyncio.get_running_loop()
        deadline = loop.time() + erase_budget
        report: dict[str, dict[str, Any]] = {
            _key(c, p): {"erased": 0, "remaining": None} for c, p in RESOURCES
        }
        workspace = str(principal.workspace)
        async with cells.session() as http:

            async def run(cell: str, plural: str) -> bool:
                """True when the resource has nothing left."""
                entry = report[_key(cell, plural)]
                while (left := deadline - loop.time()) > 0:
                    try:
                        # The budget is a hard limit: a batch may not run past it.
                        erased, remaining = await asyncio.wait_for(
                            cells.erase(http, cell, plural, principal.token, workspace),
                            left,
                        )
                    except TimeoutError:
                        return False
                    except CellError as exc:
                        report[_key(cell, plural)] = {"error": exc.kind}
                        return False
                    entry["erased"] += erased
                    entry["remaining"] = remaining
                    if remaining == 0:
                        return True
                    if erased == 0:
                        # Still pointed at: the references catch up after the earlier
                        # stage's events are relayed. Wait a little, within the budget.
                        if deadline - loop.time() <= erase_pause:
                            return False
                        await asyncio.sleep(erase_pause)
                return False

            done = True
            for stage in ERASE_STAGES:
                finished = await asyncio.gather(*(run(c, p) for c, p in stage))
                if not all(finished):
                    done = False
                    break
        audit(
            "finance-api.erase",
            principal,
            outcome="done" if done else "partial",
            erased=sum(int(v.get("erased", 0)) for v in report.values()),
            errors={k: v["error"] for k, v in report.items() if "error" in v},
        )
        return {"done": done, "resources": report}

    return app
