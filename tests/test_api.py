"""SPEC-001 AC-01, AC-02, AC-04, AC-06, AC-08: the HTTP API with fake cells."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from finance_api.app import create_app
from finance_api.auth import TokenVerifier
from finance_api.cells import CellClient

ISSUER = "empire-platform-test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC = (
    KEY.public_key()
    .public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    .decode()
)
CELLS = {
    name: f"https://{name}.example"
    for name in (
        "finance-accounts",
        "transactions",
        "budgets",
        "cash-flow",
        "subscriptions",
        "liabilities",
        "investments",
        "business-assets",
        "tax",
    )
}


def token(
    aud=("finance-api", "transactions-api"), issuer=ISSUER, key=PRIVATE, **claims
):
    now = int(time.time())
    body = {
        "iss": issuer,
        "aud": list(aud),
        "sub": str(uuid.uuid4()),
        "ws": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 600,
        "jti": str(uuid.uuid4()),
        **claims,
    }
    return jwt.encode(body, key, algorithm="RS256")


class Cells:
    """Fake cells: rows per (host, plural); records every request."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.fail: dict[str, int | str] = {}
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        host = request.url.host.split(".")[0]
        plural = request.url.path.removeprefix("/v1/")
        if host in self.fail:
            what = self.fail[host]
            if what == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(int(what), json={"detail": {"error": "x"}})
        rows = self.rows.get((host, plural), [])
        after = int(request.url.params.get("after_id", 0))
        limit = int(request.url.params.get("limit", 100))
        page = [r for r in rows if r["id"] > after][:limit]
        return httpx.Response(200, json={"items": page})


@pytest.fixture
def cells():
    return Cells()


@pytest.fixture
def client(cells):
    transport = httpx.MockTransport(cells.handler)
    app = create_app(
        TokenVerifier(PUBLIC, ISSUER, "finance-api"),
        CellClient(CELLS, transport=transport, timeout=2.0, page_cap=250),
    )
    return TestClient(app)


def auth(t=None):
    return {"Authorization": f"Bearer {t or token()}"}


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_tokens_are_checked_before_any_cell_is_called(client, cells):
    other = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    for headers in (
        {},
        auth("not-a-jwt"),
        auth(token(aud=("transactions-api",))),
        auth(token(issuer="someone-else")),
        auth(token(key=other)),
        auth(token(exp=int(time.time()) - 5)),
    ):
        assert (
            client.get(
                "/v1/overview", params={"month": "2026-11"}, headers=headers
            ).status_code
            == 401
        )
    assert cells.seen == []


def test_the_callers_bearer_is_forwarded_unchanged(client, cells):
    t = token()
    assert (
        client.get(
            "/v1/overview", params={"month": "2026-11"}, headers=auth(t)
        ).status_code
        == 200
    )
    assert cells.seen
    for request in cells.seen:
        assert request.headers["authorization"] == f"Bearer {t}"
        assert request.url.scheme == "https"
        assert "workspace" not in str(request.url)
        assert "actor" not in str(request.url)


