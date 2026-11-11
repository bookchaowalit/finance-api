# SPEC-003 — finance-api is not limited as one Cloudflare IP

Standard: ADR-0004 — TDD + SDD. Phase 5 (ops) of the finance SaaS plan.

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-003 / 1.0 |
| Status | approved (2026-11-11, see Approval) |
| Repository / path | bookchaowalit/finance-api / `specs/003-edge-rate-limit-bypass/spec.md` |
| Owner (maker) | Claude Code (session "ระบบบัญชีหลังบ้าน", 2026-11-11) |
| Reviewer (checker) | Codex review Stop hook + CI gates |
| Approver | owner, in chat 2026-11-11 ("แก้ด้วย header ลับ") |
| Main-only | yes |

## 1. Problem and intent

- **Problem:** the Cloudflare rule "gateway per-IP rate limit (SPEC-001)" allows 60 requests per 10 s per IP on
  `gateway.bookchaowalit.com`. finance-api calls every cell through the gateway from one Railway IP for all users;
  one overview is about ten requests, so about six overviews per 10 s across *all* users block finance-api for 10 s.
- **Outcome:** finance-api's calls to the gateway carry a secret header; a Cloudflare custom rule with the *Skip*
  action skips the rate-limiting phase for requests that carry it. Per-user limits stay: the gateway's 120/min per
  subject and finance-api's own 12/min per subject.

## 2. Scope

| In scope | Out of scope |
|---|---|
| `FINANCE_API_EDGE_BYPASS` (optional) → header `X-Edge-Bypass` on requests to the gateway host only; the Cloudflare skip rule (zone config, recorded here) | Changing the per-IP limit for everyone else; the gateway (it does not forward the header: `FORWARD_REQUEST` allowlist) |

## 3. Behaviour

| ID | Given / When / Then |
|---|---|
| AC-01 | **Given** `FINANCE_API_EDGE_BYPASS` is set, **when** finance-api calls a cell URL on `gateway.bookchaowalit.com`, **then** the request carries `X-Edge-Bypass: <value>`. |
| AC-02 | **Given** a cell URL on any other host (a `FINANCE_API_CELLS` override), **then** the header is never sent. |
| AC-03 | **Given** the variable is unset or empty, **then** no header is sent (behaviour as before). |
| AC-04 | **Given** the value has characters outside `[A-Za-z0-9_-]` or is shorter than 32, **then** the service does not start. |
| AC-05 | **Given** the Cloudflare skip rule, **when** a request to the gateway host carries the exact header value, **then** the `http_ratelimit` phase is skipped; without it the per-IP rule applies as before. |

## 4. Interfaces and data

| Interface | Change |
|---|---|
| Env | `FINANCE_API_EDGE_BYPASS` (secret; Infisical `/cloudflare` `GATEWAY_RATELIMIT_BYPASS` is the source) |
| Cloudflare zone `bookchaowalit.com` | custom rule "Skip gateway rate limit for finance-api (SPEC-003)": `http.host eq "gateway.bookchaowalit.com" and any(http.request.headers["x-edge-bypass"][*] eq "<value>")` → skip phase `http_ratelimit` |

## 5. Risks

The value sits in the zone's rule expression (readable by anyone with zone read access) and in finance-api's
environment. A leak lets the holder skip only the per-IP edge limit; tokens, the gateway's per-subject limit and the
cells' own checks still apply. Rotate: new value in Infisical → Railway → rule.

## 6. Tests

`tests/test_edge_bypass.py` (AC-01–AC-04). AC-05 is checked live after the rule is created (70 requests in 10 s with the
header pass; without it the 61st is blocked).

## Approval

Owner in chat 2026-11-11: "แก้ด้วย header ลับ".
