"""SPEC-005: the monthly pass on platform-credits, with fake credits and identity."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_api import ISSUER, PUBLIC, token

from finance_api.app import create_app
from finance_api.auth import TokenVerifier
from finance_api.billing import Billing, Credits, ServiceToken, billing_from_env
from finance_api.cells import DEFAULT_CELLS, CellClient

CELLS = {name: f"https://{name}.example" for name in DEFAULT_CELLS}
CREDITS = "https://credits.example"
IDENTITY = "https://identity.example"
SELLER = uuid.uuid4()
SERVICE_KEY = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
URL = "https://checkout.example/pay/secret-session"


class Fake:
    """identity /v1/token + credits accounts, prices, holds, top-ups."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.price: dict[str, Any] | None = {"unit": "month", "unit_price": 19_900}
        self.holds: list[dict[str, Any]] = []
        self.keys: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.fail_capture = 0
        self.token_requests = 0
        self.clock = time.time

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, f"{request.url.host}{request.url.path}"))
        if self.down:
            return httpx.Response(503, json={"error": "x"})
        if request.url.host == "identity.example":
            self.token_requests += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            assert form["audience"] == ["credits-api"]
            assert set(form["scope"][0].split()) == {
                "credits:spend-any",
                "credits:admin",
                "credits:topup",
            }
            now = int(time.time())
            tok = jwt.encode(
                {"ws": str(SELLER), "exp": now + 600, "sub": "svc"},
                "fake-identity-signing-key-for-tests-0123456789",
                algorithm="HS256",
            )
            return httpx.Response(200, json={"access_token": tok, "expires_in": 600})
        assert request.headers["authorization"].startswith("Bearer ")
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/accounts" and request.method == "POST":
            ref = body["owner_ref"]
            created = ref not in self.accounts
            self.accounts.setdefault(
                ref,
                {
                    "id": str(uuid.uuid4()),
                    "owner_ref": ref,
                    "available": 0,
                    "currency": "THB",
                },
            )
            return httpx.Response(201 if created else 200, json=self.accounts[ref])
        account = next(
            (
                a
                for a in self.accounts.values()
                if path.startswith(f"/v1/accounts/{a['id']}")
            ),
            None,
        )
        if path == "/v1/prices":
            if self.price is None:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200, json=[{"service": "finance-pass", "active": True, **self.price}]
            )
        if account is not None and path.endswith("/holds"):
            assert request.url.params["service"] == "finance-pass"
            assert request.url.params["status"] == "captured"
            mine = [h for h in self.holds if h["account_id"] == account["id"]]
            return httpx.Response(200, json=mine[::-1])
        if account is not None:
            return httpx.Response(200, json=account)
        if path == "/v1/holds":
            key = (body["account_id"], request.headers["idempotency-key"])
            if key in self.keys:
                if self.keys[key]["max_units"] != body["max_units"]:
                    return httpx.Response(409, json={"error": "idempotency_conflict"})
                return httpx.Response(200, json=self.keys[key])
            if self.price is None:
                return httpx.Response(422, json={"error": "unknown_price"})
            acct = next(
                a for a in self.accounts.values() if a["id"] == body["account_id"]
            )
            need = self.price["unit_price"] * body["max_units"]
            if acct["available"] < need:
                return httpx.Response(
                    402,
                    json={
                        "error": "insufficient_credits",
                        "available": acct["available"],
                        "required": need,
                    },
                )
            acct["available"] -= need
            hold = {
                "id": str(uuid.uuid4()),
                "account_id": acct["id"],
                "service": "finance-pass",
                "max_units": body["max_units"],
                "status": "held",
                "captured_units": None,
                "finished_at": None,
            }
            self.keys[key] = hold
            return httpx.Response(201, json=hold)
        if path.startswith("/v1/holds/") and path.endswith("/capture"):
            hold = next(h for h in self.keys.values() if h["id"] == path.split("/")[3])
            if self.fail_capture:
                self.fail_capture -= 1
                return httpx.Response(503, json={"error": "x"})
            if hold["status"] == "held":
                hold.update(
                    status="captured",
                    captured_units=body["units"],
                    finished_at=datetime.fromtimestamp(self.clock(), UTC).isoformat(),
                )
                self.holds.append(hold)
            return httpx.Response(200, json=hold)
        if path == "/v1/topups":
            return httpx.Response(
                201,
                json={
                    "id": str(uuid.uuid4()),
                    "account_id": body["account_id"],
                    "amount": body["amount"],
                    "currency": "THB",
                    "status": "pending",
                    "payment_status": "open",
                    "checkout_url": URL,
                },
            )
        raise AssertionError(f"unexpected {request.method} {path}")


