# SPEC-005 — Monthly pass on platform-credits

Standard: ADR-0004 — TDD + SDD. Phase 6 (SaaS layer), billing part. Depends on platform-credits SPEC-004
(`GET /v1/accounts/{id}/holds`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-005 / 1.0 |
| Status | approved (2026-11-11, see Approval) |
| Repository / path | bookchaowalit/finance-api / `specs/005-monthly-pass/spec.md` |
| Owner (maker) | Claude Code (session "ระบบบัญชีหลังบ้าน", 2026-11-11) |
| Reviewer (checker) | independent reviewer subagent + Codex review Stop hook + CI gates |
| Approver | owner, in chat 2026-11-11 ("Pass รายเดือนบน credits", option A of `docs/PHASE-6-SAAS.md`) |
| Main-only | yes |

## 1. Problem and intent

- **Problem:** the suite has no way to charge. platform-credits (prepaid THB units, top-ups through
  platform-payment) is live.
- **Outcome:** a workspace tops up credits, buys a pass of 1–12 months, and the views answer only while a pass is
  current. The price lives in credits (`PUT /v1/prices/finance-pass`), so the owner sets or changes it without a
  deploy. Billing is off until the owner turns it on.

## 2. Scope

| In scope | Out of scope |
|---|---|
| `GET /v1/billing`, `POST /v1/billing/topups`, `POST /v1/billing/pass`; the pass gate on the views; modes off / shadow / enforce | Automatic renewal (a worker, later); a free tier or trial (owner decision, later); gating direct cell calls (the bundle token also reaches the cells through the gateway — closing that needs the gate in identity's `/v1/token`, a later spec); invoices / tax receipts |

## 3. Model

- One credits account per workspace, in the seller's credits workspace, `owner_ref = "finance:<workspace id>"`,
  opened on first use (`POST /v1/accounts` answers 200 for an existing identical account).
- A pass is a credits hold of service `finance-pass` for `months` units, captured at once. Each captured hold adds
  `captured_units × 30 days`, starting at its capture time or at the end of the pass then current, whichever is
  later. "Current" = now before that end.
- finance-api calls credits with its own identity service token (`client_credentials` + `private_key_jwt`;
  audience `credits-api`; scopes `credits:spend-any credits:admin credits:topup`). The customer's token never goes
  to credits.

## 4. Behaviour

| ID | Given / When / Then |
|---|---|
| AC-01 | **Given** a valid token, **when** `GET /v1/billing`, **then** 200 `{"mode", "pass": {"active", "expires_at"}, "balance": {"available", "currency"}, "price": {"unit_price", "unit"} or null}`; with mode `off` → `{"mode": "off"}` and credits is not called. |
| AC-02 | **When** `POST /v1/billing/topups {"amount"}` (1 000 – 10 000 000 units) with `Idempotency-Key` (8–100 chars), **then** a credits top-up for the workspace's account; 201/200 `{"id", "amount", "currency", "status", "checkout_url"}` with `Cache-Control: no-store`; the checkout URL is never logged. |
| AC-03 | **When** `POST /v1/billing/pass {"months": 1-12}` with `Idempotency-Key`, **then** hold + capture of `months` units of `finance-pass` (credits key `pass:<actor>:<key>`, so a retry never charges twice) and 200 with the new pass state. Not enough credits → 402 `{"error": "insufficient_credits", "available", "required"}`; no active price → 409 `pass_not_for_sale`. A key whose hold expired or was released (credits gave the units back) or that was used for another number of months → 409 `idempotency_key_used`: use a new key per purchase. A replay of a finished purchase answers 200 and charges nothing. Any member of the workspace may buy with their own token; a delegated token (an agent acting for someone, `del` claim) is refused on the pass and top-up routes (403 `delegated_token_refused`, nothing charged). A billing role (who in a workspace may spend) needs a platform permission and is a later decision. |
| AC-04 | **Given** mode `enforce` and no current pass, **then** `/v1/overview`, `/v1/budget-vs-actual`, `/v1/net-worth` answer 402 `{"error": "pass_required"}` before any cell is called. Mode `shadow` answers as usual and logs `finance-api.pass_missing`. Billing routes, `/v1/export` and `/v1/erase` are never gated (PDPA rights do not depend on payment). |
| AC-05 | **Given** credits or identity cannot answer, **then** the billing routes answer 503 `billing_unavailable` (`Retry-After: 30`) and the gate lets the request through and logs `finance-api.pass_unknown` (fail open: a paying customer is not locked out by our outage). |
| AC-06 | Pass state per workspace is cached: 60 s when current, 5 s when not (a purchase through another replica shows at once), 15 s of "unknown" after credits failed (views are not held up by an outage). A purchase on this replica replaces the entry. |
| AC-07 | Mode comes from `FINANCE_API_BILLING_MODE` (`off` default). `shadow`/`enforce` need `FINANCE_API_CREDITS_URL` (https), `FINANCE_API_IDENTITY_ISSUER` (https), `FINANCE_API_SERVICE_CLIENT_ID`, `FINANCE_API_SERVICE_KEY_PEM` (RSA ≥ 2048) and `FINANCE_API_SERVICE_WORKSPACE` (uuid); missing or invalid → the service does not start. |
| AC-08 | Every purchase that captured credits writes `finance-api.pass_bought`, a replay `finance-api.pass_replayed`, a top-up `finance-api.topup`, with actor, workspace, months or amount; never tokens or URLs. 100 or more past purchases log `finance-api.pass_history_cut` (only the newest 100 are read). |

## 5. Verification

`tests/test_billing.py` with a fake credits and identity (httpx MockTransport): AC-01 – AC-08. Red first.

## 6. Turning it on (owner)

1. Set the price: `PUT /v1/prices/finance-pass {"unit": "month", "unit_price": <THB units>}` on credits.
2. Register the `finance-api` service client in identity (`IDENTITY_SERVICE_CLIENTS_JSON`), key in Infisical.
3. Set the env of AC-07 with mode `shadow`; watch the logs; then `enforce`.

## 7. Approval

2026-11-11 owner: "Pass รายเดือนบน credits". Price, free tier and when to enforce are still the owner's.
