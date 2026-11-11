"""SPEC-004: a cell trusts platform-identity next to the main issuer, each with its own keys and contract."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from finance_api.auth import JwksKeys, TokenVerifier, TrustedIssuer, Unauthenticated

AUD = "kit-sample-api"
MAIN = "empire-platform"
IDENTITY = "https://identity.example"


def _pair() -> tuple[str, str, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return (
        private,
        public,
        {**jwk, "alg": "RS256", "use": "sig", "kid": uuid.uuid4().hex},
    )


MAIN_PRIVATE, MAIN_PUBLIC, MAIN_JWK = _pair()
ID_PRIVATE, _ID_PUBLIC, ID_JWK = _pair()


def _jwks(jwk: dict[str, Any]) -> JwksKeys:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"keys": [jwk]})
        )
    )
    return JwksKeys("https://keys.example/jwks.json", client=client)


def _token(
    private: str,
    iss: str,
    kid: str | None,
    *,
    aud: str = AUD,
    ttl: int = 300,
    **extra: Any,
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": str(uuid.uuid4()),
        "ws": str(uuid.uuid4()),
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        **extra,
    }
    return "Bearer " + jwt.encode(
        claims, private, algorithm="RS256", headers={"kid": kid} if kid else None
    )


def _verifier(identity: bool = True) -> TokenVerifier:
    return TokenVerifier(
        MAIN_PUBLIC,
        MAIN,
        AUD,
        jwks=_jwks(MAIN_JWK),
        identity=TrustedIssuer(IDENTITY, _jwks(ID_JWK)) if identity else None,
    )


def test_ac01_without_identity_trust_only_the_main_issuer_verifies():
    verifier = _verifier(identity=False)
    assert verifier.verify(_token(MAIN_PRIVATE, MAIN, MAIN_JWK["kid"]), None)
    with pytest.raises(Unauthenticated):
        verifier.verify(_token(ID_PRIVATE, IDENTITY, ID_JWK["kid"]), None)


def test_ac02_an_identity_token_binds_the_principal_to_its_workspace():
    token = _token(ID_PRIVATE, IDENTITY, ID_JWK["kid"])
    claims = jwt.decode(
        token.removeprefix("Bearer "), options={"verify_signature": False}
    )
    principal = _verifier().verify(token, "api")
    assert principal.workspace == uuid.UUID(claims["ws"])
    assert principal.actor == uuid.UUID(claims["sub"])
    assert principal.delegation is None
    # the main issuer keeps working next to it
    assert _verifier().verify(_token(MAIN_PRIVATE, MAIN, MAIN_JWK["kid"]), None)
    assert _verifier().verify(
        _token(MAIN_PRIVATE, MAIN, None), None
    )  # pinned key fallback


@pytest.mark.parametrize("kid", [MAIN_JWK["kid"], None])
def test_ac03_an_identity_token_signed_with_a_main_key_is_refused(kid):
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(MAIN_PRIVATE, IDENTITY, kid), None)


@pytest.mark.parametrize("kid", [ID_JWK["kid"], None])
def test_ac04_a_main_token_signed_with_an_identity_key_is_refused(kid):
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(ID_PRIVATE, MAIN, kid), None)


@pytest.mark.parametrize(
    "extra",
    [
        {"del": str(uuid.uuid4())},
        {"obo": str(uuid.uuid4())},
        {"act": {"sub": str(uuid.uuid4())}},
    ],
)
def test_ac05_identity_tokens_never_act_for_someone_else(extra):
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(ID_PRIVATE, IDENTITY, ID_JWK["kid"], **extra), None)


def test_ac06_identity_tokens_are_short_lived():
    verifier = _verifier()
    assert verifier.verify(_token(ID_PRIVATE, IDENTITY, ID_JWK["kid"], ttl=3600), None)
    with pytest.raises(Unauthenticated, match="lifetime"):
        verifier.verify(_token(ID_PRIVATE, IDENTITY, ID_JWK["kid"], ttl=3601), None)


def test_ac07_wrong_audience_or_unknown_issuer_is_refused():
    verifier = _verifier()
    with pytest.raises(Unauthenticated):
        verifier.verify(
            _token(ID_PRIVATE, IDENTITY, ID_JWK["kid"], aud="other-api"), None
        )
    with pytest.raises(Unauthenticated):
        verifier.verify(_token(ID_PRIVATE, "https://evil.example", ID_JWK["kid"]), None)
    with pytest.raises(Unauthenticated):
        verifier.verify("Bearer not-a-jwt", None)


def test_ac08_identity_issuer_must_differ_from_the_main_issuer():
    with pytest.raises(ValueError, match="differ"):
        TokenVerifier(
            MAIN_PUBLIC, MAIN, AUD, identity=TrustedIssuer(MAIN, _jwks(ID_JWK))
        )
