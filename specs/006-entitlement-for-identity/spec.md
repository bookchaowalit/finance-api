# SPEC-006 — Entitlement answer for identity (no pass, no writes)

Standard: ADR-0004. Pairs with platform-identity SPEC-005 (the read-only scope ceiling).

| Field | Value |
|---|---|
| Spec ID / version | SPEC-006 / 1.1 |
| Status | approved (owner 2026-11-12: "ทำไปเลย เราอนุมัติทั้งหมดหลังจากนี้") |
| Repository / path | bookchaowalit/finance-api / `specs/006-entitlement-for-identity/spec.md` |
| Owner (maker) / reviewer | Claude Code / solo:solo-reviewer + Codex Stop hook + CI |
| Main-only | yes |

## 1. Problem and intent
A finance-api bundle token (aud = finance-api + the cells) reaches the cells directly through the gateway, so the
pass gate on finance-api's views (SPEC-005) can be skipped. identity will issue a token without a current pass
with a scope ceiling of `:read` and `:delete` only (the platform PDP enforces `scope`). It asks finance-api whether
the workspace has a current pass.

## 2. Behaviour
| ID | Given / When / Then |
|---|---|
| AC-01 | `GET /v1/internal/entitlement` with an identity-issued token whose `sub` is `FINANCE_API_ENTITLEMENT_SUBJECT`, `scope` holds `finance-api.entitlement:read`, `ws` = the workspace asked about → 200 `{"active": bool, "expires_at": str|null, "mode": "off"|"shadow"|"enforce"}`. Billing off → `active: true`. |
| AC-02 | Any other token (main issuer, another subject, missing scope, delegated) → 403 `{"error": "forbidden"}`; no token → 401. Unset subject → the route answers 404. |
| AC-03 | credits unavailable → 503 `billing_unavailable` (identity then fails open). The answer uses the SPEC-005 cache (60 s current, 5 s none). |
| AC-04 | The entitlement subject's token is refused on every other route (403 `service_only`). |
| AC-05 | `Principal` carries the token's `scope` (None when absent) and whether identity issued it; nothing else changes. |

## 3. Verification
`tests/test_entitlement.py`; red first.

## Change log

| Version | Date | Change | Approved by |
|---|---|---|---|
| 1.0 | 2026-11-11 | first version | owner, chat 2026-11-11 |
| 1.1 | 2026-11-12 | Codex review: the entitlement route verifies the token in a worker thread (a JWKS refresh is a blocking HTTP call and stalled the event loop). Red: `test_the_token_is_checked_off_the_event_loop`; green: 130 passed. | owner, chat 2026-11-11 |
