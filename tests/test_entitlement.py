"""SPEC-006: finance-api tells identity whether a workspace has a current pass."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_identity_issuer import (
    ID_JWK,
    ID_PRIVATE,
    IDENTITY,
    MAIN_PRIVATE,
    MAIN_PUBLIC,
    _jwks,
    _token,
)

from finance_api import billing as billing_mod
from finance_api.app import create_app
from finance_api.auth import TokenVerifier, TrustedIssuer
from finance_api.cells import DEFAULT_CELLS, CellClient

SUBJECT = uuid.uuid4()
SCOPE = "finance-api.entitlement:read"
MAIN = "empire-platform"


class FakeBilling:
    def __init__(self, mode: str = "enforce", state=None, down: bool = False) -> None:
        self.mode, self._state, self.down = mode, state, down
        self.asked: list[uuid.UUID] = []
        self.credits = None  # the billing routes are not called here

    async def state(self, workspace):
        self.asked.append(workspace)
        if self.down:
            raise billing_mod.Unavailable("down")
        return self._state


def client_for(billing=None, subject=SUBJECT):
    verifier = TokenVerifier(
        MAIN_PUBLIC,
        MAIN,
        "finance-api",
        identity=TrustedIssuer(IDENTITY, _jwks(ID_JWK)),
    )
    cells = CellClient(
        {n: f"https://{n}.example" for n in DEFAULT_CELLS},
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"items": []})
        ),
    )
    return TestClient(
        create_app(verifier, cells, billing=billing, entitlement_subject=subject)
    )


def identity_token(ws, *, sub=SUBJECT, scope=SCOPE):
    extra = {"sub": str(sub), "ws": str(ws)}
    if scope:
        extra["scope"] = scope
    return {
        "Authorization": _token(
            ID_PRIVATE, IDENTITY, ID_JWK["kid"], aud="finance-api", **extra
        )
    }


def test_active_and_lapsed_passes():
    ws = uuid.uuid4()
    later = datetime.now(UTC) + timedelta(days=3)
    on = client_for(FakeBilling(state=billing_mod.PassState(later)))
    r = on.get("/v1/internal/entitlement", headers=identity_token(ws))
    assert r.status_code == 200
    assert r.json() == {
        "active": True,
        "expires_at": later.isoformat(),
        "mode": "enforce",
    }
    off = client_for(FakeBilling(mode="shadow", state=billing_mod.PassState(None)))
    assert off.get("/v1/internal/entitlement", headers=identity_token(ws)).json() == {
        "active": False,
        "expires_at": None,
        "mode": "shadow",
    }


def test_billing_off_means_entitled():
    r = client_for(None).get(
        "/v1/internal/entitlement", headers=identity_token(uuid.uuid4())
    )
    assert r.json() == {"active": True, "expires_at": None, "mode": "off"}


def test_only_the_identity_entitlement_subject_with_its_scope():
    c = client_for(FakeBilling(state=billing_mod.PassState(None)))
    ws = uuid.uuid4()
    main = {
        "Authorization": _token(
            MAIN_PRIVATE,
            MAIN,
            None,
            aud="finance-api",
            sub=str(SUBJECT),
            ws=str(ws),
            scope=SCOPE,
        )
    }
    for headers in (
        identity_token(ws, sub=uuid.uuid4()),
        identity_token(ws, scope=None),
        identity_token(ws, scope="finance-api.other:read"),
        main,
    ):
        r = c.get("/v1/internal/entitlement", headers=headers)
        assert r.status_code == 403, headers
        assert r.json() == {"detail": {"error": "forbidden"}}
    assert c.get("/v1/internal/entitlement").status_code == 401


def test_unset_subject_hides_the_route():
    c = client_for(FakeBilling(), subject=None)
    assert (
        c.get(
            "/v1/internal/entitlement", headers=identity_token(uuid.uuid4())
        ).status_code
        == 404
    )


def test_credits_down_is_503():
    r = client_for(FakeBilling(down=True)).get(
        "/v1/internal/entitlement", headers=identity_token(uuid.uuid4())
    )
    assert r.status_code == 503
    assert r.json() == {"detail": {"error": "billing_unavailable"}}


@pytest.mark.parametrize("path", ["/v1/net-worth", "/v1/export", "/v1/billing"])
def test_the_service_subject_is_refused_elsewhere(path):
    r = client_for(None).get(path, headers=identity_token(uuid.uuid4()))
    assert r.status_code == 403
    assert r.json() == {"detail": {"error": "service_only"}}


def test_the_service_subject_cannot_erase():
    r = client_for(None).post(
        "/v1/erase",
        headers=identity_token(uuid.uuid4()),
        json={"confirm": str(uuid.uuid4())},
    )
    assert r.status_code == 403
    assert r.json() == {"detail": {"error": "service_only"}}


def test_the_token_is_checked_off_the_event_loop(monkeypatch):
    """A JWKS refresh is a blocking HTTP call: verify() must never run on the event loop."""
    import asyncio

    seen: list[bool] = []
    real = TokenVerifier.verify

    def spy(self, *args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen.append(True)
        except RuntimeError:
            seen.append(False)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(TokenVerifier, "verify", spy)
    client_for(billing=None).get(
        "/v1/internal/entitlement", headers=identity_token(uuid.uuid4())
    )
    assert seen == [False]
