# SPEC-004 — PDPA: export and erase a workspace

Standard: ADR-0004 — TDD + SDD. Phase 6 (SaaS layer) of the finance SaaS plan (2026-11-11).
Depends on the cells' SPEC-008 (`POST /v1/<plural>/erase`) in all ten finance services.

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-004 / 1.0 |
| Status | approved (2026-11-11, see Approval) |
| Repository / path | bookchaowalit/finance-api / `specs/004-pdpa-export-erase/spec.md` |
| Owner (maker) | Claude Code (session "ระบบบัญชีหลังบ้าน", 2026-11-11) |
| Reviewer (checker) | independent reviewer subagent + Codex review Stop hook + CI gates |
| Approver | owner, in chat 2026-11-11 ("ทำ phase 6 ต่อเลย") |
| Main-only | yes |

## 1. Problem and intent

- **Problem:** financial data is sensitive under the PDPA. A customer has the right to get a copy of their data
  (section 30/31, data portability) and to have it erased (section 33). The suite keeps a workspace's data in ten
  cells; nothing reads or erases all of it in one place.
- **Outcome:** `GET /v1/export` returns every record of the caller's workspace in one JSON file;
  `POST /v1/erase` erases them in the order the references need, in bounded steps the caller repeats until done.

## 2. Scope

| In scope | Out of scope |
|---|---|
| Export of all 15 resources of the ten cells; erase across the ten cells; a stricter rate limit for both; an audit log line per call | The web page and the confirmation screen (finance-suite-web); erasing the workspace or membership in platform-empire/identity (provisioning, a later spec); erasing copies outside the cells (see §6, retention) |

## 3. Behaviour

| ID | Given / When / Then |
|---|---|
| AC-01 | **Given** a valid token, **when** `GET /v1/export`, **then** 200 with `Content-Disposition: attachment; filename="finance-export-<workspace>-<YYYYMMDD>.json"`, `Cache-Control: no-store` and `{"format": "finance-export/1", "workspace", "generated_at", "records": {"<cell>.<plural>": [rows…]}}` holding every record of all 15 resources, read with the caller's own bearer (each cell checks `:read`). |
| AC-02 | **Given** any resource that cannot be read (timeout, unreachable, forbidden, cell error) or has more than 20 000 records, or the reads take longer than 70 seconds (`timeout`), **then** 502 `{"error": "export_incomplete", "sources": {"<cell>.<plural>": "<reason>"}}` (`too_large` for the cap) — an export is complete or not given. Only one export runs at a time per replica (503 `export_busy`, `Retry-After: 30`): it holds the whole workspace in memory. |
| AC-03 | **Given** `POST /v1/erase` without `{"confirm": "<the caller's workspace id>"}` (missing, other value, extra fields), **then** 422 and no cell is called. |
| AC-04 | **Given** a confirmed erase, **then** finance-api calls the cells' `POST /v1/<plural>/erase` in three stages — (1) every resource nothing points at, (2) investments, market prices, assets, subscriptions, (3) accounts — repeating each resource while it still erases records, moving to the next stage only when the stage has nothing left. A resource that erased nothing but still has records (still pointed at while the references catch up) is retried every 3 seconds. Everything runs within a hard 60-second budget (a batch is not let run past it), under Cloudflare's 100 seconds. |
| AC-05 | **Given** the run ends, **then** 200 `{"done": bool, "resources": {"<cell>.<plural>": {"erased": n, "remaining": m} or {"error": "<reason>"}}}`; `done` is true only when every resource reports `remaining = 0`. Records still referenced (references not yet caught up) or the budget running out leave `done` false: the caller repeats the call. |
| AC-06 | **Given** a cell refuses (403 → `forbidden`, 503 → `unavailable`, other → `cell_error`, timeout, unreachable), **then** that resource reports the error, the later stages do not run, and `done` is false. |
| AC-07 | **Given** export or erase, **then** each has a stricter limit per subject (actor + workspace) on top of the general 12/min: export 3 and erase 10 per 10 minutes (429 with `Retry-After`). An erase refused for its confirmation (AC-03) costs no erase slot. |
| AC-08 | **Given** export or erase, **then** one JSON log line `finance-api.export` / `finance-api.erase` names the actor, workspace, outcome and counts (no record content); the cells keep their own audit (authz decisions for reads, one platform execution per erase batch). |

## 4. Design

- `cells.CellClient.erase()` posts one batch with the same slot, deadline and error mapping as `list_all`.
- `app.RESOURCES` lists the 15 resources; `ERASE_STAGES` the three stages; both are checked by a test against a copy
  of the cells' `cell.yaml` references in `tests/test_pdpa.py` (dependents always in an earlier stage).
- The export reads with `list_all(page_cap=20 000)` under one semaphore and a 70-second deadline; the erase runs the resources of a stage concurrently (bounded
  by `max_inflight`).

## 5. Verification

- `tests/test_pdpa.py` with fake cells: AC-01 – AC-08; `server.configure_logging` puts the audit lines on stdout
  (uvicorn configures only its own loggers).
- Red first (404 on the new routes), then green; coverage stays ≥ 90 %.

## 6. Retention of copies outside the cells (for the privacy notice)

| Copy | What it holds | How long |
|---|---|---|
| Cell outbox rows, `finance.events.v1` on Kafka, the R2 lake (Bronze) | reference data only (ids, dates, types, currency, status — never amounts, names or notes; SPEC-005) | outbox: kept; Kafka: topic retention; lake: until a lake erase job (follow-up) |
| Nightly encrypted database backups (ops-backups → R2) | everything | until the backup ages out of the backup retention |
| Platform executions / authz decisions | who did what, row hashes (no content) | platform retention |
| `create_keys` (idempotency keys) | client-chosen keys and record ids | kept (no customer content) |

## 7. Approval

- 2026-11-11 owner: "ทำ phase 6 ต่อเลย". Phase 6 = provisioning, billing, PDPA export/delete, audit; this spec is the
  PDPA part. Provisioning and billing wait on owner decisions (see `docs/PHASE-6-SAAS.md`).
