# 01 — Business process (As-Is / To-Be)

finance-api คือ BFF แบบ stateless ของ finance suite: เว็บ finance-suite-web และ client ฝั่งเซิร์ฟเวอร์เรียกครั้งเดียวเพื่อได้ภาพรวมการเงิน (overview, budget vs actual, net worth) ที่รวมจาก 10 finance cells โดยส่ง bearer ของผู้เรียกต่อไปให้แต่ละ cell; นอกจากนั้นให้ export/erase ตาม PDPA และขายแพ็กเกจรายเดือน (pass) ด้วยเครดิตจาก platform-credits ซึ่ง finance-api ไม่มีฐานข้อมูลของตัวเอง

Last reviewed: 2026-11-27 · commit `192dc4b`

## As-Is (จากโค้ดวันนี้)

```mermaid
flowchart LR
  WEB["finance-suite-web / server client + bearer"] --> AUTH["verify JWT aud finance-api, per-principal rate limit"]
  AUTH -->|invalid| E401["401 / 429"]
  AUTH --> ROUTE{"route"}
  ROUTE -->|"overview, budget-vs-actual, net-worth"| GATE{"billing mode"}
  GATE -->|"enforce and no current pass"| E402["402 pass_required"]
  GATE -->|"enforce and active pass"| FAN
  GATE -->|"off, shadow, or credits unreachable (fail open)"| FAN["fan out to cells with the caller's bearer"]
  FAN --> GW["edge gateway gateway.bookchaowalit.com"]
  GW --> CELLS[("10 finance cells: accounts, transactions, budgets, cash-flow, ...")]
  CELLS --> VIEW["views.py: exact decimals per currency; a failing cell sets ok:false on every section that uses it (net-worth: the whole result)"]
  VIEW --> RESP["200 sections ok or error"]
  ROUTE -->|"GET /v1/export"| EXP["read all 15 resources in parallel, 25 s"]
  EXP -->|"any read fails"| E502["502 export_incomplete, no partial data"]
  ROUTE -->|"POST /v1/erase"| ERASE["confirm must equal caller workspace (else 422), then erase in 3 staged steps, 25 s"]
  EXP --> GW
  ERASE --> GW
  ROUTE -->|"POST /v1/billing/pass, /topups"| MEMBER["person token only, member check via a cell"]
  MEMBER --> CREDITS["platform-credits: hold + capture finance-pass, top-up checkout"]
  ROUTE -->|"GET /v1/billing"| CREDITS
  ENT["identity asks GET /v1/internal/entitlement"] --> CREDITS
  E502 --> LOG["audit JSON log line only for export, erase, pass, topup, pass_missing/unknown; Sentry has no personal data"]
  ERASE --> LOG
```

Sources: `src/finance_api/app.py`, `src/finance_api/cells.py`, `src/finance_api/views.py`, `src/finance_api/billing.py`, `src/finance_api/auth.py`, `src/finance_api/gateway.py`, `README.md`, `PRODUCT.md`, `specs/004-pdpa-export-erase/spec.md`, `specs/005-monthly-pass/spec.md`, `specs/006-entitlement-for-identity/spec.md`

ข้อเท็จจริงสำคัญจากโค้ด:

- `FINANCE_API_BILLING_MODE` default คือ `off`; โหมด `shadow` แค่ log `finance-api.pass_missing`, `enforce` ตอบ 402; `/v1/export`, `/v1/erase` และ route billing ไม่ถูก gate
- เมื่อ credits/identity ตอบไม่ได้ gate ปล่อยผ่าน (fail open) และ log `finance-api.pass_unknown`
- token แบบ delegated (agent) ถูกปฏิเสธที่ route ซื้อ pass และ top-up
- โหมด billing ที่ production ตั้งไว้จริงตอนนี้อ่านไม่ได้จากโค้ด [ต้องยืนยัน]
- `finance-suite-web` ใน `PRODUCT.md` ยังเป็น "planned"; สถานะ deploy จริงของเว็บ [ต้องยืนยัน]

## To-Be (จาก `docs/PHASE-6-SAAS.md`, `specs/005-monthly-pass`, `specs/004-pdpa-export-erase`)

