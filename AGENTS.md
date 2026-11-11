# Agent Instructions

- Human-facing replies are in Thai by default; keep code, commands and identifiers as-is.
- Never print, commit or copy secrets. Tokens and keys live in Railway variables and Infisical only.
- finance-api is stateless: no database, no stored tokens. It forwards the caller's bearer to the finance
  cells and never adds a workspace, actor or token of its own. Keep it that way; a feature that needs state
  belongs in a cell or in an event-fed read model with its own spec.
- `src/finance_api/auth.py` and `_sentry.py` are copies of the finance cells' cellkit files (cells own their
  code); keep the verification rules in step with them (SPEC-004 identity issuer).
- Run `.venv/bin/pytest -q --cov`, `.venv/bin/mypy` and `uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .`
  before committing. Tests use `httpx.MockTransport` cells and never reach a hosted service.
- Spec before code (ADR-0004): `specs/`.
- Merging to `main` deploys production (Railway GitHub source + Wait for CI); verify `/health` afterwards.
