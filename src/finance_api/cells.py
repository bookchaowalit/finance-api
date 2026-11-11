"""Reading the finance cells with the caller's own bearer (SPEC-001 AC-02, AC-04, AC-06)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

# Through the edge gateway (SPEC-001 AC-09): Cloudflare WAF and rate limit, and the signed
# X-Gateway-Assertion the cells check, so the cells can refuse direct calls. Override with
# FINANCE_API_CELLS. The names are the gateway route names (routes.json).
GATEWAY = "https://gateway.bookchaowalit.com/v1"
DEFAULT_CELLS = {
    name: f"{GATEWAY}/{name}"
    for name in (
        "finance-accounts",
        "transactions",
        "budgets",
        "cash-flow",
        "reconciliations",
        "business-assets",
        "investments",
        "liabilities",
        "subscriptions",
        "tax",
    )
}
PAGE_SIZE = 1000  # the cells' MAX_LIMIT


class CellError(Exception):
    """A cell could not answer: ``timeout``, ``unreachable``, ``forbidden`` or ``cell_error``."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _https(name: str, url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.query:
        raise ValueError(f"{name}: a cell URL must be a plain https origin")
    return url.rstrip("/")


class CellClient:
    def __init__(
        self,
        urls: Mapping[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
        page_cap: int = 10_000,
        max_inflight: int = 16,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        self.urls = {
            name: _https(name, url) for name, url in (urls or DEFAULT_CELLS).items()
        }
        self.transport = transport
        self.timeout = timeout
        self.page_cap = page_cap
        # Bounds the fan-out of all requests in this process (SPEC-001 AC-10).
        self.max_inflight = max_inflight
        self._inflight = asyncio.Semaphore(max_inflight)

    def session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=False
        )

    async def list_all(
        self,
        http: httpx.AsyncClient,
        cell: str,
        plural: str,
        bearer: str,
        filters: Mapping[str, str | list[str]] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every row (by id, paging with after_id) up to the cap; True when rows were left out."""
        if cell not in self.urls:
            raise CellError("unreachable")
        rows: list[dict[str, Any]] = []
        after = 0
        while True:
            # One more than still fits, to tell "exactly the cap" from "more than the cap".
            limit = min(PAGE_SIZE, self.page_cap - len(rows) + 1)
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("limit", str(limit)),
                ("after_id", str(after)),
            ]
            params.extend(
                (name, item)
                for name, value in (filters or {}).items()
                for item in (value if isinstance(value, list) else [value])
            )
            try:
                async with self._inflight:
                    response = await http.get(
                        f"{self.urls[cell]}/v1/{plural}",
                        params=params,
                        headers={"Authorization": f"Bearer {bearer}"},
                    )
            except httpx.TimeoutException as exc:
                raise CellError("timeout") from exc
            except httpx.HTTPError as exc:
                raise CellError("unreachable") from exc
            if response.status_code in (401, 403):
                raise CellError("forbidden")
            if response.status_code != 200:
                raise CellError("cell_error")
            try:
                page = response.json()["items"]
            except (ValueError, KeyError, TypeError) as exc:
                raise CellError("cell_error") from exc
            rows.extend(page)
            if len(rows) > self.page_cap:
                return rows[: self.page_cap], True
            if len(page) < limit or not page:
                return rows, False
            after = int(page[-1]["id"])