@pytest.fixture
def fake():
    return Fake()


class Cells:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, json={"items": []})


def make(fake: Fake, mode: str = "enforce", cells: Cells | None = None, **kw: Any):
    transport = httpx.MockTransport(fake.handler)
    tokens = ServiceToken(
        IDENTITY, "finance-api", SERVICE_KEY, SELLER, transport=transport
    )
    billing = Billing(mode, Credits(CREDITS, tokens, transport=transport), **kw)
    cells = cells or Cells()
    app = create_app(
        TokenVerifier(PUBLIC, ISSUER, "finance-api"),
        CellClient(CELLS, transport=httpx.MockTransport(cells.handler), timeout=2.0),
        rate_per_minute=1000,
        billing=billing,
    )
    return TestClient(app), cells


def caller():
    t = token()
    claims = jwt.decode(t, options={"verify_signature": False})
    return {"Authorization": f"Bearer {t}"}, claims["ws"], claims["sub"]


def fund(fake: Fake, ws: str, amount: int) -> None:
    ref = f"finance:{ws}"
    fake.accounts.setdefault(
        ref,
        {"id": str(uuid.uuid4()), "owner_ref": ref, "available": 0, "currency": "THB"},
    )
    fake.accounts[ref]["available"] += amount


def key(k: str = "buy-0001") -> dict[str, str]:
    return {"Idempotency-Key": k}


def test_off_is_the_default_and_calls_nothing(fake):
    client, _ = make(fake, mode="off")
    headers, _, _ = caller()
    assert client.get("/v1/billing", headers=headers).json() == {"mode": "off"}
    assert client.get("/v1/net-worth", headers=headers).status_code == 200
    assert fake.calls == []
    assert (
        client.post(
            "/v1/billing/pass", headers=headers | key(), json={"months": 1}
        ).status_code
        == 404
    )


def test_billing_state(fake):
    client, _ = make(fake)
    headers, ws, _ = caller()
    fund(fake, ws, 50_000)
    data = client.get("/v1/billing", headers=headers).json()
    assert data == {
        "mode": "enforce",
        "pass": {"active": False, "expires_at": None},
        "balance": {"available": 50_000, "currency": "THB"},
        "price": {"unit_price": 19_900, "unit": "month"},
    }
    fake.price = None
    fresh, _ = make(fake)
    assert fresh.get("/v1/billing", headers=headers).json()["price"] is None


def test_the_gate_needs_a_pass_then_lets_through(fake):
    client, cells = make(fake)
    headers, ws, _ = caller()
    for path in ("/v1/overview", "/v1/budget-vs-actual", "/v1/net-worth"):
        refused = client.get(path, headers=headers)
        assert refused.status_code == 402
        assert refused.json() == {"detail": {"error": "pass_required"}}
    assert cells.seen == []
    # PDPA rights and billing are never gated.
    assert client.get("/v1/export", headers=headers).status_code == 200
    assert client.get("/v1/billing", headers=headers).status_code == 200
    fund(fake, ws, 50_000)
    bought = client.post(
        "/v1/billing/pass", headers=headers | key(), json={"months": 2}
    )
    assert bought.status_code == 200, bought.text
    state = bought.json()["pass"]
    assert state["active"] is True
    expires = datetime.fromisoformat(state["expires_at"])
    assert timedelta(days=59) < expires - datetime.now(UTC) <= timedelta(days=60)
    assert fake.accounts[f"finance:{ws}"]["available"] == 50_000 - 2 * 19_900
    # The purchase replaced the cached "no pass".
    assert client.get("/v1/net-worth", headers=headers).status_code == 200


