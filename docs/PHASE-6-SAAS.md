# Phase 6 — the SaaS layer of the finance suite

Status 2026-11-11. Phase 6 of the finance SaaS plan has four parts: PDPA export/erase, audit, provisioning and
billing.

| Part | State | Where |
|---|---|---|
| PDPA export + erase | built (finance-api SPEC-004, cells SPEC-008) | `GET /v1/export`, `POST /v1/erase`; cells `POST /v1/<plural>/erase` |
| Audit | in place | cells: authz decision per read, platform execution per write and per erase batch (row hashes, no content); finance-api: one JSON log line per export/erase |
| Provisioning (self sign-up) | **design below, waits on the owner** | platform-identity + platform-empire |
| Billing | **options below, waits on the owner** | platform-credits (+ platform-payment) |

## Provisioning — proposed design (needs approval)

Today a workspace, identity, membership and role are created only by hand (platform-empire). identity-api reads
`platform.identities` / `platform.memberships` and has no writer, by design.

Proposal (SPEC in platform-identity once approved):

1. platform-empire migration: `platform.signup(external_subject, email, display_name, product)` — a
   `SECURITY DEFINER` function, owned by the platform owner role, that in one transaction creates the identity (or
   finds it), one workspace per (identity, product), an owner membership and the product's owner role
   (`finance.owner` = the ten cells' read/write/delete + `finance-api`). identity-api's role gets `EXECUTE` on that
   function only, never table writes.
2. platform-identity: `POST /v1/signup` with a Google `id_token` (same verification as `/v1/token`), the per-IP
   limiter and a per-subject limit; idempotent (a second call returns the same workspace); answers the workspace id.
3. A kill switch `IDENTITY_SIGNUP_PRODUCTS` (empty = sign-up off).

Blocked by: the Google OAuth client (`IDENTITY_GOOGLE_CLIENT_IDS`), owner approval of public sign-up.

## Billing — options (owner decides)

| Option | How | Needs |
|---|---|---|
| A. Monthly pass on credits (recommended for the beta) | credits account per workspace; a pass = a capture of N THB-units each month by a worker; finance-api answers 402 `plan_required` when the pass has lapsed (free tier: read-only or N records) | price, free-tier limits; PromptPay or Stripe top-ups through platform-payment (live) |
| B. Stripe subscriptions | Stripe Billing, webhook → entitlement | Stripe live account onboarding (https://dashboard.stripe.com/account/onboarding), price |
| C. Free closed beta | no billing until 5–10 users | nothing |

Owner decisions: target customer (freelancer or SME), Pro price per month, what Free includes, payment channel.

## PDPA notes for the privacy notice

What an erase does not remove, and how long it stays, is in `specs/004-pdpa-export-erase/spec.md` §6. A lake erase
job (R2 Bronze, reference data only) is a follow-up.
