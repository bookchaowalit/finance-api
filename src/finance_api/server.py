"""``finance-api``: configuration from the environment, then uvicorn (SPEC-001)."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sys
import urllib.parse
import uuid

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
    inflight = int(env.get(f"{PREFIX}_MAX_INFLIGHT", "16"))
    try:
        return CellClient(
            urls, timeout=timeout, max_inflight=inflight, edge_bypass=edge_bypass(env)
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def edge_bypass(env: dict[str, str] | os._Environ[str] = os.environ) -> str | None:
    """SPEC-003: the secret that skips the Cloudflare per-IP limit; unset or empty = none."""
    value = env.get(f"{PREFIX}_EDGE_BYPASS", "")
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value):
        raise SystemExit(
            f"{PREFIX}_EDGE_BYPASS must be 32-256 characters of [A-Za-z0-9_-]"
        )
    return value


def entitlement_subject(
    env: dict[str, str] | os._Environ[str] = os.environ,
) -> uuid.UUID | None:
    """SPEC-006: identity's entitlement subject; unset = no /v1/internal/entitlement."""
    raw = env.get(f"{PREFIX}_ENTITLEMENT_SUBJECT", "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise SystemExit(f"{PREFIX}_ENTITLEMENT_SUBJECT must be a UUID") from None


def rate_per_minute(env: dict[str, str] | os._Environ[str] = os.environ) -> int:
    """Requests per subject per minute; each one fans out to up to 8 cells (SPEC-001 AC-10)."""
    value = int(env.get(f"{PREFIX}_RATE_PER_MINUTE", "12"))
    if value < 1:
        raise SystemExit(f"{PREFIX}_RATE_PER_MINUTE must be at least 1")
    return value


def configure_logging() -> logging.Logger:
    """The service's own INFO lines (SPEC-004 AC-08 audit) on stdout; uvicorn sets up only its own."""
    logger = logging.getLogger("finance_api")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def run() -> None:
    import uvicorn

    from finance_api import gateway
    from finance_api._sentry import init_sentry
    from finance_api.app import create_app

    init_sentry()
    configure_logging()
    # platform-api-gateway SPEC-002: off until the gateway owner sets GATEWAY_ASSERTION_MODE/KEY.
    check = gateway.GatewayCheck.from_env(gateway.SYSTEM)
    from finance_api.billing import billing_from_env

    app = create_app(
        verifier(),
        cells(),
        rate_per_minute=rate_per_minute(),
        billing=billing_from_env(),
        entitlement_subject=entitlement_subject(),
    )
    gateway.install(app, check)
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "3790")),
        proxy_headers=False,
    )