def test_a_retry_never_charges_twice_and_passes_add_up(fake):
    client, _ = make(fake)
    headers, ws, actor = caller()
    fund(fake, ws, 100_000)
    first = client.post(
        "/v1/billing/pass", headers=headers | key("same-key"), json={"months": 1}
    )
    again = client.post(
        "/v1/billing/pass", headers=headers | key("same-key"), json={"months": 1}
    )
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert fake.accounts[f"finance:{ws}"]["available"] == 100_000 - 19_900
    assert "finance-pass" in json.dumps(list(fake.keys.values()))
    assert all(k[1].startswith(f"pass:{actor}:") for k in fake.keys)
    one = datetime.fromisoformat(first.json()["pass"]["expires_at"])
    later = client.post(
        "/v1/billing/pass", headers=headers | key("next-key"), json={"months": 1}
    )
    # A second pass starts where the first ends.
    assert datetime.fromisoformat(
        later.json()["pass"]["expires_at"]
    ) - one == timedelta(days=30)


def test_pass_errors(fake):
    client, _ = make(fake)
    headers, ws, _ = caller()
    fund(fake, ws, 10_000)
    poor = client.post("/v1/billing/pass", headers=headers | key(), json={"months": 1})
    assert poor.status_code == 402
    assert poor.json() == {
        "detail": {
            "error": "insufficient_credits",
            "available": 10_000,
            "required": 19_900,
        }
    }
    fake.price = None
    gone = client.post(
        "/v1/billing/pass", headers=headers | key("other-key"), json={"months": 1}
    )
    assert gone.status_code == 409
    assert gone.json() == {"detail": {"error": "pass_not_for_sale"}}
    for bad, extra in (
        ({"months": 0}, key()),
        ({"months": 13}, key()),
        ({"months": 1}, {}),
        ({"months": 1}, key("short")),
    ):
        assert client.post(
            "/v1/billing/pass", headers=headers | extra, json=bad
        ).status_code in (400, 422)


def test_topups_return_the_checkout_once_and_never_log_it(fake, caplog):
    client, _ = make(fake)
    headers, _, _ = caller()
    with caplog.at_level(logging.DEBUG):
        r = client.post(
            "/v1/billing/topups", headers=headers | key(), json={"amount": 50_000}
        )
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "id": r.json()["id"],
        "amount": 50_000,
        "currency": "THB",
        "status": "pending",
        "checkout_url": URL,
    }
    assert URL not in caplog.text
    assert "secret-session" not in caplog.text
    events = [json.loads(m) for m in caplog.messages if m.startswith("{")]
    assert any(
        e["event"] == "finance-api.topup" and e["amount"] == 50_000 for e in events
    )
    for amount in (999, 10_000_001):
        assert (
            client.post(
                "/v1/billing/topups", headers=headers | key(), json={"amount": amount}
            ).status_code
            == 422
        )


def test_shadow_answers_and_logs(fake, caplog):
    client, _ = make(fake, mode="shadow")
    headers, _, _ = caller()
    with caplog.at_level(logging.INFO, logger="finance_api"):
        assert client.get("/v1/net-worth", headers=headers).status_code == 200
    assert "finance-api.pass_missing" in caplog.text


def test_outage_fails_open_for_views_and_503_for_billing(fake, caplog):
    client, _ = make(fake)
    headers, _, _ = caller()
    fake.down = True
    with caplog.at_level(logging.INFO, logger="finance_api"):
        assert client.get("/v1/net-worth", headers=headers).status_code == 200
    assert "finance-api.pass_unknown" in caplog.text
    r = client.get("/v1/billing", headers=headers)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "30"
    assert r.json() == {"detail": {"error": "billing_unavailable"}}


