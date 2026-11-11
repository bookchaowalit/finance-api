"""Startup configuration (SPEC-001 §4)."""

from __future__ import annotations

import pytest

from finance_api import server
from finance_api.cells import DEFAULT_CELLS

PEM = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA\n-----END PUBLIC KEY-----"


def test_the_verifier_needs_an_issuer_and_a_key():
    for env in (
        {},
        {"FINANCE_API_JWT_ISSUER": "empire-platform"},
        {"FINANCE_API_JWT_PUBLIC_KEY_PEM": PEM},
    ):
        with pytest.raises(SystemExit):
            server.verifier(env)
    with pytest.raises(SystemExit, match="dev issuer"):
        server.verifier(
            {
                "FINANCE_API_JWT_ISSUER": "empire-platform-dev",
                "FINANCE_API_JWT_PUBLIC_KEY_PEM": PEM,
            }
        )
    v = server.verifier(
        {
            "FINANCE_API_JWT_ISSUER": "empire-platform",
            "FINANCE_API_JWT_PUBLIC_KEY_PEM": PEM,
        }
    )
    assert v.audience == "finance-api"


def test_the_identity_issuer_comes_in_pairs_over_https():
    base = {
        "FINANCE_API_JWT_ISSUER": "empire-platform",
        "FINANCE_API_JWT_PUBLIC_KEY_PEM": PEM,
    }
    with pytest.raises(SystemExit, match="both"):
        server.verifier(
            {**base, "FINANCE_API_JWT_IDENTITY_ISSUER": "platform-identity"}
        )
    with pytest.raises(SystemExit, match="differ"):
        server.verifier(
            {
                **base,
                "FINANCE_API_JWT_IDENTITY_ISSUER": "empire-platform",
                "FINANCE_API_JWT_IDENTITY_JWKS_URL": "https://id.example/jwks",
            }
        )
    with pytest.raises(SystemExit, match="https"):
        server.verifier(
            {
                **base,
                "FINANCE_API_JWT_IDENTITY_ISSUER": "platform-identity",
                "FINANCE_API_JWT_IDENTITY_JWKS_URL": "http://id.example/jwks",
            }
        )
    v = server.verifier(
        {
            **base,
            "FINANCE_API_JWT_IDENTITY_ISSUER": "platform-identity",
            "FINANCE_API_JWT_IDENTITY_JWKS_URL": "https://id.example/jwks",
        }
    )
    assert v.identity is not None


def test_cell_urls_default_to_production_and_can_be_overridden():
    assert server.cells({}).urls == DEFAULT_CELLS
    custom = server.cells(
        {
            "FINANCE_API_CELLS": '{"tax": "https://tax.example"}',
            "FINANCE_API_TIMEOUT_SECONDS": "5",
        }
    )
    assert custom.urls["tax"] == "https://tax.example"
    assert custom.timeout == 5.0
    with pytest.raises(SystemExit):
        server.cells({"FINANCE_API_CELLS": '{"tax": "http://tax.example"}'})
