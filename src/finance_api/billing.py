"""The monthly pass on platform-credits (SPEC-005).

finance-api talks to credits with its own identity service token; the customer's token never leaves
finance-api. A pass is a captured credits hold of service ``finance-pass``: each captured unit adds 30 days.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

log = logging.getLogger("finance_api")

SERVICE = "finance-pass"
DAYS_PER_UNIT = 30
SCOPES = "credits:spend-any credits:admin credits:topup"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MODES = ("off", "shadow", "enforce")
CACHE_SECONDS = 60.0  # a current pass
INACTIVE_CACHE_SECONDS = 5.0  # no pass: short, so a purchase elsewhere shows at once
UNKNOWN_CACHE_SECONDS = 15.0  # credits down: don't wait on it for every view
HOLDS_PAGE = 100
TIMEOUT = 10.0


class Unavailable(Exception):
    """credits or identity cannot answer (or answered something unexpected)."""


class Insufficient(Exception):
    def __init__(self, available: int, required: int) -> None:
        super().__init__("insufficient credits")
        self.available, self.required = available, required


class NotForSale(Exception):
    """No active ``finance-pass`` price in credits."""


class KeyUsed(Exception):
    """The Idempotency-Key belongs to a purchase that cannot complete (expired, released, other months)."""


def _https(value: str, name: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.query:
        raise ValueError(f"{name} must be a plain https URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class PassState:
    expires_at: datetime | None

    def active(self, now: datetime) -> bool:
        return self.expires_at is not None and now < self.expires_at

    def public(self, now: datetime) -> dict[str, Any]:
        return {
            "active": self.active(now),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def _instant(value: Any) -> datetime:
    at = datetime.fromisoformat(str(value))
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def pass_state(captured: list[Any]) -> PassState:
    """Each captured hold adds units x 30 days from its capture or the end of the pass then current."""
    passes: list[tuple[datetime, int]] = []
    for hold in captured:
        if not isinstance(hold, Mapping):
            raise ValueError("a hold row is not an object")
        if hold.get("status") != "captured" or not hold.get("finished_at"):
            continue
        units = hold.get("captured_units")
        if type(units) is not int or units < 1:
            raise ValueError("captured_units")
        passes.append((_instant(hold["finished_at"]).astimezone(UTC), units))
    expires: datetime | None = None
    for at, units in sorted(passes):  # by instant, whatever the offset notation
        start = max(expires, at) if expires else at
        expires = start + timedelta(days=DAYS_PER_UNIT * units)
    return PassState(expires)


class ServiceToken:
    """identity ``client_credentials`` + ``private_key_jwt``; one token reused until a minute before expiry."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        private_key_pem: str,
        workspace: uuid.UUID,
        *,
        audience: str = "credits-api",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.issuer = _https(issuer, "identity issuer")
        self.client_id, self.workspace, self.audience = client_id, workspace, audience
        try:
            key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("the service key is not a PEM private key") from exc
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("the service key must be RSA, at least 2048 bits")
        self._key = key
        self.transport = transport
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return f"ServiceToken(issuer={self.issuer!r}, client_id={self.client_id!r})"

    def _assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": f"{self.issuer}/v1/token",
            "iat": now,
            "exp": now + 120,
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(claims, self._key, algorithm="RS256")

    def invalidate(self) -> None:
        self._token, self._expires_at = None, 0.0

    async def token(self) -> str:
        async with self._lock:
            if self._token is not None and time.time() < self._expires_at - 60:
                return self._token
            try:
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=TIMEOUT, follow_redirects=False
                ) as http:
                    response = await http.post(
                        f"{self.issuer}/v1/token",
                        data={
                            "grant_type": "client_credentials",
                            "client_assertion_type": ASSERTION_TYPE,
                            "client_assertion": self._assertion(),
                            "audience": self.audience,
                            "scope": SCOPES,
                            "client_id": self.client_id,
                        },
                    )
            except httpx.HTTPError as exc:
                raise Unavailable("identity unreachable") from exc
            if response.status_code != 200:
                raise Unavailable(f"service token refused ({response.status_code})")
            try:
                body = response.json()
                token, expires_in = body["access_token"], body["expires_in"]
                ws = jwt.decode(token, options={"verify_signature": False}).get("ws")
            except (ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
                raise Unavailable("malformed service token") from exc
            if (
                not isinstance(token, str)
                or type(expires_in) is not int
                or expires_in <= 0
            ):
                raise Unavailable("malformed service token")
            if ws != str(self.workspace):
                raise Unavailable("service token for another workspace")
            self._token, self._expires_at = token, time.time() + expires_in
            return token


class Credits:
    """The few credits calls finance-api makes, with its service token."""

    def __init__(
        self,
        base: str,
        tokens: ServiceToken,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base = _https(base, "credits URL")
        self.tokens, self.transport = tokens, transport
        self._accounts: dict[uuid.UUID, str] = {}

    def __repr__(self) -> str:
        return f"Credits(base={self.base!r})"

    async def _call(
        self, method: str, path: str, *, key: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self.tokens.token()}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=TIMEOUT, follow_redirects=False
            ) as http:
                response = await http.request(
                    method, f"{self.base}{path}", headers=headers, **kwargs
                )
        except httpx.HTTPError as exc:
            raise Unavailable("credits unreachable") from exc
        if response.status_code == 401:
            self.tokens.invalidate()
        return response

    @staticmethod
    def _body(response: httpx.Response, *ok: int) -> Any:
        if response.status_code not in ok:
            raise Unavailable(f"credits answered {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise Unavailable("credits answered no JSON") from exc

    async def account(self, workspace: uuid.UUID) -> dict[str, Any]:
        if workspace in self._accounts:
            body = self._body(
                await self._call("GET", f"/v1/accounts/{self._accounts[workspace]}"),
                200,
            )
        else:
            body = self._body(
                await self._call(
                    "POST", "/v1/accounts", json={"owner_ref": f"finance:{workspace}"}
                ),
                200,
                201,
            )
        if not isinstance(body, dict) or not isinstance(body.get("id"), str):
            raise Unavailable("credits account body")
        self._accounts[workspace] = body["id"]
        return body

    async def pass_state(self, workspace: uuid.UUID) -> PassState:
        account = await self.account(workspace)
        rows = self._body(
            await self._call(
                "GET",
                f"/v1/accounts/{account['id']}/holds",
                params={
                    "service": SERVICE,
                    "status": "captured",
                    "limit": str(HOLDS_PAGE),
                },
            ),
            200,
        )
        if not isinstance(rows, list):
            raise Unavailable("credits holds body")
        if len(rows) >= HOLDS_PAGE:
            # 100 purchases: the oldest may be cut off, so the end could be too early. Say so.
            log.warning('{"event": "finance-api.pass_history_cut"}')
        try:
            return pass_state(rows)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise Unavailable("credits hold rows") from exc

    async def price(self) -> dict[str, Any] | None:
        rows = self._body(await self._call("GET", "/v1/prices"), 200)
        for row in rows if isinstance(rows, list) else []:
            if (
                isinstance(row, dict)
                and row.get("service") == SERVICE
                and row.get("active")
            ):
                return {"unit_price": row.get("unit_price"), "unit": row.get("unit")}
        return None

    async def topup(
        self, workspace: uuid.UUID, amount: int, key: str
    ) -> dict[str, Any]:
        account = await self.account(workspace)
        response = await self._call(
            "POST",
            "/v1/topups",
            key=f"topup:{workspace}:{key}",
            json={"account_id": account["id"], "amount": amount},
        )
        if response.status_code == 409:  # same key, another amount
            raise KeyUsed()
        body = self._body(response, 200, 201)
        return {
            "status_code": response.status_code,
            **{
                k: body.get(k)
                for k in ("id", "amount", "currency", "status", "checkout_url")
            },
        }

    async def buy(
        self, workspace: uuid.UUID, actor: uuid.UUID, months: int, key: str
    ) -> tuple[PassState, bool]:
        """The pass after the purchase, and whether this call captured it (False: a replay)."""
        account = await self.account(workspace)
        response = await self._call(
            "POST",
            "/v1/holds",
            key=f"pass:{actor}:{key}",
            json={"account_id": account["id"], "service": SERVICE, "max_units": months},
        )
        if response.status_code == 402:
            body = self._body(response, 402)
            try:
                available, required = int(body["available"]), int(body["required"])
            except (TypeError, ValueError, KeyError) as exc:
                raise Unavailable("credits 402 body") from exc
            raise Insufficient(available, required)
        if response.status_code == 422 and _error(response) == "unknown_price":
            raise NotForSale()
        if response.status_code == 409:  # same key, another request (e.g. other months)
            raise KeyUsed()
        hold = self._body(response, 200, 201)
        if not isinstance(hold, dict) or not isinstance(hold.get("id"), str):
            raise Unavailable("credits hold body")
        charged = False
        if hold.get("status") == "held":
            captured = self._body(
                await self._call(
                    "POST", f"/v1/holds/{hold['id']}/capture", json={"units": months}
                ),
                200,
            )
            if not isinstance(captured, dict) or captured.get("status") != "captured":
                raise Unavailable("pass hold not captured")
            charged = True
        elif hold.get("status") != "captured":
            # Expired or released (credits gave the units back): this key is spent; use a new one.
            raise KeyUsed()
        return await self.pass_state(workspace), charged


def _error(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


class Billing:
    """Mode, credits and a 60 s cache of each workspace's pass."""

    def __init__(
        self,
        mode: str,
        credits: Credits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"billing mode must be one of {MODES}")
        self.mode, self.credits, self.clock = mode, credits, clock
        self._cache: dict[uuid.UUID, tuple[float, PassState | None]] = {}

    def __repr__(self) -> str:
        return f"Billing(mode={self.mode!r}, credits={self.credits!r})"

    def remember(self, workspace: uuid.UUID, state: PassState | None) -> None:
        self._cache[workspace] = (self.clock(), state)

    async def state(self, workspace: uuid.UUID) -> PassState:
        """Unavailable also when credits failed in the last 15 s (no wait on every view)."""
        hit = self._cache.get(workspace)
        if hit:
            age, cached = self.clock() - hit[0], hit[1]
            if cached is None:
                if age < UNKNOWN_CACHE_SECONDS:
                    raise Unavailable("credits recently unavailable")
            elif age < (
                CACHE_SECONDS
                if cached.active(datetime.now(UTC))
                else INACTIVE_CACHE_SECONDS
            ):
                return cached
        try:
            state = await self.credits.pass_state(workspace)
        except Unavailable:
            self.remember(workspace, None)
            raise
        self.remember(workspace, state)
        return state


def billing_from_env(env: Mapping[str, str] = os.environ) -> Billing | None:
    """AC-07: ``FINANCE_API_BILLING_MODE`` off (default) → None; otherwise everything must be set."""
    mode = env.get("FINANCE_API_BILLING_MODE", "off").strip() or "off"
    if mode not in MODES:
        raise SystemExit(f"FINANCE_API_BILLING_MODE must be one of {', '.join(MODES)}")
    if mode == "off":
        return None
    names = (
        "FINANCE_API_CREDITS_URL",
        "FINANCE_API_IDENTITY_ISSUER",
        "FINANCE_API_SERVICE_CLIENT_ID",
        "FINANCE_API_SERVICE_KEY_PEM",
        "FINANCE_API_SERVICE_WORKSPACE",
    )
    missing = [n for n in names if not env.get(n, "").strip()]
    if missing:
        raise SystemExit(f"billing mode {mode} needs {', '.join(missing)}")
    client_id = env["FINANCE_API_SERVICE_CLIENT_ID"].strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,62}", client_id):
        raise SystemExit("FINANCE_API_SERVICE_CLIENT_ID is not a client id")
    try:
        workspace = uuid.UUID(env["FINANCE_API_SERVICE_WORKSPACE"].strip())
        tokens = ServiceToken(
            env["FINANCE_API_IDENTITY_ISSUER"].strip(),
            client_id,
            env["FINANCE_API_SERVICE_KEY_PEM"],
            workspace,
        )
        return Billing(mode, Credits(env["FINANCE_API_CREDITS_URL"].strip(), tokens))
    except ValueError as exc:
        # The message never carries the key itself.
        raise SystemExit(f"billing configuration: {exc}") from None