```mermaid
flowchart LR
  WEB["finance-suite-web"] --> AUTH["verify JWT"]
  SIGNUP["เปิด signup product finance ใน production (endpoint มีแล้ว ใน platform-identity)"] --> AUTH
  AUTH --> GATE{"pass current? (enforce)"}
  GATE -->|"มี free tier ตามที่ owner กำหนด"| FAN["fan out to cells"]
  GATE -->|yes| FAN
  FAN --> CELLS[("10 finance cells")]
  CELLS --> RESP["200 sections"]
  RENEW["worker ต่อ pass อัตโนมัติ"] --> CREDITS["platform-credits"]
  GATE --> CREDITS
  LAKE["lake erase job: R2 Bronze"] --> CELLS
  CLOSE["identity enforce จำกัด scope ด้วย ceiling อยู่แล้ว; ตัดสินใจเรื่อง :read และ :delete ที่ยังผ่าน"] --> AUTH
```

Sources: `docs/PHASE-6-SAAS.md` (Provisioning, Billing), `specs/005-monthly-pass/spec.md` §2 (out of scope "later"), `specs/004-pdpa-export-erase/spec.md` §6

## Gap

| ID | As-Is → To-Be | Status | Owner | Evidence |
|---|---|---|---|---|
| G1 | `POST /v1/signup` และ `tenancy_signup` มีใน platform-identity แล้ว (ปิดได้ด้วย signup mode/products) → ยืนยันว่า production เปิด tenancy และอนุญาต signup ของ product `finance` [ต้องยืนยัน] | decide | owner | `platform-identity` `src/platform_identity/tenancy.py` (`signup`); `docs/PHASE-6-SAAS.md` "Provisioning" |
| G2 | pass ต่อเองไม่ได้ → ต่ออัตโนมัติโดย worker | todo | claude | `specs/005-monthly-pass/spec.md` §2 "Automatic renewal (a worker, later)" |
| G3 | ไม่มี free tier หรือ trial → owner กำหนดราคา ขอบเขต free tier และเวลา enforce | decide | owner | `specs/005-monthly-pass/spec.md` §2, Approval note; `docs/PHASE-6-SAAS.md` "Owner decisions" |
| G4 | identity ใช้ entitlement จำกัด scope ของ token เมื่อ mode เป็น enforce และไม่มี pass แล้ว (ceiling) แต่ยังให้ `:read` และ `:delete` เรียก cell ตรงได้ → ตัดสินใจว่าจะปิดสองสิทธิ์นี้หรือไม่ และยืนยัน mode ของ identity ใน production [ต้องยืนยัน] | decide | owner | `platform-identity` `src/platform_identity/app.py` (`cap_scopes`); `specs/006-entitlement-for-identity/spec.md` |
| G5 | ข้อมูลอ้างอิงใน R2 Bronze ไม่ถูกลบตอน erase → lake erase job | todo | claude | `specs/004-pdpa-export-erase/spec.md` §6 |
| G6 | route table ของ gateway ตั้ง `max_body_bytes` ของ `finance-api` เป็น 8 MiB แล้ว (เดิมเพดาน 1 MiB) → ยืนยันว่าค่านี้ deploy ใน production แล้ว [ต้องยืนยัน] | done | owner | `platform-api-gateway` `routes.json` limits.finance-api; `specs/004-pdpa-export-erase/spec.md` change log 1.1 |
| G7 | ใครมีสิทธิ์ซื้อ pass ในเวิร์กสเปซ (billing role) ยังไม่กำหนด → ตัดสินใจ | decide | owner | `specs/005-monthly-pass/spec.md` AC-03 |

## Open questions

- production ตั้ง `FINANCE_API_BILLING_MODE` เป็นอะไร (off, shadow, enforce)
- สถานะ deploy ของ `finance-suite-web` (PRODUCT.md เขียนว่า planned)
- ใบแจ้งหนี้/ใบกำกับภาษี อยู่นอกขอบเขตของ spec 005 และไม่มี To-Be ที่เขียนไว้