def test_state_is_cached_for_a_minute(fake):
    now = [1_000_000.0]
    client, _ = make(fake, clock=lambda: now[0])
    headers, _, _ = caller()
    client.get("/v1/net-worth", headers=headers)
    calls = len(fake.calls)
    client.get("/v1/net-worth", headers=headers)
    assert len(fake.calls) == calls
    now[0] += 61
    client.get("/v1/net-worth", headers=headers)
    assert len(fake.calls) > calls


def test_the_service_token_is_reused(fake):
    client, _ = make(fake)
    headers, ws, _ = caller()
    fund(fake, ws, 100_000)
    client.get("/v1/billing", headers=headers)
    client.post("/v1/billing/pass", headers=headers | key(), json={"months": 1})
    assert fake.token_requests == 1


def test_configuration():
    assert billing_from_env({}) is None
    assert billing_from_env({"FINANCE_API_BILLING_MODE": "off"}) is None
    good = {
        "FINANCE_API_BILLING_MODE": "shadow",
        "FINANCE_API_CREDITS_URL": CREDITS,
        "FINANCE_API_IDENTITY_ISSUER": IDENTITY,
        "FINANCE_API_SERVICE_CLIENT_ID": "finance-api",
        "FINANCE_API_SERVICE_KEY_PEM": SERVICE_KEY,
        "FINANCE_API_SERVICE_WORKSPACE": str(SELLER),
    }
    billing = billing_from_env(good)
    assert billing is not None
    assert billing.mode == "shadow"
    assert SERVICE_KEY not in repr(billing)
    for name, value in (
        ("FINANCE_API_BILLING_MODE", "on"),
        ("FINANCE_API_CREDITS_URL", "http://credits.example"),
        ("FINANCE_API_IDENTITY_ISSUER", ""),
        ("FINANCE_API_SERVICE_CLIENT_ID", ""),
        ("FINANCE_API_SERVICE_KEY_PEM", "not a key"),
        ("FINANCE_API_SERVICE_WORKSPACE", "nope"),
    ):
        with pytest.raises(SystemExit):
            billing_from_env({**good, name: value})


def test_a_failed_capture_is_finished_by_the_retry(fake, caplog):
    client, _ = make(fake)
    headers, ws, _ = caller()
    fund(fake, ws, 100_000)
    fake.fail_capture = 1
    first = client.post("/v1/billing/pass", headers=headers | key(), json={"months": 1})
    assert first.status_code == 503
    with caplog.at_level(logging.INFO, logger="finance_api"):
        retry = client.post(
            "/v1/billing/pass", headers=headers | key(), json={"months": 1}
        )
    assert retry.status_code == 200
    assert retry.json()["pass"]["active"] is True
    assert fake.accounts[f"finance:{ws}"]["available"] == 100_000 - 19_900
    assert "finance-api.pass_bought" in caplog.text
    with caplog.at_level(logging.INFO, logger="finance_api"):
        caplog.clear()
        again = client.post(
            "/v1/billing/pass", headers=headers | key(), json={"months": 1}
        )
    assert again.json() == retry.json()
    assert "finance-api.pass_replayed" in caplog.text
    assert "finance-api.pass_bought" not in caplog.text


def test_a_spent_key_asks_for_a_new_one(fake):
    client, _ = make(fake)
    headers, ws, _ = caller()
    fund(fake, ws, 100_000)
    fake.fail_capture = 1
    client.post("/v1/billing/pass", headers=headers | key(), json={"months": 1})
    for hold in fake.keys.values():
        hold["status"] = "expired"  # credits gave the units back
    expired = client.post(
        "/v1/billing/pass", headers=headers | key(), json={"months": 1}
    )
    assert expired.status_code == 409
    assert expired.json() == {"detail": {"error": "idempotency_key_used"}}
    other = client.post("/v1/billing/pass", headers=headers | key(), json={"months": 3})
    assert other.status_code == 409


