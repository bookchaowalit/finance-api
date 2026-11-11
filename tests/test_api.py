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