def test_overview_has_every_section(client, cells):
    cells.rows[("transactions", "transactions")] = [
        {
            "id": 1,
            "transaction_date": "2026-11-02",
            "transaction_type": "income",
            "amount": "10.0000",
            "currency": "THB",
        }
    ]
    body = client.get(
        "/v1/overview", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert body["month"] == "2026-11"
    for section in (
        "accounts",
        "transactions",
        "budgets",
        "cash_flow",
        "subscriptions",
        "liabilities",
        "investments",
        "tax",
    ):
        assert body[section]["ok"] is True, section
    assert body["transactions"]["income"] == {"THB": "10.0000"}


def test_a_failing_cell_blanks_only_its_sections(client, cells):
    cells.fail["budgets"] = "timeout"
    cells.fail["tax"] = 403
    cells.fail["liabilities"] = 500
    body = client.get(
        "/v1/overview", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert body["budgets"] == {"ok": False, "error": "timeout"}
    assert body["tax"] == {"ok": False, "error": "forbidden"}
    assert body["liabilities"] == {"ok": False, "error": "cell_error"}
    assert body["transactions"]["ok"] is True
    cells.fail["transactions"] = "timeout"
    body = client.get(
        "/v1/overview", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert body["transactions"]["ok"] is False
    assert body["budgets"]["ok"] is False  # budgets need the transactions too
    assert body["cash_flow"]["ok"] is False


def test_paging_follows_after_id_and_marks_truncation(client, cells):
    cells.rows[("transactions", "transactions")] = [
        {
            "id": n,
            "transaction_date": "2026-11-02",
            "transaction_type": "expense",
            "amount": "1.0000",
            "currency": "THB",
        }
        for n in range(1, 301)
    ]
    body = client.get(
        "/v1/overview", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert body["transactions"]["truncated"] is True
    assert body["transactions"]["expense"] == {"THB": "250.0000"}  # the page cap
    cells.rows[("transactions", "transactions")] = cells.rows[
        ("transactions", "transactions")
    ][:120]
    body = client.get(
        "/v1/overview", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert body["transactions"]["truncated"] is False
    assert body["transactions"]["expense"] == {"THB": "120.0000"}


def test_a_bad_month_is_refused(client):
    for month in ("2026-13", "november", ""):
        assert (
            client.get(
                "/v1/overview", params={"month": month}, headers=auth()
            ).status_code
            == 422
        )


def test_net_worth_and_budget_vs_actual(client, cells):
    cells.rows[("finance-accounts", "accounts")] = [
        {"id": 1, "opening_balance": "100.0000", "currency": "THB", "status": "active"}
    ]
    cells.rows[("budgets", "budget-plans")] = [
        {
            "id": 1,
            "name": "Food",
            "category": "food",
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "planned_amount": "10.0000",
            "currency": "THB",
            "status": "active",
        }
    ]
    cells.rows[("transactions", "transactions")] = [
        {
            "id": 1,
            "transaction_date": "2026-11-02",
            "transaction_type": "expense",
            "amount": "4.0000",
            "currency": "THB",
            "account_id": 1,
            "category": "food",
        }
    ]
    worth = client.get("/v1/net-worth", headers=auth()).json()
    assert worth["net_worth"] == {"THB": "96.0000"}
    bva = client.get(
        "/v1/budget-vs-actual", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert bva["budgets"]["lines"][0]["remaining"] == "6.0000"
    cells.fail["finance-accounts"] = 503
    assert client.get("/v1/net-worth", headers=auth()).json() == {
        "ok": False,
        "error": "cell_error",
        "section": "accounts",
    }


def test_the_cell_client_refuses_plain_http():
    with pytest.raises(ValueError, match="https"):
        CellClient({"budgets": "http://budgets.example"})


# --- SPEC-002: bounded transaction reads -----------------------------------------------------------


def _tx_params(cells):
    return [
        dict(r.url.params) for r in cells.seen if r.url.host.startswith("transactions.")
    ]


def test_overview_reads_only_the_month(client, cells):  # SPEC-002 AC-01, AC-05
    cells.rows[("transactions", "transactions")] = [
        {
            "id": 1,
            "transaction_date": "2026-11-02",
            "transaction_type": "income",
            "amount": "10.0000",
            "currency": "THB",
        }
    ]
    body = client.get(
        "/v1/overview", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert body["transactions"]["income"] == {"THB": "10.0000"}
    (sent,) = _tx_params(cells)
    assert sent["transaction_date_from"] == "2026-11-01"
    assert sent["transaction_date_to"] == "2026-11-30"


def test_budget_window_follows_the_plans(client, cells):  # SPEC-002 AC-02, AC-03, AC-05
    cells.rows[("budgets", "budget-plans")] = [
        {
            "id": 1,
            "name": "Quarter",
            "category": "food",
            "period_start": "2026-10-01",
            "period_end": None,
            "planned_amount": "10.0000",
            "currency": "THB",
            "status": "active",
        }
    ]
    cells.rows[("transactions", "transactions")] = [
        {
            "id": 1,
            "transaction_date": "2026-10-05",
            "transaction_type": "expense",
            "amount": "4.0000",
            "currency": "THB",
            "category": "food",
        }
    ]
    bva = client.get(
        "/v1/budget-vs-actual", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert bva["budgets"]["lines"][0]["spent"] == "4.0000"
    (sent,) = _tx_params(cells)
    assert sent["transaction_date_from"] == "2026-10-01"
    assert "transaction_date_to" not in sent


def test_unreadable_plans_keep_the_month_window(client, cells):  # SPEC-002 AC-04
    cells.fail["budgets"] = 503
    body = client.get(
        "/v1/budget-vs-actual", params={"month": "2026-11"}, headers=auth()
    ).json()
    assert body["budgets"]["ok"] is False
    (sent,) = _tx_params(cells)
    assert (sent["transaction_date_from"], sent["transaction_date_to"]) == (
        "2026-11-01",
        "2026-11-30",
    )


def test_net_worth_reads_every_transaction(client, cells):  # SPEC-002 AC-06
    client.get("/v1/net-worth", headers=auth())
    (sent,) = _tx_params(cells)
    assert not {"transaction_date_from", "transaction_date_to"} & set(sent)


def test_a_subject_over_its_rate_is_refused_before_any_cell_call(cells):
    """AC-10: per actor+workspace request budget; 429 with Retry-After, no fan-out."""
    transport = httpx.MockTransport(cells.handler)
    app = create_app(
        TokenVerifier(PUBLIC, ISSUER, "finance-api"),
        CellClient(CELLS, transport=transport, timeout=2.0, page_cap=250),
        rate_per_minute=2,
    )
    c = TestClient(app)
    me = token()
    for _ in range(2):
        assert c.get("/v1/net-worth", headers=auth(me)).status_code == 200
    calls = len(cells.seen)
    refused = c.get("/v1/overview", params={"month": "2026-11"}, headers=auth(me))
    assert refused.status_code == 429
    assert refused.json()["detail"]["error"] == "rate_limited"
    assert int(refused.headers["retry-after"]) >= 1
    assert len(cells.seen) == calls
    # Another subject has its own budget; health is never limited.
    assert c.get("/v1/net-worth", headers=auth(token())).status_code == 200
    assert c.get("/health").status_code == 200


def test_upstream_calls_are_bounded(cells):
    """AC-10: at most max_inflight cell requests at once per process."""
    import asyncio as _asyncio

    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await _asyncio.sleep(0.01)
        state["now"] -= 1
        return cells.handler(request)

    app = create_app(
        TokenVerifier(PUBLIC, ISSUER, "finance-api"),
        CellClient(
            CELLS,
            transport=httpx.MockTransport(handler),
            timeout=2.0,
            page_cap=250,
            max_inflight=3,
        ),
    )
    assert (
        TestClient(app)
        .get("/v1/overview", params={"month": "2026-11"}, headers=auth())
        .status_code
        == 200
    )
    assert state["peak"] == 3


def test_a_gateway_base_keeps_its_path():
    """AC-09: base https://gateway/v1/<cell> + /v1/<plural>."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    app = create_app(
        TokenVerifier(PUBLIC, ISSUER, "finance-api"),
        CellClient(
            {n: f"https://gw.example/v1/{n}" for n in CELLS},
            transport=httpx.MockTransport(handler),
        ),
    )
    assert TestClient(app).get("/v1/net-worth", headers=auth()).status_code == 200
    paths = {r.url.path for r in seen}
    assert "/v1/finance-accounts/v1/accounts" in paths
    assert all(r.url.host == "gw.example" for r in seen)


def test_the_rate_limiter_is_safe_across_threads():
    """AC-10: FastAPI runs the sync auth dependency in a threadpool, so check() must be atomic."""
    import threading
    import uuid as _uuid

    from finance_api.app import RateLimiter
    from finance_api.auth import Principal

    limiter = RateLimiter(50)
    me = Principal(_uuid.uuid4(), _uuid.uuid4(), None, "api", "t")
    allowed: list[bool] = []
    start = threading.Barrier(20)

    def hit() -> None:
        start.wait()
        for _ in range(10):
            allowed.append(limiter.check(me) == 0)
            # other subjects churn the dict (pruning) at the same time
            limiter.check(Principal(_uuid.uuid4(), _uuid.uuid4(), None, "api", "t"))

    import sys

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible to expose races
    try:
        threads = [threading.Thread(target=hit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert sum(allowed) == 50
    assert len(allowed) == 200  # no thread died on "dict changed size during iteration"


def test_waiting_for_an_upstream_slot_counts_against_the_timeout():
    """AC-04/AC-10: a request that cannot get an inflight slot within the timeout is a
    `timeout` for that cell, not an unbounded wait."""
    import asyncio as _asyncio

    from finance_api.cells import CellError

    client = CellClient(
        CELLS,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"items": []})
        ),
        timeout=0.05,
        max_inflight=1,
    )

    async def run() -> str:
        await client._inflight.acquire()  # every slot busy (a slow cell)
        try:
            async with client.session() as http:
                await client.list_all(http, "tax", "tax-items", "t")
        except CellError as exc:
            return exc.kind
        finally:
            client._inflight.release()
        return "no error"

    assert _asyncio.run(_asyncio.wait_for(run(), 2)) == "timeout"


def test_slot_wait_and_cell_call_share_one_deadline():
    """AC-04: time spent waiting for a slot is taken off the cell call's timeout."""
    import asyncio as _asyncio

    sent: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"items": []})

    client = CellClient(
        CELLS, transport=httpx.MockTransport(handler), timeout=1.0, max_inflight=1
    )

    async def run() -> None:
        await client._inflight.acquire()
        _asyncio.get_running_loop().call_later(0.3, client._inflight.release)
        async with client.session() as http:
            await client.list_all(http, "tax", "tax-items", "t")

    _asyncio.run(run())
    (left,) = sent
    assert 0 < left <= 0.75
