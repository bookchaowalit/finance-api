"""``finance-api``: configuration from the environment, then uvicorn (SPEC-001)."""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.parse

from finance_api.auth import JwksKeys, TokenVerifier, TrustedIssuer
from finance_api.cells import DEFAULT_CELLS, CellClient

AUDIENCE = "finance-api"
PREFIX = "FINANCE_API"


def checked_url(url: str, name: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise SystemExit(f"{name} must be https (http only on loopback)")
    return url.rstrip("/")


def verifier(env: dict[str, str] | os._Environ[str] = os.environ) -> TokenVerifier:
    pem = env.get(f"{PREFIX}_JWT_PUBLIC_KEY_PEM", "").strip()
    jwks_url = env.get(f"{PREFIX}_JWT_JWKS_URL", "").strip()
    issuer = env.get(f"{PREFIX}_JWT_ISSUER", "").strip()
    if not (pem or jwks_url) or not issuer:
        raise SystemExit(
            f"set {PREFIX}_JWT_PUBLIC_KEY_PEM or {PREFIX}_JWT_JWKS_URL, and {PREFIX}_JWT_ISSUER"
        )
    if issuer == "empire-platform-dev":
        raise SystemExit("finance-api refuses the dev issuer")
    jwks = (
        JwksKeys(checked_url(jwks_url, f"{PREFIX}_JWT_JWKS_URL")) if jwks_url else None
    )
    identity = None
    id_issuer = env.get(f"{PREFIX}_JWT_IDENTITY_ISSUER", "").strip()
    id_url = env.get(f"{PREFIX}_JWT_IDENTITY_JWKS_URL", "").strip()
    if id_issuer or id_url:
        if not (id_issuer and id_url):
            raise SystemExit(
                f"set both {PREFIX}_JWT_IDENTITY_ISSUER and {PREFIX}_JWT_IDENTITY_JWKS_URL, or neither"
            )
        if id_issuer == issuer:
            raise SystemExit(
                f"{PREFIX}_JWT_IDENTITY_ISSUER must differ from {PREFIX}_JWT_ISSUER"
            )
        identity = TrustedIssuer(
            id_issuer, JwksKeys(checked_url(id_url, f"{PREFIX}_JWT_IDENTITY_JWKS_URL"))
        )
    return TokenVerifier(pem, issuer, AUDIENCE, jwks=jwks, identity=identity)


def cells(env: dict[str, str] | os._Environ[str] = os.environ) -> CellClient:
    raw = env.get(f"{PREFIX}_CELLS", "").strip()
    urls = {**DEFAULT_CELLS, **(json.loads(raw) if raw else {})}
    timeout = float(env.get(f"{PREFIX}_TIMEOUT_SECONDS", "20"))
    try:
        return CellClient(urls, timeout=timeout)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def run() -> None:
    import uvicorn

    from finance_api._sentry import init_sentry
    from finance_api.app import create_app

    init_sentry()
    app = create_app(verifier(), cells())
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "3790")),
        proxy_headers=False,
    )