def test_no_pass_is_cached_briefly_and_an_outage_is_not_waited_on(fake):
    now = [1_000_000.0]
    client, _ = make(fake, clock=lambda: now[0])
    headers, ws, _ = caller()
    assert client.get("/v1/net-worth", headers=headers).status_code == 402
    # A pass bought through another replica shows within seconds.
    fund(fake, ws, 100_000)
    other, _ = make(fake)
    assert (
        other.post(
            "/v1/billing/pass", headers=headers | key(), json={"months": 1}
        ).status_code
        == 200
    )
    now[0] += 6
    assert client.get("/v1/net-worth", headers=headers).status_code == 200
    # credits down: one failed lookup, then 15 s without asking again (and the views stay open).
    fake.down = True
    now[0] += 61
    assert client.get("/v1/net-worth", headers=headers).status_code == 200
    calls = len(fake.calls)
    assert client.get("/v1/net-worth", headers=headers).status_code == 200
    assert len(fake.calls) == calls


def test_pass_arithmetic_orders_by_instant_and_refuses_bad_rows():
    from finance_api.billing import pass_state

    rows = [
        {
            "status": "captured",
            "finished_at": "2026-01-01T07:00:00+07:00",
            "captured_units": 1,
        },
        {
            "status": "captured",
            "finished_at": "2026-01-01T01:00:00Z",
            "captured_units": 2,
        },
        {
            "status": "released",
            "finished_at": "2026-01-01T00:00:00Z",
            "captured_units": None,
        },
    ]
    # 00:00Z (the +07:00 row) first: to 31 Jan; the 01:00Z row stacks from there: +60 d.
    end = pass_state(rows).expires_at
    assert end == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    assert end.utcoffset() == timedelta(0)
    assert pass_state([]).expires_at is None
    for bad in (
        ["x"],
        [{"status": "captured", "finished_at": "2026-01-01", "captured_units": "1"}],
    ):
        with pytest.raises(ValueError, match=r"hold row|captured_units"):
            pass_state(bad)


@pytest.mark.parametrize(
    "claim",
    [
        {"del": str(uuid.uuid4())},
        {"obo": str(uuid.uuid4())},
        {"act": {"sub": "agent"}},
    ],
)
def test_delegated_tokens_cannot_spend(fake, claim):
    client, _ = make(fake)
    t = token(**claim)
    ws = jwt.decode(t, options={"verify_signature": False})["ws"]
    fund(fake, ws, 100_000)
    headers = {"Authorization": f"Bearer {t}"}
    for path, body in (
        ("/v1/billing/pass", {"months": 1}),
        ("/v1/billing/topups", {"amount": 5_000}),
    ):
        r = client.post(path, headers=headers | key(), json=body)
        assert r.status_code == 403
        assert r.json() == {"detail": {"error": "delegated_token_refused"}}
    assert fake.accounts[f"finance:{ws}"]["available"] == 100_000
    assert not any(p.endswith(("/v1/holds", "/v1/topups")) for _, p in fake.calls)


def test_the_longest_accepted_key_still_fits_the_credits_header(fake):
    """platform-credits caps Idempotency-Key at 128; with the pass:/topup: namespace added it fits."""
    client, _ = make(fake)
    headers, ws, _ = caller()
    fund(fake, ws, 100_000)
    longest = "k" * 80
    ok = client.post(
        "/v1/billing/pass", headers=headers | key(longest), json={"months": 1}
    )
    assert ok.status_code == 200, ok.text
    assert all(len(k[1]) <= 128 for k in fake.keys)
    too_long = client.post(
        "/v1/billing/pass", headers=headers | key("k" * 81), json={"months": 1}
    )
    assert too_long.status_code == 400
    assert too_long.json()["detail"] == {"error": "idempotency_key_required"}
