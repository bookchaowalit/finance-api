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
GATEWAY_HOST = "gateway.bookchaowalit.com"
GATEWAY = f"https://{GATEWAY_HOST}/v1"
# SPEC-003: lets these calls skip the Cloudflare per-IP limit (one IP serves every user); sent to the gateway only.
EDGE_BYPASS_HEADER = "X-Edge-Bypass"
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
TOO_LARGE = "upstream response too large"  # platform-api-gateway edge._proxy


def _too_large(response: httpx.Response) -> bool:
    """The gateway's own answer for an upstream body over its response cap."""
    if response.status_code != 502:
        return False
    try:
        return bool(response.json().get("error") == TOO_LARGE)
    except (ValueError, AttributeError):
        return False


ERASE_BATCH = 500  # the cells' largest erase batch (SPEC-008)


class CellError(Exception):
    """A cell could not answer: ``timeout``, ``unreachable``, ``forbidden``, ``unavailable`` or ``cell_error``."""

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
        edge_bypass: str | None = None,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        self.urls = {
            name: _https(name, url) for name, url in (urls or DEFAULT_CELLS).items()
        }
        self.edge_bypass = edge_bypass or None
        self.transport = transport
        self.timeout = timeout
        self.page_cap = page_cap
        # Bounds the fan-out of all requests in this process (SPEC-001 AC-10).
        self.max_inflight = max_inflight
        self._inflight = asyncio.Semaphore(max_inflight)

    def _headers(self, cell: str, bearer: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {bearer}"}
        if self.edge_bypass and urlsplit(self.urls[cell]).hostname == GATEWAY_HOST:
            headers[EDGE_BYPASS_HEADER] = self.edge_bypass
        return headers

    def session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=False
        )

    async def _send(
        self,
        http: httpx.AsyncClient,
        method: str,
        cell: str,
        path: str,
        bearer: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """One cell request under a shared slot and one deadline (AC-04); errors as CellError."""
        if cell not in self.urls:
            raise CellError("unreachable")
        # Waiting for a slot counts against the timeout too, so busy or slow cells can't
        # hold this request past it.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        try:
            await asyncio.wait_for(self._inflight.acquire(), self.timeout)
        except TimeoutError as exc:
            raise CellError("timeout") from exc
        try:
            left = deadline - loop.time()
            if left <= 0:
                raise CellError("timeout")
            # httpx's timeout is per connect/read/write; wait_for bounds the whole call,
            # so a trickling body can't run past the deadline.
            return await asyncio.wait_for(
                http.request(
                    method,
                    f"{self.urls[cell]}{path}",
                    headers=self._headers(cell, bearer),
                    timeout=left,
                    **kwargs,
                ),
                left,
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise CellError("timeout") from exc
        except httpx.HTTPError as exc:
            raise CellError("unreachable") from exc
        finally:
            self._inflight.release()  # also on error or cancellation

    async def list_all(
        self,
        http: httpx.AsyncClient,
        cell: str,
        plural: str,
        bearer: str,
        filters: Mapping[str, str | list[str]] | None = None,
        *,
        page_cap: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every row (by id, paging with after_id) up to the cap; True when rows were left out."""
        cap = self.page_cap if page_cap is None else page_cap
        rows: list[dict[str, Any]] = []
        after = 0
        size = PAGE_SIZE
        while True:
            # One more than still fits, to tell "exactly the cap" from "more than the cap".
            limit = min(size, cap - len(rows) + 1)
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("limit", str(limit)),
                ("after_id", str(after)),
            ]
            params.extend(
                (name, item)
                for name, value in (filters or {}).items()
                for item in (value if isinstance(value, list) else [value])
            )
            response = await self._send(
                http, "GET", cell, f"/v1/{plural}", bearer, params=params
            )
            if response.status_code in (401, 403):
                raise CellError("forbidden")
            if limit > 1 and _too_large(response):
                # The gateway refuses an upstream body over its cap (1 MiB): the same page in
                # halves, and the rest of the read keeps the size that fitted. Any other 502 (a
                # cell that is down) is a cell_error at once, never a burst of retries.
                size = max(1, limit // 2)
                continue
            if response.status_code != 200:
                raise CellError("cell_error")
            try:
                page = response.json()["items"]
            except (ValueError, KeyError, TypeError) as exc:
                raise CellError("cell_error") from exc
            rows.extend(page)
            if len(rows) > cap:
                return rows[:cap], True
            if len(page) < limit or not page:
                return rows, False
            after = int(page[-1]["id"])

    async def erase(
        self,
        http: httpx.AsyncClient,
        cell: str,
        plural: str,
        bearer: str,
        workspace: str,
        limit: int = ERASE_BATCH,
    ) -> tuple[int, int]:
        """One erase batch (cells' SPEC-008): (erased, remaining)."""
        response = await self._send(
            http,
            "POST",
            cell,
            f"/v1/{plural}/erase",
            bearer,
            json={"confirm": workspace, "limit": limit},
        )
        if response.status_code in (401, 403):
            raise CellError("forbidden")
        if response.status_code == 503:
            raise CellError("unavailable")
        if response.status_code != 200:
            raise CellError("cell_error")
        try:
            body = response.json()
            erased, remaining = int(body["erased"]), int(body["remaining"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CellError("cell_error") from exc
        if erased < 0 or remaining < 0:
            raise CellError("cell_error")
        return erased, remaining
